"""The web app: FastAPI routes under /api and the static front end.

Start it with  python -m app  (see app/__main__.py) or run.bat. It only ever
listens on 127.0.0.1, and LocalOnlyMiddleware refuses anything that is not a
request from this PC to this app.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, export
from .analysis import sqlrunner
from .analysis.relations import Relations
from .analysis.report import Report
from .analysis.scripts import Scripts
from .jobs import Busy, Jobs
from .model import RANK, relationship_kinds
from .profiling.data import QueryFailed
from .profiling.service import Profiling
from .security import BodyLimitMiddleware, LocalOnlyMiddleware, ReadOnlyViolation
from .sources import SourceError
from .sources.database import ConnectionProfile, ReadOnlyDatabase, engines_info
from .sources.files import DATA_EXT
from .workspace import NeedsPassword, NotFound, Workspaces, new_id

MAX_HOPS = 10


def create_app(data_dir=None):
    # No /docs or /openapi.json: the interactive docs load their scripts from a CDN.
    app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)
    # Added first = innermost: the localhost guard below still sees every request before this one.
    app.add_middleware(BodyLimitMiddleware, limit=lambda: config.MAX_UPLOAD_BYTES,
                       message='This upload is larger than the limit of {mb} MB. Give the file or folder path '
                               'instead: files are then read in place, without a copy.')
    app.add_middleware(LocalOnlyMiddleware)
    ws = Workspaces(data_dir or config.DATA_DIR)
    app.state.workspaces = ws
    jobs = Jobs()                       # one running job per workspace, whatever its kind
    app.state.jobs = jobs
    prof = Profiling(ws, jobs)
    app.state.profiling = prof
    relations = Relations(ws, prof, jobs)
    app.state.relations = relations
    runner = sqlrunner.SqlRunner(ws, prof)
    app.state.sql = runner
    report = Report(ws, prof, relations)
    app.state.report = report
    scripts = Scripts(ws, jobs)
    app.state.scripts = scripts

    # ------------------------------------------------------------- errors
    @app.exception_handler(SourceError)
    async def _source_error(request: Request, exc: SourceError):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.exception_handler(NeedsPassword)
    async def _needs_password(request: Request, exc: NeedsPassword):
        return JSONResponse({'detail': str(exc), 'needs_password': True, 'source': getattr(exc, 'source', None)},
                            status_code=409)

    @app.exception_handler(Busy)
    async def _busy(request: Request, exc: Busy):
        return JSONResponse({'detail': str(exc), 'job': exc.job}, status_code=409)

    @app.exception_handler(sqlrunner.Busy)
    async def _query_running(request: Request, exc: sqlrunner.Busy):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(QueryFailed)
    async def _query_failed(request: Request, exc: QueryFailed):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(ReadOnlyViolation)
    async def _read_only(request: Request, exc: ReadOnlyViolation):
        return JSONResponse({'detail': f'Blocked by the read-only guard: {exc}'}, status_code=400)

    @app.exception_handler(export.NothingToExport)
    async def _nothing(request: Request, exc: export.NothingToExport):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(export.TooBigForPng)
    async def _too_big(request: Request, exc: export.TooBigForPng):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    # --------------------------------------------------------------- basics
    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'app': config.APP_NAME, 'version': config.APP_VERSION}

    @app.get('/api/meta')
    def meta():
        return {'app': config.APP_NAME, 'version': config.APP_VERSION, 'engines': engines_info(),
                'features': config.FEATURES, 'statement_timeout_s': config.STATEMENT_TIMEOUT_S,
                'data_extensions': list(DATA_EXT), 'relationship_kinds': relationship_kinds(),
                'max_upload_mb': config.MAX_UPLOAD_BYTES // (1024 * 1024),
                'sample_threshold_rows': config.SAMPLE_TARGET_ROWS,
                'sample_target_rows': config.SAMPLE_TARGET_ROWS,
                'file_sample_threshold_rows': config.SAMPLE_THRESHOLD_ROWS,
                'table_budget_s': config.TABLE_BUDGET_S}

    @app.get('/api/session')
    def get_session():
        """Settings that live in this process's memory only and are gone when the app stops."""
        return {'show_personal': prof.show_personal}

    @app.put('/api/session')
    def set_session(body: dict = Body(default={})):
        """The "show personal values" switch. It is never saved: every start of the app begins masked."""
        if 'show_personal' in body:
            prof.show_personal = body['show_personal'] is True
        return {'show_personal': prof.show_personal}

    # ----------------------------------------------------------- workspaces
    @app.get('/api/workspaces')
    def list_workspaces():
        return ws.list_workspaces()

    @app.post('/api/workspaces')
    def create_workspace(body: dict = Body(default={})):
        return ws.create_workspace(body.get('name'))

    @app.get('/api/workspaces/{ws_id}')
    def get_workspace(ws_id: str):
        return ws.get_workspace(ws_id)

    @app.patch('/api/workspaces/{ws_id}')
    def rename_workspace(ws_id: str, body: dict = Body(default={})):
        return ws.rename_workspace(ws_id, body.get('name'))

    @app.delete('/api/workspaces/{ws_id}')
    def delete_workspace(ws_id: str):
        ws.delete_workspace(ws_id)
        return {'deleted': ws_id}

    # ------------------------------------------------- database connections
    @app.post('/api/db/test')
    def test_connection(body: dict = Body(default={})):
        """Try a connection without saving anything."""
        db = ReadOnlyDatabase(ConnectionProfile.from_dict(body), body.get('password'))
        try:
            return db.test()
        finally:
            db.dispose()

    @app.get('/api/profiles')
    def list_profiles():
        return ws.list_profiles()

    @app.post('/api/profiles')
    def save_profile(body: dict = Body(default={})):
        return ws.save_profile(body.get('name'), body)        # ConnectionProfile has no password field

    @app.delete('/api/profiles/{profile_id}')
    def delete_profile(profile_id: str):
        ws.delete_profile(profile_id)
        return {'deleted': profile_id}

    # --------------------------------------------------------------- sources
    def add_file_source(ws_id, kind, extensions, files, path, label):
        ws.get_workspace(ws_id)
        files = [f for f in files or [] if f.filename]
        path = (path or '').strip()
        if files:
            source_id = new_id()
            folder = ws.upload_dir(ws_id, source_id)
            try:
                names = _save_uploads(files, folder, extensions)
                return ws.add_source(ws_id, kind, {'mode': 'upload', 'path': str(folder), 'files': names},
                                     label, source_id=source_id)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
        if not path:
            raise SourceError('Choose files to upload, or enter a file or folder path on this PC.')
        return ws.add_source(ws_id, kind, {'mode': 'path', 'path': path}, label)

    @app.post('/api/workspaces/{ws_id}/sources/sql')
    def add_sql_source(ws_id: str, files: list[UploadFile] = File(default=[]),
                       path: str = Form(default=''), label: str = Form(default='')):
        return add_file_source(ws_id, 'sql_files', ('.sql',), files, path, label)

    @app.post('/api/workspaces/{ws_id}/sources/files')
    def add_files_source(ws_id: str, files: list[UploadFile] = File(default=[]),
                         path: str = Form(default=''), label: str = Form(default='')):
        return add_file_source(ws_id, 'files', DATA_EXT, files, path, label)

    @app.post('/api/workspaces/{ws_id}/sources/db')
    def add_db_source(ws_id: str, body: dict = Body(default={})):
        source = ws.add_source(ws_id, 'database', body, body.get('label'), password=body.get('password'))
        if body.get('save_profile'):
            ws.save_profile(body.get('profile_name'), body)
        return source

    @app.post('/api/workspaces/{ws_id}/sources/{source_id}/refresh')
    def refresh_source(ws_id: str, source_id: str, body: dict = Body(default={})):
        return ws.refresh_source(ws_id, source_id, body.get('password'))

    @app.delete('/api/workspaces/{ws_id}/sources/{source_id}/password')
    def forget_password(ws_id: str, source_id: str):
        """Drop a database source's password from memory. Reading it again will ask for it."""
        return ws.forget_password(ws_id, source_id)

    @app.delete('/api/workspaces/{ws_id}/sources/{source_id}')
    def remove_source(ws_id: str, source_id: str):
        ws.remove_source(ws_id, source_id)
        return {'deleted': source_id}

    # ----------------------------------------------------------------- model
    @app.get('/api/workspaces/{ws_id}/model')
    def get_model(ws_id: str):
        """The merged model: every table (without its columns) and every relationship."""
        model = ws.model(ws_id)
        counts = prof.row_counts(ws_id)

        def summary(t):
            rows, kind = counts.get(t.key, (None, None))
            return {**t.to_dict(columns=False), 'column_names': [c.name for c in t.columns],
                    'row_count': rows, 'row_count_kind': kind, 'can_profile': prof.can_profile(t)}
        return {'workspace': ws.get_workspace(ws_id),
                'tables': [summary(t) for t in model.tables],
                'relationships': [{'id': f'r{i}', **r.to_dict()} for i, r in enumerate(model.relationships)],
                'schemas': sorted({t.schema or '' for t in model.tables}),
                'stats': model.stats()}

    # ------------------------------------------------- profiling and quality
    @app.post('/api/workspaces/{ws_id}/profile')
    def start_profile(ws_id: str, body: dict = Body(default={})):
        """Start a background profiling job. Body: {"tables": [ids] or "all", "counts_only": false}."""
        return prof.start(ws_id, body.get('tables', 'all'), counts_only=body.get('counts_only') is True,
                          passwords=body.get('passwords'))

    @app.get('/api/workspaces/{ws_id}/profile')
    def profile_overview(ws_id: str):
        """Row counts and profile status of every table, and the workspace's current (or last) job."""
        return prof.overview(ws_id)

    @app.delete('/api/workspaces/{ws_id}/profile')
    def clear_statistics(ws_id: str):
        """Delete the saved row counts and statistics of this workspace."""
        return prof.clear(ws_id)

    @app.get('/api/jobs/{job_id}')
    def get_job(job_id: str):
        return jobs.get(job_id)

    @app.delete('/api/jobs/{job_id}')
    def cancel_job(job_id: str):
        """Ask a job (profiling or a relationship check) to stop. It stops before its next step."""
        return jobs.cancel(job_id)

    @app.get('/api/workspaces/{ws_id}/quality')
    def get_quality(ws_id: str):
        return prof.quality(ws_id)

    @app.post('/api/workspaces/{ws_id}/personal')
    def mark_personal(ws_id: str, body: dict = Body(default={})):
        """Mark a column personal / not personal: {"table": id, "column": name, "personal": true|false|null}."""
        return prof.mark(ws_id, body.get('table') or '', body.get('column') or '', body.get('personal'))

    # --------------------------------------------------- relationship checks
    @app.get('/api/workspaces/{ws_id}/relations/check')
    def relation_checks(ws_id: str):
        """Every relationship with its last check (coverage, orphans, cardinality), and the suggested links."""
        return relations.overview(ws_id)

    @app.post('/api/workspaces/{ws_id}/relations/check')
    def start_relation_check(ws_id: str, body: dict = Body(default={})):
        """Start a background check. Body: {"relationships": "all" or [keys], "suggest": false}."""
        return relations.start(ws_id, body.get('relationships', 'all'), suggest=body.get('suggest') is True,
                               passwords=body.get('passwords'))

    @app.post('/api/workspaces/{ws_id}/relations/suggestions')
    def decide_suggestion(ws_id: str, body: dict = Body(default={})):
        """Accept or reject a link suggested by values: {"key": ..., "action": "accept" | "reject" | "reset"}."""
        return relations.decide(ws_id, body.get('key'), body.get('action'))

    # ------------------------------------------------------------- SQL runner
    @app.get('/api/workspaces/{ws_id}/sql')
    def sql_info(ws_id: str):
        """The sources a query can run against, the last query texts, and the limits."""
        return runner.info(ws_id)

    @app.post('/api/workspaces/{ws_id}/sql')
    def run_sql(ws_id: str, body: dict = Body(default={})):
        """Run one read-only query: {"source": id, "sql": text}. At most 1,000 rows come back."""
        return runner.run(ws_id, body.get('source'), body.get('sql'), password=body.get('password'))

    @app.delete('/api/workspaces/{ws_id}/sql/history')
    def clear_sql_history(ws_id: str):
        return runner.clear_history(ws_id)

    @app.get('/api/workspaces/{ws_id}/sql/results/{result_id}.csv')
    def sql_result_csv(ws_id: str, result_id: str):
        """A result as CSV, exactly as it was shown (masked values stay masked)."""
        name, content = runner.csv(ws_id, result_id)
        return Response(content, media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})

    # These three come before the plain table route: its {table_id:path} would swallow them.
    @app.get('/api/workspaces/{ws_id}/tables/{table_id:path}/profile')
    def get_table_profile(ws_id: str, table_id: str, sample: bool = True):
        """Column statistics of one table as last saved, and its first rows read live (masked)."""
        return prof.table_profile(ws_id, table_id, sample=sample)

    @app.get('/api/workspaces/{ws_id}/tables/{table_id:path}/sample')
    def get_table_sample(ws_id: str, table_id: str):
        return prof.sample(ws_id, table_id)

    @app.post('/api/workspaces/{ws_id}/tables/{table_id:path}/count')
    def count_table(ws_id: str, table_id: str):
        """An exact COUNT(*) of one table, under the statement timeout."""
        return prof.exact_count(ws_id, table_id)

    @app.get('/api/workspaces/{ws_id}/tables/{table_id:path}')
    def get_table(ws_id: str, table_id: str):
        """One table: its columns, its relationships and where the information came from."""
        model = ws.model(ws_id)
        table = model.find(table_id)
        if table is None:
            raise NotFound('Table not found.')
        links = []
        for i, r in enumerate(model.relationships):
            if r.from_table is not table and r.to_table is not table:
                continue
            outgoing = r.from_table is table
            other = r.to_table if outgoing else r.from_table
            if r.from_table is r.to_table:
                role = 'self'
            elif r.cardinality == 'unknown':
                role = 'match'
            else:
                role = 'references' if outgoing else 'referenced_by'
            links.append({'id': f'r{i}', 'role': role, 'other': other.id,
                          'columns': r.from_columns if outgoing else r.to_columns,
                          'other_columns': r.to_columns if outgoing else r.from_columns,
                          'source': r.source, 'confidence': r.confidence, 'cardinality': r.cardinality,
                          'name': r.name, 'evidence': r.evidence, 'seen': r.seen})
        # "profile": a short summary of the saved profile; None means not profiled yet.
        saved = prof.store.tables(ws_id).get(table.key) or {}
        profile = None
        if saved.get('profiled_at'):
            profile = {'profiled_at': saved['profiled_at'], 'sampled': bool(saved['profile'].get('sampled')),
                       'rows_profiled': saved['profile'].get('rows_profiled')}
        return {**table.to_dict(), 'row_count': saved.get('row_count'), 'row_count_kind': saved.get('row_count_kind'),
                'can_profile': prof.can_profile(table), 'relationships': links, 'profile': profile}

    @app.get('/api/workspaces/{ws_id}/export')
    def export_diagram(ws_id: str, format: str = 'drawio', focus: str | None = None, hops: int = 1,
                       kinds: str | None = None, min_confidence: float = 0.0, schema: str | None = None,
                       all_columns: bool = False):
        """The whole diagram, or the same view as on screen (filters, and N hops around `focus`)."""
        if format not in ('drawio', 'png'):
            raise SourceError('format must be drawio or png.')
        model = ws.model(ws_id)
        name = ws.get_workspace(ws_id)['name']
        focus_table = None
        if focus:
            focus_table = model.find(focus)
            if focus_table is None:
                raise NotFound('Table not found.')
        hops = max(1, min(int(hops), MAX_HOPS))
        tables, relationships = model.view(
            kinds=None if kinds is None else {k for k in kinds.split(',') if k in RANK},
            min_confidence=min_confidence, schemas=None if schema is None else [schema],
            focus=focus_table, hops=hops)
        label, stem = name, export.file_name(name)
        if focus_table is not None:
            label = f'{name} - {focus_table.name} ({hops} hop{"s" if hops > 1 else ""})'
            stem = export.file_name(name, focus_table.name, f'{hops}hop')
        if format == 'drawio':
            content, media = export.to_drawio(model, tables, relationships, label), 'application/xml'
        else:
            content, media = export.to_png(model, tables, relationships, label, all_columns), 'image/png'
        return Response(content, media_type=media,
                        headers={'Content-Disposition': f'attachment; filename="{stem}.{format}"'})

    @app.get('/api/workspaces/{ws_id}/report')
    def get_report(ws_id: str, format: str = 'html', scope: str = 'all', focus: str | None = None, hops: int = 1,
                   kinds: str | None = None, min_confidence: float = 0.0, schema: str | None = None):
        """The report as a download: one self-contained HTML file, or an Excel workbook. Always masked.
        scope=all is the whole workspace; scope=view takes the same filters as the diagram export."""
        if format not in ('html', 'xlsx'):
            raise SourceError('format must be html or xlsx.')
        if scope not in ('all', 'view'):
            raise SourceError('scope must be all or view.')
        if focus and ws.model(ws_id).find(focus) is None:
            raise NotFound('Table not found.')
        options = {'scope': scope, 'focus': focus, 'hops': max(1, min(int(hops), MAX_HOPS)),
                   'kinds': None if kinds is None else [k for k in kinds.split(',') if k],
                   'min_confidence': min_confidence, 'schema': schema}
        name, content = report.html(ws_id, **options) if format == 'html' else report.xlsx(ws_id, **options)
        media = 'text/html; charset=utf-8' if format == 'html' else (
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="{name}"'})

    # ---------------------------------------------------- scripts (SQL Lineage)
    @app.post('/api/workspaces/{ws_id}/scripts/scan')
    def scan_scripts(ws_id: str, body: dict = Body(default={})):
        """Scan a folder of .sql and .py scripts in the background: {"folder": path, "dialect": "sqlite"}.
        The scripts are only read, never run; nothing is written into the folder. A new scan replaces the last."""
        return scripts.start_scan(ws_id, body.get('folder'), body.get('dialect'))

    @app.get('/api/workspaces/{ws_id}/scripts')
    def scripts_overview(ws_id: str):
        """The last scan: summary, join catalog, conflicts, suggested views, subquery links, joins without a
        key, unreadable statements; and the dialects, the preselected one and the scan job."""
        return scripts.overview(ws_id)

    @app.get('/api/workspaces/{ws_id}/scripts/impact')
    def scripts_impact(ws_id: str, name: str = ''):
        """What a change to TABLE or TABLE.COLUMN touches: the direct uses, then level by level."""
        return scripts.impact(ws_id, name)

    @app.get('/api/workspaces/{ws_id}/scripts/report')
    def scripts_report(ws_id: str):
        """The scan's offline report (one HTML file, no network), built in memory."""
        name, content = scripts.report(ws_id)
        return Response(content, media_type='text/html; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{name}"'})

    @app.delete('/api/workspaces/{ws_id}/scripts')
    def forget_scripts(ws_id: str):
        """Forget the scan: its saved row and its graph file."""
        return scripts.forget(ws_id)

    # ------------------------------------------------------------- front end
    @app.get('/')
    def index():
        return FileResponse(config.STATIC_DIR / 'index.html', headers={'Cache-Control': 'no-cache'})

    app.mount('/static', StaticFiles(directory=config.STATIC_DIR), name='static')
    return app


def _save_uploads(files, folder, extensions):
    """Store uploaded files in the workspace's own folder under data/uploads. Returns their names."""
    wrong = [f.filename for f in files if Path(f.filename).suffix.lower() not in extensions]
    if wrong:
        raise SourceError(f'Not accepted here: {", ".join(wrong[:5])}. Expected {", ".join(extensions)}.')
    folder.mkdir(parents=True, exist_ok=True)
    names = []
    for f in files:
        name = Path(f.filename.replace('\\', '/')).name or 'file'
        target, n = folder / name, 1
        while target.exists():
            n += 1
            target = folder / f'{Path(name).stem}_{n}{Path(name).suffix}'
        with open(target, 'wb') as out:
            shutil.copyfileobj(f.file, out)
        names.append(target.name)
    return names
