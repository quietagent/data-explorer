"""Profiling as the API sees it: background jobs, saved results, personal-data marks, quality.

    Profiling.start()          start a job for some or all tables of a workspace
    Profiling.overview()       row counts and profile status of every table
    Profiling.table_profile()  one table: column statistics, and its sample rows read live
    Profiling.quality()        data-quality findings

A job (app.jobs) runs in a thread of this process: it survives closing the browser tab, not a
restart of the app. Per table it opens one connection and sends ONE read (see profiler.py); the
statistics are worked out in memory. Each table has a time budget; when it is used up the table
is marked "timed out" and the job moves on. "Cancel" is checked between the steps.

Whether a column is personal is decided at the moment a table's statistics are saved, with the
user's marks as they are then, and the masking happens then. Removing a source or a workspace
cancels the job that reads it; nothing is saved for it afterwards.

The "show personal values" switch is a plain attribute of this object: it lives in memory only
and is off again whenever the app starts. While it is on, the real values that were read for a
table are kept in memory, so opening the table again does not read them again.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..jobs import Busy, Cancelled, Jobs
from ..security import ReadOnlyViolation, sanitize_error
from ..sources import SourceError
from ..sources.database import ConnectionProfile, ReadOnlyDatabase
from ..sources.files import source_root
from ..workspace import NeedsPassword, NotFound
from . import queries as q
from .data import DatabaseData, FileData, QueryFailed
from .personal import CATEGORY_TEXT, mask, mask_stats, personal_by_name, personal_by_values
from .profiler import (Estimates, TimedOut, bounded_count, budget_check, catalog_estimates, exact_count, jsonable, live_values,
                       mask_result, profile_copy, read_table, sample_rows, table_summary)
from .quality import RULES, SEVERITIES, workspace_findings
from .store import ProfileStore

STRUCTURE_ONLY = 'structure only, no data to profile'
MORE_THAN = 'The table has more than '          # how the note starts that is only true until the table is counted
__all__ = ['Profiling', 'Busy', 'Target', 'STRUCTURE_ONLY']


@dataclass
class Target:
    """A table of the merged model and where its data can be read from."""
    table: object                       # app.model.Table (merged)
    source: dict | None = None          # the source row (id, kind, label, settings, model)
    origin: object | None = None        # the table's origin in that source
    source_table: object | None = None  # the table as that source's own snapshot has it
    reason: str | None = None           # why it cannot be profiled


class Profiling:
    def __init__(self, workspaces, jobs=None):
        self.ws = workspaces
        self.jobs = jobs or Jobs()
        self.store = ProfileStore(workspaces._db)
        self._show_personal = False     # memory only
        self._live = {}                 # (workspace id, table key) -> real values read while the switch is on
        self._rules_applied = set()     # workspaces whose saved statistics were checked against today's rules
        self._marks = threading.RLock() # held while marks are read and applied, and while one is changed
        self.on_mark = []               # callbacks (workspace id, table, column, category): other saved values to mask
        self.on_clear = []              # callbacks (workspace id): other saved results to delete
        self.on_switch = []             # callbacks (on): the "show personal values" switch changed
        workspaces.on_remove.append(self._removing)
        workspaces.on_change.append(self._model_changed)

    # --------------------------------------------------------------- switch
    @property
    def show_personal(self):
        return self._show_personal

    @show_personal.setter
    def show_personal(self, on):
        self._show_personal = on is True
        if not self._show_personal:
            self._live.clear()          # the real values leave memory with the switch
        for hook in self.on_switch:
            hook(self._show_personal)

    # ------------------------------------------------------------- targets
    def _removing(self, workspace_id, source_id=None):
        """A source or the whole workspace is about to be removed: stop the job that reads it."""
        self.jobs.stop_for(workspace_id, source_id)
        self._forget_live(workspace_id)

    def _model_changed(self, workspace_id, removed_source=None, deleted=False):
        """A source was removed or read again: drop the statistics of tables that are gone."""
        self._forget_live(workspace_id)
        if deleted:
            return
        try:
            if removed_source:
                self.store.drop_source(workspace_id, removed_source)
            keys = {t.key for t in self.ws.model(workspace_id).tables}
            for key in self.store.tables(workspace_id):
                if key not in keys:
                    self.store.drop_table(workspace_id, key)
        except NotFound:
            pass

    def _forget_live(self, workspace_id, table_key=None):
        for key in [k for k in self._live if k[0] == workspace_id and table_key in (None, k[1])]:
            self._live.pop(key, None)

    @staticmethod
    def can_profile(table):
        return any(o.kind in ('db', 'file') for o in table.origins)

    def targets(self, workspace_id, tables):
        """Where each of these merged tables can be read from. A live database is preferred to a file."""
        sources = {s['id']: s for s in self.ws.source_rows(workspace_id)}
        out = []
        for table in tables:
            target = Target(table, reason=STRUCTURE_ONLY)
            for kind in ('db', 'file'):
                origin = next((o for o in table.origins if o.kind == kind and o.source_id in sources), None)
                if origin is None:
                    continue
                source = sources[origin.source_id]
                found = self._source_table(source['model'], origin, table)
                if found is None:
                    target.reason = f'{table.id} was not found in {source["label"]}; read the source again.'
                    continue
                target = Target(table, source, origin, found)
                break
            out.append(target)
        return out

    @staticmethod
    def _source_table(model, origin, table):
        """The table in the source's own snapshot (its names are the ones to use in queries)."""
        if origin.table or origin.path:
            for candidate in model.tables:
                for o in candidate.origins:
                    if (o.kind, o.schema, o.table, o.path, o.sheet) == (
                            origin.kind, origin.schema, origin.table, origin.path, origin.sheet):
                        return candidate
        return model.find(table.id) or model.find(table.name)       # snapshots saved before session 2

    def database(self, workspace_id, source, cache=None, timeout_s=None):
        """A ReadOnlyDatabase for a database source (kept in `cache` for the length of a job)."""
        if cache is not None and source['id'] in cache:
            return cache[source['id']]
        profile = ConnectionProfile.from_dict(source['settings'])
        password = self.ws.password(workspace_id, source['id'])
        if profile.engine != 'sqlite' and not password:
            raise self.needs_password(source)
        db = ReadOnlyDatabase(profile, password, timeout_s=timeout_s)
        if cache is not None:
            cache[source['id']] = db
        return db

    @staticmethod
    def needs_password(source):
        exc = NeedsPassword(f'Enter the password for {source["label"]} to read its data.')
        exc.source = {'id': source['id'], 'label': source['label']}
        return exc

    def require_passwords(self, workspace_id, targets):
        """Ask for a missing database password before anything starts."""
        for target in targets:
            source = target.source
            if (source and source['kind'] == 'database' and source['settings'].get('engine') != 'sqlite'
                    and not self.ws.password(workspace_id, source['id'])):
                raise self.needs_password(source)

    @staticmethod
    def names(target):
        """(schema, table) exactly as the source's catalog has them."""
        origin = target.origin
        if origin.table:
            return origin.schema, origin.table
        return target.source_table.schema, target.source_table.name

    def database_table(self, workspace_id, target, cache=None, timeout_s=None):
        """(DatabaseData, [ColumnPlan]) for a table of a database source. The caller closes it."""
        db = self.database(workspace_id, target.source, cache, timeout_s)
        engine = db.profile.engine
        schema, name = self.names(target)
        columns = [q.ColumnPlan.of(c.name, c.types.get('db') or c.type, engine) for c in target.source_table.columns]
        if engine != 'sqlite':
            for column in columns:                      # a type the app does not know: never fetched
                if not column.type:
                    column.family = 'lob'
        return DatabaseData(db, schema, name, is_view=target.source_table.kind == 'view'), columns

    def file_table(self, target, **options):
        """An unopened FileData for a table of a files source."""
        origin, source = target.origin, target.source
        path, sheet = origin.path, origin.sheet
        if not path:                                    # snapshots saved before session 2: only the display text
            path, _, sheet = (origin.detail or '').partition(' · sheet ')
            sheet = sheet or None
        if not path:
            raise QueryFailed('the file of this table is not known; read the source again')
        return FileData(source_root(source['settings'].get('path') or '') / Path(path), sheet, origin.encoding, **options)

    # ------------------------------------------------------------ personal
    @staticmethod
    def personal_category(marks, table, column, detected=None):
        """Why a column is personal, or None: the user's mark first, then its name, then its values."""
        mark = marks.get((table.key, column.upper()))
        if mark is True:
            return 'user'
        if mark is False:
            return None
        return personal_by_name(column, table.name) or detected

    def is_personal(self, workspace_id, marks=None):
        """function(table, column name, stats or None) -> category or None, with the marks as they are now."""
        marks = self.store.marks(workspace_id) if marks is None else marks
        return lambda table, column, stats=None: self.personal_category(marks, table, column, (stats or {}).get('detected'))

    def mark(self, workspace_id, table_id, column, personal):
        """Mark a column personal (True), not personal (False), or back to automatic (None)."""
        table = self._table(workspace_id, table_id)
        found = table.column(column or '')
        if found is None:
            raise NotFound('Column not found.')
        if personal not in (True, False, None):
            raise SourceError('personal must be true, false or null.')
        with self._marks:                               # not while a table's statistics are being masked and saved
            self.store.set_mark(workspace_id, table.key, found.name.upper(), personal)
            saved = self.store.columns(workspace_id, table.key).get(table.key, {}).get(found.name.upper())
            category = self.personal_category(self.store.marks(workspace_id), table, found.name,
                                              (saved or {}).get('detected'))
            if category:
                self.store.mask_column(workspace_id, table.key, found.name.upper(), category)
            self._forget_live(workspace_id, table.key)
            for hook in self.on_mark:
                hook(workspace_id, table, found.name, category)
        return {'table': table.id, 'column': found.name, 'personal': bool(category), 'personal_marked': personal,
                'personal_reason': self._reason(category, personal),
                # masking cannot be undone: the values come back with the next profile
                'reprofile': bool(not category and saved and saved.get('masked'))}

    @staticmethod
    def _reason(category, mark=None):
        if not category:
            return 'Marked not personal by you' if mark is False else None
        if category == 'user':
            return 'Marked personal by you'
        return f'Looks like {CATEGORY_TEXT.get(category, category)}'

    def mask_saved(self, workspace_id):
        """Statistics saved under older rules: mask in the file what counts as personal now.

        personal_rules.json may have gained a category since a table was profiled (pay; or one the
        team added). What is shown is masked whatever is stored (_public); this makes what is stored
        say the same. Done once per workspace each time the app runs, when the workspace is first
        looked at. A column marked "not personal" by the user is left alone. Returns how many
        columns were masked.
        """
        if workspace_id in self._rules_applied:
            return 0
        self._rules_applied.add(workspace_id)
        saved = self.store.columns(workspace_id)
        tables = {t.key: t for t in self.ws.model(workspace_id).tables}
        done = 0
        with self._marks:                               # not while a table's statistics are being masked and saved
            marks = self.store.marks(workspace_id)
            for table_key, columns in saved.items():
                table = tables.get(table_key)
                for column in (table.columns if table else ()):
                    stats = columns.get(column.name.upper())
                    if not stats or stats.get('masked'):
                        continue
                    category = self.personal_category(marks, table, column.name, stats.get('detected'))
                    if category and self.store.mask_column(workspace_id, table_key, column.name.upper(), category, compact=False):
                        done += 1
                        for hook in self.on_mark:
                            hook(workspace_id, table, column.name, category)
        if done:
            self.store.compact()                        # the unmasked values must not linger in the file's free pages
        return done

    # ----------------------------------------------------------------- jobs
    def _table(self, workspace_id, table_id):
        table = self.ws.model(workspace_id).find(table_id)
        if table is None:
            raise NotFound('Table not found.')
        return table

    def running(self, workspace_id):
        return self.jobs.running(workspace_id)

    def start(self, workspace_id, tables='all', counts_only=False, passwords=None):
        """Start profiling in the background. tables: 'all' or a list of table ids. Returns the job."""
        model = self.ws.model(workspace_id)
        for source_id, password in (passwords or {}).items():
            self.ws.set_password(workspace_id, str(source_id), password)
        if tables in (None, 'all', '*'):
            wanted = list(model.tables)
        else:
            if isinstance(tables, str):
                tables = [tables]
            wanted = []
            for table_id in tables:
                table = model.find(str(table_id))
                if table is None:
                    raise NotFound(f'Table not found: {table_id}')
                if table not in wanted:
                    wanted.append(table)
        targets = [t for t in self.targets(workspace_id, wanted) if t.source is not None]
        if not targets:
            raise SourceError('None of these tables has data to profile: SQL files give the structure only. '
                              'Add a live database or CSV / Excel exports.')
        self.require_passwords(workspace_id, targets)
        job = self.jobs.start(workspace_id, 'profile', len(targets), lambda job: self._run(job, targets),
                              sources={t.source['id'] for t in targets}, phase='row counts',
                              counts_only=bool(counts_only))
        return job.to_dict()

    def job(self, job_id):
        return self.jobs.get(job_id)

    def cancel(self, job_id):
        return self.jobs.cancel(job_id)

    def wait(self, job_id, timeout=120):
        return self.jobs.wait(job_id, timeout)

    def _run(self, job, targets):
        cache, estimates = {}, Estimates()
        try:
            self._count_rows(job, targets, cache, estimates)
            if job.options.get('counts_only'):
                return
            job.phase, job.done = 'profiling', 0
            for target in targets:
                job.check()
                job.current = target.table.id
                try:
                    self._profile_one(job, target, cache, estimates)
                except Cancelled:
                    raise
                except TimedOut as exc:
                    self._not_profiled(job, target, 'timed_out', str(exc))
                except QueryFailed as exc:
                    self._not_profiled(job, target, 'timed_out' if exc.timeout else 'failed', str(exc))
                except (NeedsPassword, SourceError) as exc:
                    self._not_profiled(job, target, 'failed', str(exc))
                except ReadOnlyViolation as exc:
                    self._not_profiled(job, target, 'failed', f'Blocked by the read-only guard: {exc}')
                except Exception as exc:                # one table must not stop the others
                    self._not_profiled(job, target, 'failed', f'{type(exc).__name__}: {exc}')
                job.done += 1
        finally:
            for db in cache.values():
                db.dispose()

    def _not_profiled(self, job, target, status, message):
        """A table timed out or failed: say so in the job, note it with the table, and move on."""
        message = sanitize_error(message, limit=300)
        job.fail(target.table.id, message, timeout=status == 'timed_out')
        with self.jobs.saving(job):
            self.store.set_attempt(job.workspace_id, target.table.key, target.source['id'], status, message)

    def _count_rows(self, job, targets, cache, estimates):
        """The row-count step, for every table first: the engine's own estimates where it has them
        (ONE catalog statement per schema). A table without a usable estimate is not counted with
        COUNT(*): its one read tells how many rows it has, up to the read's limit. In a
        "row counts only" job such a table is counted no further than that limit."""
        ws_id, only_counts = job.workspace_id, job.options.get('counts_only')
        limit = config.SAMPLE_TARGET_ROWS
        by_source = {}
        for target in targets:
            if target.source['kind'] == 'database' or only_counts:
                by_source.setdefault(target.source['id'], []).append(target)
        for source_id, items in by_source.items():
            job.check()
            source = items[0].source
            if source['kind'] != 'database':            # a counts-only job: a file has to be read to be counted
                for target in items:
                    job.check()
                    job.current = target.table.id
                    try:
                        rows = self.file_table(target).count_rows()
                        with self.jobs.saving(job):
                            self.store.set_count(ws_id, target.table.key, source_id, rows, 'exact')
                    except (QueryFailed, SourceError) as exc:
                        job.fail(target.table.id, str(exc))
                    job.done += 1
                continue
            job.current = f'{source["label"]}: row counts'
            db = self.database(ws_id, source, cache)
            found = {}
            has_estimates = q.estimate_query(db.profile.engine, 'x') is not None
            if has_estimates:
                for schema in dict.fromkeys(self.names(t)[0] for t in items):
                    job.check()
                    try:
                        found[schema] = catalog_estimates(db, schema)
                    except ReadOnlyViolation:
                        raise
                    except Exception as exc:
                        job.notes.append(f'{source["label"]}: no row estimates for {schema} ({db.clean_error(exc)}).')
            for target in items:
                job.check()
                schema, name = self.names(target)
                rows, kind = found.get(schema, {}).get(name), 'estimate'
                size = getattr(found.get(schema), 'sizes', {}).get(name)
                if size is not None:
                    estimates.sizes[target.table.key] = size        # Oracle: decides between row and block sampling
                if rows is not None:
                    estimates[target.table.key] = rows
                elif not has_estimates:
                    # An engine whose catalog has no row estimates (SQLite): every table is counted, each under
                    # the statement timeout. One that does not finish in time is left at "more than".
                    job.current = target.table.id
                    rows, kind = self._count_exactly(job, db, target, schema, name, limit, estimates)
                elif only_counts:
                    job.current = target.table.id
                    data = DatabaseData(db, schema, name)
                    try:
                        rows, more = bounded_count(data, limit)
                        kind = 'more_than' if more else 'exact'
                    except QueryFailed as exc:
                        job.fail(target.table.id, str(exc), timeout=exc.timeout)
                    finally:
                        data.close()
                if rows is not None:
                    with self.jobs.saving(job):
                        self.store.set_count(ws_id, target.table.key, source_id, rows, kind)
                if only_counts:
                    job.done += 1

    def _count_exactly(self, job, db, target, schema, name, limit, estimates):
        """COUNT(*) of one table, under the statement timeout: (rows, 'exact'). When it does not finish in
        time the table stays at "more than `limit`" - (limit, 'more_than') in a "row counts only" job, which
        counts no further than that; (None, None) in a profile, whose one read will say - and the job, the
        table's page and the report say that the count timed out."""
        table = target.table
        data = DatabaseData(db, schema, name)
        try:
            rows = exact_count(data)
            estimates.exact[table.key] = rows
            return rows, 'exact'
        except QueryFailed as exc:
            if not exc.timeout:
                job.fail(table.id, str(exc))
                return None, None
            estimates.count_notes[table.key] = f'an exact count did not finish within {db.timeout_s} s.'
            # (no quotation marks in it: what stands in quotes in a job's error is taken for data and blanked)
            job.fail(table.id, f'the exact count did not finish within {db.timeout_s} s: the table is listed with '
                               f'more than {limit:,} rows', timeout=True)
            if not job.options.get('counts_only'):
                return None, None
            data.close()
            data = DatabaseData(db, schema, name)
            try:
                rows, more = bounded_count(data, limit)
                return rows, 'more_than' if more else 'exact'
            except QueryFailed:
                return None, None
        finally:
            data.close()

    def _profile_one(self, job, target, cache, estimates):
        """One table: the read, the statistics in memory, then masking and saving in one step."""
        ws_id, table = job.workspace_id, target.table
        check = budget_check(config.TABLE_BUDGET_S, also=job.check)
        started = time.monotonic()
        database = target.source['kind'] == 'database'
        if database:
            data, plans = self.database_table(ws_id, target, cache)
            try:
                copy = read_table(data, plans, estimates.get(table.key), check,
                                  size=getattr(estimates, 'sizes', {}).get(table.key), exact=estimates.exact.get(table.key),
                                  count_note=estimates.count_notes.get(table.key))
            finally:
                data.close()
        else:
            copy = self.file_table(target).open(check)
            plans = copy.plans
        try:
            found = profile_copy(copy, check)
            summary = table_summary(copy, started, database)
        finally:
            copy.close()
        columns = {source.name: found[plan.name] for source, plan in zip(plans, copy.plans)}
        summary['source_kind'], summary['source_label'] = target.source['kind'], target.source['label']
        with self.jobs.saving(job):                     # not for a job that was cancelled or whose source is gone
            with self._marks:
                marks = self.store.marks(ws_id)         # the marks as they are NOW, not as they were at the start
                mask_result(columns, {name: self.personal_category(marks, table, name, st.get('detected'))
                                      for name, st in columns.items()})
                self.store.save_profile(ws_id, table.key, target.source['id'], summary,
                                        [(plan.name, columns[plan.name]) for plan in plans])
        self._forget_live(ws_id, table.key)
        return {'table': summary, 'columns': columns}

    # -------------------------------------------------------------- reading
    def row_counts(self, workspace_id):
        """{table key: (rows, 'estimate' / 'exact' / 'more_than')} for the table list."""
        return {key: (row['row_count'], row['row_count_kind']) for key, row in self.store.tables(workspace_id).items()
                if row['row_count'] is not None}

    @staticmethod
    def _status(row):
        """profiled / timed_out / failed / None for a row of ProfileStore.tables()."""
        attempt = (row or {}).get('attempt')
        if attempt:
            return attempt.get('status'), attempt.get('message')
        return ('profiled' if (row or {}).get('profiled_at') else None), None

    @staticmethod
    def notes(row):
        """The notes of a table's last profile, as far as they are still true.

        "The table has more than 100,000 rows; how many is not known ..." is only said while that is all
        that is known. Once the rows are counted - "Exact count", or a count the profile itself was
        given - it is dropped: the page and the report would otherwise say "100,000 rows of 2,000,373"
        and "how many is not known" one under the other. (A profile saved before 13 Oct 2026 has that
        sentence among its other notes; it is taken out of those the same way.)
        """
        profile = (row or {}).get('profile') or {}
        notes = [n for n in profile.get('notes') or [] if not str(n).startswith(MORE_THAN)]
        if (row or {}).get('row_count_kind') == 'more_than':
            said = profile.get('count_note') or next((n for n in profile.get('notes') or [] if str(n).startswith(MORE_THAN)), None)
            if said:
                notes.insert(0, said)
        return notes

    def overview(self, workspace_id):
        """Every table with its row count and whether it has been profiled; and the current job."""
        model = self.ws.model(workspace_id)
        self.mask_saved(workspace_id)                   # once per run: what older rules left unmasked in the file
        saved = self.store.tables(workspace_id)
        tables = []
        for table in model.tables:
            row = saved.get(table.key) or {}
            profile = row.get('profile') or {}
            able = self.can_profile(table)
            status, message = self._status(row)
            tables.append({'id': table.id, 'row_count': row.get('row_count'), 'row_count_kind': row.get('row_count_kind'),
                           'counted_at': row.get('counted_at'), 'profiled_at': row.get('profiled_at'),
                           'status': status, 'status_message': message,
                           'sampled': profile.get('sampled'), 'rows_profiled': profile.get('rows_profiled'),
                           'sample_method': profile.get('sample_method'),
                           'can_profile': able, 'reason': None if able else STRUCTURE_ONLY})
        job = self.running(workspace_id) or self.jobs.latest(workspace_id)
        return {'job': job.to_dict() if job else None, 'tables': tables, 'show_personal': self.show_personal,
                'settings': {'sample_threshold_rows': config.SAMPLE_TARGET_ROWS,
                             'sample_target_rows': config.SAMPLE_TARGET_ROWS,
                             'file_sample_threshold_rows': config.SAMPLE_THRESHOLD_ROWS,
                             'table_budget_s': config.TABLE_BUDGET_S,
                             'statement_timeout_s': config.STATEMENT_TIMEOUT_S}}

    def table_profile(self, workspace_id, table_id, sample=True):
        """One table: what is saved for it, and (sample=True) its first rows read live."""
        table = self._table(workspace_id, table_id)
        row = self.store.tables(workspace_id).get(table.key) or {}
        saved = self.store.columns(workspace_id, table.key).get(table.key, {})
        marks = self.store.marks(workspace_id)
        profile = row.get('profile') or {}
        (target,) = self.targets(workspace_id, [table])
        columns, personal = [], {}
        for c in table.columns:
            stats = saved.get(c.name.upper())
            mark = marks.get((table.key, c.name.upper()))
            category = self.personal_category(marks, table, c.name, (stats or {}).get('detected'))
            personal[c.name.upper()] = category
            columns.append({'name': c.name, 'type': c.type, 'pk': c.pk, 'fk': c.fk, 'pk_inferred': c.pk_inferred,
                            'fk_inferred': c.fk_inferred, 'personal': bool(category), 'personal_marked': mark,
                            'personal_reason': self._reason(category, mark), 'stats': self._public(stats, category)})
        status, message = self._status(row)
        out = {
            'table': table.id, 'can_profile': target.source is not None, 'reason': target.reason,
            'profiled': bool(row.get('profiled_at')), 'profiled_at': row.get('profiled_at'),
            'status': status, 'status_message': message,
            'row_count': row.get('row_count'), 'row_count_kind': row.get('row_count_kind'),
            'counted_at': row.get('counted_at'),
            'source': None if target.source is None else {'id': target.source['id'], 'label': target.source['label'],
                                                          'kind': target.source['kind']},
            'sampled': bool(profile.get('sampled')), 'sample_method': profile.get('sample_method'),
            'sample_random': profile.get('sample_random'), 'rows_profiled': profile.get('rows_profiled'),
            'notes': self.notes(row), 'elapsed_s': profile.get('elapsed_s'), 'queries': profile.get('queries'),
            'columns': columns, 'show_personal': self.show_personal, 'sample': None,
        }
        if target.source is None:
            return out
        wanted = [c['name'] for c in columns if c['personal'] and c['stats']]
        live = self.show_personal and bool(wanted)
        if not (sample or live):
            return out
        try:
            if live:                                    # the switch is on: the real values, read once and kept in memory
                fresh = self._live_values(workspace_id, target, row, saved, wanted)
                for c in columns:
                    if c['name'].upper() in fresh and c['stats']:
                        c['stats'] = {**c['stats'], **fresh[c['name'].upper()], 'masked': False, 'live': True}
                        c['stats'].pop('top_hidden', None)      # pay: its top values are shown like the others'
            if sample:
                out['sample'] = self._sample(workspace_id, target, table, marks, saved)
        except NeedsPassword as exc:
            out['sample'] = {'error': str(exc), 'needs_password': True, 'source': getattr(exc, 'source', None)}
        except (QueryFailed, SourceError, TimedOut) as exc:
            out['sample'] = {'error': str(exc)}
        except ReadOnlyViolation as exc:
            out['sample'] = {'error': f'Blocked by the read-only guard: {exc}'}
        return out

    def _live_values(self, workspace_id, target, row, saved, wanted):
        """{COLUMN: {min, max, top}} unmasked, for the "show personal values" switch.

        ONE read of the same sample the saved profile describes, the personal columns only, into
        memory; the numbers come from that copy. The result is kept for the session, so the next
        look at the table costs its source nothing.
        """
        table, key = target.table, (workspace_id, target.table.key)
        signature = (row.get('profiled_at'), tuple(sorted(c.upper() for c in wanted)))
        cached = self._live.get(key)
        if cached and cached.get('signature') == signature and 'values' in cached:
            return cached['values']
        profile = row.get('profile') or {}
        check = budget_check(config.TABLE_BUDGET_S)
        database = target.source['kind'] == 'database'
        if database:
            data, plans = self.database_table(workspace_id, target)
            try:
                names = {p.name for p in plans if p.name.upper() in signature[1]}
                copy = read_table(data, plans, row.get('row_count') if row.get('row_count_kind') == 'estimate' else None,
                                  check, only=names, plan=profile.get('sample_plan'))
            finally:
                data.close()
                data.db.dispose()
            source_names = [p.name for p in copy.source_plans]
        else:
            copy = self.file_table(target, only=wanted).open(check)
            source_names = [p.name for p in copy.plans]
        try:
            unique = {plan.name for plan, name in zip(copy.plans, source_names)
                      if (saved.get(name.upper()) or {}).get('unique')}
            found = live_values(copy, no_top=unique)
        finally:
            copy.close()
        values = {name.upper(): found[plan.name] for plan, name in zip(copy.plans, source_names)}
        if self.show_personal:                          # kept only while the switch is on
            self._live[key] = {**(cached if cached and cached.get('signature') == signature else {}),
                               'signature': signature, 'values': values}
        return values

    def sample(self, workspace_id, table_id):
        """Only the sample rows of a table (read live, masked unless the switch is on)."""
        return self.table_profile(workspace_id, table_id, sample=True)['sample']

    def _sample(self, workspace_id, target, table, marks, saved):
        """The first rows of a table: one small read. With the switch on they are kept in memory
        with the table's other real values, and are not read a second time."""
        key = (workspace_id, table.key)
        show = self.show_personal
        cached = self._live.get(key) if show else None
        if cached and 'sample' in cached:
            return cached['sample']
        data, db = None, None
        try:
            if target.source['kind'] == 'database':
                data, plans = self.database_table(workspace_id, target)
                db = data.db
            else:
                data = self.file_table(target, limit=config.PROFILE_SAMPLE_ROWS).open()
                plans = data.plans
            names, rows = sample_rows(data, plans)
        finally:
            if data is not None:
                data.close()
            if db is not None:
                db.dispose()
        families = {p.name: p.family for p in plans}
        hidden = []
        for i, name in enumerate(names):
            detected = (saved.get(name.upper()) or {}).get('detected') or personal_by_values(
                [r[i] for r in rows], name, families.get(name))
            hidden.append(self.personal_category(marks, table, name, detected))
        shown = [[jsonable(v) if show or not category else mask(v, category) for v, category in zip(r, hidden)]
                 for r in rows]
        left_out = [p.name for p in plans if p.family == 'lob']
        out = {'columns': names, 'rows': shown, 'personal': [n for n, h in zip(names, hidden) if h],
               'masked': not show and any(hidden), 'left_out': left_out, 'error': None}
        if show and self.show_personal:
            entry = self._live.setdefault(key, {})
            entry['sample'] = out
        return out

    def _public(self, stats, category):
        """Saved statistics as they may be shown. Safety net: a personal column is never shown unmasked
        from storage, even if it was stored before it became personal."""
        if not stats:
            return None
        out = dict(stats)
        out.pop('personal', None)
        if category and not out.get('masked'):
            mask_stats(out, category)
            out['masked'] = True
        elif not category and out.get('masked'):
            out['stale_mask'] = True                    # unmarked since: the values return with the next profile
        return out

    def masked_columns(self, workspace_id, table):
        """{COLUMN: stats as they may be shown, always masked} for a table - what reports use.
        Never reads the "show personal values" switch."""
        saved = self.store.columns(workspace_id, table.key).get(table.key, {})
        decide = self.is_personal(workspace_id)
        return {key: self._public(stats, decide(table, key, stats)) for key, stats in saved.items()}

    def exact_count(self, workspace_id, table_id):
        """COUNT(*) of one table, under the statement timeout. Saved as its exact row count.
        The only full count the app runs, and only on request."""
        table = self._table(workspace_id, table_id)
        (target,) = self.targets(workspace_id, [table])
        if target.source is None:
            raise SourceError(f'{table.id}: {target.reason}.')
        if self.running(workspace_id) is not None:
            raise Busy(self.running(workspace_id).to_dict())
        data = None
        started = time.monotonic()
        try:
            if target.source['kind'] == 'database':
                data, _ = self.database_table(workspace_id, target)
                rows = exact_count(data)
            else:
                rows = self.file_table(target).count_rows()
        except QueryFailed as exc:
            raise SourceError(f'{table.id}: the exact count {"timed out" if exc.timeout else "failed"} ({exc}).') from None
        finally:
            if data is not None:
                data.close()
                data.db.dispose()
        self.store.set_count(workspace_id, table.key, target.source['id'], rows, 'exact')
        return {'table': table.id, 'row_count': rows, 'row_count_kind': 'exact',
                'elapsed_s': round(time.monotonic() - started, 2)}

    def clear(self, workspace_id):
        """Delete the saved row counts and statistics of a workspace (the personal marks stay)."""
        self.ws.get_workspace(workspace_id)
        if self.running(workspace_id) is not None:
            raise Busy(self.running(workspace_id).to_dict())
        self._forget_live(workspace_id)
        for hook in self.on_clear:
            hook(workspace_id)
        return {'cleared_tables': self.store.clear(workspace_id)}

    def quality(self, workspace_id):
        model = self.ws.model(workspace_id)
        saved = self.store.tables(workspace_id)
        decide = self.is_personal(workspace_id)         # the marks as they are now: a finding never quotes a marked column
        findings = workspace_findings(model, saved, self.store.columns(workspace_id),
                                      personal=lambda table, column, stats: bool(decide(table, column, stats)))
        profiled = sum(1 for t in model.tables if (saved.get(t.key) or {}).get('profiled_at'))
        return {'findings': findings,
                'summary': {s: sum(1 for f in findings if f['severity'] == s) for s in SEVERITIES},
                'rules': RULES, 'severities': list(SEVERITIES),
                'tables_total': len(model.tables), 'tables_profiled': profiled,
                'tables_with_data': sum(1 for t in model.tables if self.can_profile(t)),
                'settings': {'high_null_share': config.HIGH_NULL_SHARE, 'mixed_type_share': config.MIXED_TYPE_SHARE}}
