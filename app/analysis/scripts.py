"""The Scripts tab: scan a folder of the team's .sql and .py scripts with the SQL Lineage engine.

Not to be confused with lineage.py next to it: that one follows the columns of ONE query typed in the
SQL tab, so that its result can be masked. This module reads the scripts of a folder and has nothing to
do with masking.

    Scripts.start_scan()    a background job (kind "scripts", one job per workspace as always): read every
                            script of the folder - never run one - and save the graph
    Scripts.overview()      the last scan: summary, join catalog, conflicts, suggested views, subquery
                            links, joins without a key, unreadable statements
    Scripts.impact()        what a change to a table or column touches, level by level
    Scripts.report()        the engine's offline report.html, built in memory for a download
    Scripts.forget()        delete the scan

The engine is app/sql_lineage/, a byte-for-byte copy of C:\\Projects\\SQL_Lineage\\sql_lineage refreshed by
scripts/sync_lineage.py. Never edit it here, and call only its public functions (the imports below).
The schema the scripts are checked against is the workspace model: its tables, columns, primary keys and
DECLARED relationships. Links inferred from names or joins, and accepted value links, are not declared keys.
A table of a database source carries its schema (an Oracle owner); scripts usually leave it out, and the
engine finds such a name in the schema by its rule (Schema.resolve). The default schema of the workspace's
one database source, kept when the source is read, decides a name that several schemas have.

Saved: one row per workspace in script_scans (data/explorer.db, layout 4) and the graph in
data/lineage/<workspace id>.db. A new scan replaces both; they go with the workspace and with "Forget
scan". Nothing is written into the scanned folder.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from ..export import file_name
from ..jobs import Busy, Failed
from ..sources import SourceError
from ..workspace import NotFound

# The engine's modules import each other as "sql_lineage", so the app folder is on the import path while
# it is imported (and only then: the app's own modules stay out of the way of other packages).
_APP_DIR = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _APP_DIR)
try:
    from sql_lineage import settings as engine_settings
    from sql_lineage.analysis import analyze, impact
    from sql_lineage.graph import load_graph, save_graph, scan_folder
    from sql_lineage.output import build_report
    from sql_lineage.parse import schema_from_tables
finally:
    sys.path.remove(_APP_DIR)

DIALECTS = engine_settings.DIALECTS
# The engine's dialect for each database engine of the app (ENGINES in app/sources/database.py).
DIALECT_OF_ENGINE = {'oracle': 'oracle', 'mssql': 'tsql', 'postgresql': 'postgres', 'mysql': 'mysql',
                     'sqlite': 'sqlite'}
# What the Scripts tab shows of a scan: the engine's analyze() results with these names.
RESULTS = ('summary', 'catalog', 'conflicts', 'groups', 'subquery_links', 'joins_without_key', 'unreadable',
           'unknowns')


def schema_tables(model):
    """The workspace model as the engine's plain data (schema_from_tables): every table with its columns
    and primary key, and the declared relationships only. A table no source listed in full (seen only
    in a query) has columns None: not known."""
    keys = {}
    for r in model.relationships:
        if r.source == 'declared':
            keys.setdefault(id(r.from_table), []).append({
                'columns': list(r.from_columns), 'ref_table': r.to_table.name, 'ref_schema': r.to_table.schema or '',
                'ref_columns': list(r.to_columns)})
    return [{'name': t.name, 'schema': t.schema or '', 'kind': 'view' if t.kind == 'view' else 'table',
             'columns': [c.name for c in t.columns] if t.complete else None,
             'primary_key': [c.name for c in t.columns if c.pk],
             'foreign_keys': keys.get(id(t), [])} for t in model.tables]


class Scripts:
    def __init__(self, workspaces, jobs):
        self.ws = workspaces
        self.jobs = jobs
        self.folder = workspaces.data_dir / 'lineage'
        self._loaded = {}               # workspace id -> (scanned_at, graph, analysis) of its last scan
        self._lock = threading.RLock()
        workspaces.on_remove.append(self._removing)
        workspaces.on_change.append(self._changed)

    # ----------------------------------------------------------- workspace
    def _removing(self, workspace_id, source_id=None):
        """Before a workspace is deleted: stop its scan, so that it saves nothing afterwards. (A scan reads
        no source, so removing one source does not stop it.)"""
        if source_id is None:
            self.jobs.stop_for(workspace_id)

    def _changed(self, workspace_id, removed_source=None, deleted=False):
        if deleted:                     # its script_scans row went with it (ON DELETE CASCADE)
            self._drop_file(workspace_id)

    def _file(self, workspace_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', workspace_id):
            raise NotFound('Workspace not found.')
        return self.folder / f'{workspace_id}.db'

    def _drop_file(self, workspace_id):
        with self._lock:
            self._loaded.pop(workspace_id, None)
            path = self._file(workspace_id)
            if path.exists():
                path.unlink()

    def default_dialect(self, workspace):
        """The dialect of the workspace's one database source, or None (no database source, or several)."""
        databases = [s for s in workspace['sources'] if s['kind'] == 'database']
        if len(databases) != 1:
            return None
        return DIALECT_OF_ENGINE.get(databases[0]['settings'].get('engine'))

    def default_schema(self, workspace):
        """The default schema of the workspace's one database source, as saved when it was read, or None (no
        database source, several, or one saved before the app kept it: it has one after its next reload)."""
        databases = [s for s in workspace['sources'] if s['kind'] == 'database']
        return databases[0].get('default_schema') if len(databases) == 1 else None

    def views_note(self, workspace, graph):
        """The note above the results when names of the scan are not in the workspace model and a database
        source was read without views (the model is the schema, so it has none of their views). Or None."""
        if not graph.meta.get('schema'):
            return None
        missing = sum(1 for t in graph.tables if t.in_schema is False)
        without = [s['label'] for s in workspace['sources']
                   if s['kind'] == 'database' and not s['settings'].get('include_views')]
        if not missing or not without:
            return None
        names = '1 name is' if missing == 1 else f'{missing:,} names are'
        if len(without) == 1:
            return (f'{names} not in the workspace model. {without[0]} was read without views: if some of them '
                    'are views, tick Include views on the source, reload it and scan again.')
        return (f'{names} not in the workspace model. {", ".join(without[:-1])} and {without[-1]} were read without '
                'views: if some of them are views, tick Include views on the sources, reload them and scan again.')

    # ----------------------------------------------------------------- scan
    def start_scan(self, workspace_id, folder, dialect):
        """Start reading a scripts folder in the background. Returns the job."""
        workspace = self.ws.get_workspace(workspace_id)
        if dialect not in DIALECTS:
            raise SourceError(f'Choose the dialect of the scripts: {", ".join(DIALECTS)}.')
        text = (folder or '').strip().strip('"').strip()
        if not text:
            raise SourceError('Enter the path of a folder of scripts on this PC.')
        path = Path(text)
        if not path.exists():
            raise SourceError(f'The folder {path} does not exist.')
        if not path.is_dir():
            raise SourceError(f'{path} is not a folder. Enter the folder that holds the scripts.')
        path = path.resolve()
        if self.folder.resolve().is_relative_to(path):
            raise SourceError(f'{path} holds the app\'s data folder, where scans are saved, and the app never '
                              'writes into a folder it scans. Choose the folder of the scripts themselves.')
        # A workspace without tables gives no schema at all: an empty one would call every join "without a key".
        tables = schema_tables(self.ws.model(workspace_id))
        declared = sum(len(t['foreign_keys']) for t in tables)
        schema = schema_from_tables(tables, dialect, source=f'the workspace model ({len(tables)} tables, '
                                    f'{declared} declared keys)') if tables else None
        default_schema = self.default_schema(workspace)
        job = self.jobs.start(workspace_id, 'scripts', 0,
                              lambda job: self._run(job, path, dialect, schema, default_schema),
                              phase='reading scripts', folder=str(path), dialect=dialect)
        return job.to_dict()

    def _run(self, job, folder, dialect, schema, default_schema=None):
        def progress(done, total, path):
            job.check()                 # Cancel stops the scan between two files
            job.done, job.total, job.current = done, total, path

        try:
            graph = scan_folder(folder, dialect, schema=schema, progress=progress, default_schema=default_schema)
        except ValueError as exc:       # more scripts than the engine's MAX_FILES
            raise Failed(str(exc)) from None
        job.check()
        job.phase = 'saving'
        result = analyze(graph)
        with self.jobs.saving(job):     # nothing is saved for a scan that was cancelled or whose workspace is gone
            self._save(job.workspace_id, graph, result)

    def _save(self, workspace_id, graph, result):
        path = self._file(workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f'{workspace_id}.new.db')
        save_graph(graph, temporary)
        with self._lock:
            os.replace(temporary, path)
            with self.ws._db() as con:
                con.execute('INSERT INTO script_scans (workspace_id, folder, dialect, scanned_at, summary, lineage_file) '
                            'VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (workspace_id) DO UPDATE SET folder = excluded.folder, '
                            'dialect = excluded.dialect, scanned_at = excluded.scanned_at, summary = excluded.summary, '
                            'lineage_file = excluded.lineage_file',
                            (workspace_id, graph.meta['folder'], graph.meta['dialect'], graph.meta['scanned_at'],
                             json.dumps(result['summary']), path.relative_to(self.ws.data_dir).as_posix()))
            self._loaded[workspace_id] = (graph.meta['scanned_at'], graph, result)

    # --------------------------------------------------------------- reading
    def _row(self, workspace_id):
        with self.ws._db() as con:
            row = con.execute('SELECT * FROM script_scans WHERE workspace_id = ?', (workspace_id,)).fetchone()
        return dict(row) if row else None

    def _load(self, workspace_id):
        """(row, graph, analysis) of the last scan, or None. The graph is read from its file once."""
        row = self._row(workspace_id)
        if row is None:
            return None
        with self._lock:
            loaded = self._loaded.get(workspace_id)
            if loaded is None or loaded[0] != row['scanned_at']:
                path = self.ws.data_dir / row['lineage_file']
                if not path.is_file():
                    return None
                graph = load_graph(path)
                loaded = (row['scanned_at'], graph, analyze(graph))
                self._loaded[workspace_id] = loaded
        return row, loaded[1], loaded[2]

    def _need(self, workspace_id):
        self.ws.get_workspace(workspace_id)
        found = self._load(workspace_id)
        if found is None:
            raise NotFound('This workspace has no scan of scripts yet. Scan a folder first.')
        return found

    def overview(self, workspace_id):
        """Everything the tab shows: the choices for a new scan, the scan job, and the last scan's results."""
        workspace = self.ws.get_workspace(workspace_id)
        job = self.jobs.latest(workspace_id, 'scripts')
        info = {'dialects': list(DIALECTS), 'default_dialect': self.default_dialect(workspace),
                'limits': {'max_files': engine_settings.MAX_FILES, 'max_file_mb': engine_settings.MAX_FILE_MB},
                'job': job.to_dict() if job else None, 'scan': None}
        found = self._load(workspace_id)
        if found is None:
            return info
        row, graph, result = found
        meta = graph.meta
        info['scan'] = {'folder': row['folder'], 'dialect': row['dialect'], 'scanned_at': row['scanned_at'],
                        'schema': meta.get('schema'), 'default_schema': meta.get('default_schema'),
                        'schema_warnings': meta.get('schema_warnings') or [],
                        'tool_version': meta.get('tool_version'), 'sqlglot_version': meta.get('sqlglot_version'),
                        'settings': meta.get('settings') or {}}
        info.update({name: result[name] for name in RESULTS})
        info['views_note'] = self.views_note(workspace, graph)
        info['targets'] = sorted(t.key for t in graph.tables) + sorted(c.key for c in graph.columns if c.name != '*')
        model = self.ws.model(workspace_id)
        info['in_model'] = {}           # table key of the scan -> table id in the app, for the tables of the model
        for table in graph.tables:
            found_table = model.find(table.key)
            if found_table is not None:
                info['in_model'][table.key] = found_table.id
        return info

    def impact(self, workspace_id, name):
        """The impact of TABLE or TABLE.COLUMN, as the engine works it out."""
        _, graph, _ = self._need(workspace_id)
        name = (name or '').strip()
        if not name:
            raise SourceError('Choose a table or a column (TABLE.COLUMN).')
        result = impact(graph, name)
        if result['target'] is None:
            hint = f' Did you mean: {", ".join(result["suggestions"])}?' if result.get('suggestions') else ''
            raise NotFound(f'{name} is not a table or column of this scan.{hint}')
        return result

    def report(self, workspace_id):
        """(file name, the offline report.html as text), built in memory: no file is written."""
        _, graph, result = self._need(workspace_id)
        name = self.ws.get_workspace(workspace_id)['name']
        return f'{file_name(name, "scripts")}.html', build_report(graph, result)

    def forget(self, workspace_id):
        """Delete the scan: its row and its graph file."""
        self.ws.get_workspace(workspace_id)
        running = self.jobs.running(workspace_id)
        if running is not None and running.kind == 'scripts':
            raise Busy(running.to_dict())
        with self._lock:
            with self.ws._db() as con:
                done = con.execute('DELETE FROM script_scans WHERE workspace_id = ?', (workspace_id,)).rowcount
            self._drop_file(workspace_id)
        return {'forgotten': bool(done)}
