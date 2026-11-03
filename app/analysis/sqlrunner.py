"""The SQL tab: run one read-only query against a source of the workspace.

    SqlRunner.info()   which sources can be queried, the query history, the limits
    SqlRunner.run()    check the statement, run it, mask the result
    SqlRunner.csv()    the last results as CSV, exactly as they were shown

A statement is checked by the strict guard first (app/sqlguard.py, through
security.check_read_only_sql(strict=True)); only what passes is sent, and what is sent is the
checked text with its comments removed.

  a database   through ReadOnlyDatabase.connect(): the read-only session, the statement timeout,
               no commit, rollback and close afterwards. SQL_MAX_ROWS + 1 rows are fetched and no
               more: the extra row only tells that more exist.
  files        through DuckDB in memory. Every table of the source is a view: UTF-8 CSV files are
               read where they are, typed as the source's structure says (TRY_CAST, so a bad row
               gives an empty value, not an error); Excel sheets and text in another encoding are
               loaded when the statement names them. Before the statement runs, DuckDB is cut off
               from every file but the source's own and its settings are locked, so read_csv,
               glob, COPY, ATTACH, INSTALL and LOAD could reach nothing even past the guard.

Personal columns of a result are masked unless "show personal values" is on. A result column is
judged by the table columns it comes from (lineage.py follows aliases, subqueries, WITH queries,
UNION branches and SELECT *), the way the Profile tab judges them:

  a column as it is     personal when the Profile tab masks that column of that table - by its
                        name together with its table's, by what profiling found in its values, or
                        by the user's mark on that table.column, which wins either way
  built from columns    (functions, CASE, arithmetic, aggregates) personal when one of them is
  COUNT(...) alone      never: a number of rows
  not resolved for sure (a table the source does not know, a name two tables have ...) judged as
                        before this was known: by the result column's own name, by a "personal"
                        mark on any column of that name, and by the names the statement builds it
                        from. Some names are personal only in their table (gross_pay in a table
                        of staff, latitude in a table of addresses): such a name counts here when
                        any table of the workspace hides a column called that. All of this masks
                        too much rather than too little.

A column that is not personal by these rules is still masked when its values look personal - but
not a column the user marked "not personal".

Only the text of a query is kept (the last SQL_HISTORY per workspace, in data/explorer.db).
Results live in memory, a few at a time, for the CSV export; they are never written to disk.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .. import config
from ..security import ReadOnlyViolation, check_read_only_sql, sanitize_error
from ..sqlguard import Known
from ..sources import SourceError, duck
from ..sources.database import ConnectionProfile, ReadOnlyDatabase
from ..sources.files import CSV_EXT, EXCEL_EXT, source_root
from ..workspace import NeedsPassword, NotFound, new_id
from ..profiling import queries as q
from ..profiling.data import TIMEOUT_WORDS, FileData
from ..profiling import personal
from ..profiling.personal import CATEGORY_TEXT, mask, personal_by_name, personal_by_values
from ..profiling.profiler import jsonable
from .lineage import only_counts as _only_counts, origins
from .store import AnalysisStore

KEEP_RESULTS = 8
CELL_MAX_CHARS = 500
FORMULA_START = ('=', '+', '-', '@', '\t', '\r')
SELECT_END = {'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'FETCH', 'UNION', 'INTERSECT', 'EXCEPT',
              'MINUS', 'WINDOW', 'QUALIFY', 'CONNECT', 'START', 'FOR', 'INTO'}
NOT_A_NAME = {'AS', 'DISTINCT', 'ALL', 'TOP', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'AND', 'OR', 'NOT', 'NULL', 'IS', 'IN',
              'LIKE', 'BETWEEN', 'OVER', 'PARTITION', 'BY', 'ORDER', 'ASC', 'DESC', 'FILTER', 'WHERE', 'CAST', 'SELECT',
              'FROM', 'EXISTS', 'ROWS', 'RANGE', 'UNBOUNDED', 'PRECEDING', 'FOLLOWING', 'CURRENT', 'ROW', 'WITHIN', 'GROUP',
              'NULLS', 'FIRST', 'LAST', 'TRUE', 'FALSE', 'DATE', 'TIMESTAMP', 'INTERVAL', 'ESCAPE', 'PERCENT', 'WITH', 'TIES'}


class Busy(SourceError):
    """A query is already running in this workspace."""


# ------------------------------------------------------- personal columns ----

def _is_op(tok, text):
    return tok is not None and tok.kind == 'op' and tok.text == text


def select_items(tokens):
    """Every select list of a statement, at any depth: [(depth, [items])] where an item is
    {'tokens': [...], 'alias': NAME or None, 'star': bool}. The first entry is the outermost list."""
    out, depth = [], 0
    for i, t in enumerate(tokens):
        if t.kind == 'op' and t.text == '(':
            depth += 1
        elif t.kind == 'op' and t.text == ')':
            depth -= 1
        elif t.kind == 'ident' and t.up == 'SELECT':
            items, current, d, j = [], [], depth, i + 1
            while j < len(tokens):
                x = tokens[j]
                if x.kind == 'op' and x.text == '(':
                    d += 1
                elif x.kind == 'op' and x.text == ')':
                    d -= 1
                    if d < depth:
                        break
                elif d == depth and x.kind == 'ident' and x.up in SELECT_END:
                    break
                elif d == depth and x.kind == 'op' and x.text == ',':
                    items.append(current)
                    current = []
                    j += 1
                    continue
                current.append(x)
                j += 1
            if current:
                items.append(current)
            out.append((depth, [_item(item) for item in items]))
    return out


def _item(tokens):
    alias, body = None, tokens
    if len(tokens) >= 2 and tokens[-1].kind in ('ident', 'qident') and not (tokens[-1].kind == 'ident' and tokens[-1].up in NOT_A_NAME):
        prev = tokens[-2]
        if prev.kind == 'ident' and prev.up == 'AS':
            alias, body = tokens[-1].up, tokens[:-2]
        elif not _is_op(prev, '.') and (prev.kind in ('number', 'string', 'qident') or _is_op(prev, ')')
                                        or prev.kind == 'ident' and (prev.up == 'END' or prev.up not in NOT_A_NAME)):
            alias, body = tokens[-1].up, tokens[:-1]
    if alias is None and tokens and tokens[-1].kind in ('ident', 'qident'):
        alias = tokens[-1].up                       # a plain column: its own name is the result's name
    star = _is_op(tokens[-1], '*') and (len(tokens) == 1 or len(tokens) == 3 and _is_op(tokens[1], '.')) if tokens else False
    return {'tokens': body, 'alias': alias, 'star': star}


def derived_columns(tokens, is_personal):
    """Which result columns a statement builds from personal columns, whatever it calls them.
    By names alone, without their tables: for the columns that lineage.origins() could not resolve.

    is_personal(NAME) -> category or None for a column name. Returns (per position of the outermost
    select list: category or None - or None when the list holds a *; {ALIAS: category} for every alias
    that carries a personal column anywhere in the statement)."""
    lists = select_items(tokens)
    tainted = {}

    def category(item):
        if _only_counts(item['tokens']):
            return None
        for i, t in enumerate(item['tokens']):
            if t.kind not in ('ident', 'qident') or t.kind == 'ident' and t.up in NOT_A_NAME:
                continue
            nxt = item['tokens'][i + 1] if i + 1 < len(item['tokens']) else None
            if _is_op(nxt, '(') or _is_op(nxt, '.'):
                continue                            # a function name, or the table in front of a column
            found = tainted.get(t.up) or is_personal(t.text)
            if found:
                return found
        return None

    for _ in range(6):                              # an alias of an alias of ... : until nothing changes
        before = dict(tainted)
        for _, items in lists:
            for item in items:
                found = category(item)
                if found and item['alias']:
                    tainted.setdefault(item['alias'], found)
        if tainted == before:
            break
    branches = _result_lists(lists)
    outer = None
    if branches:                                    # personal in one branch of a UNION: personal in the result
        outer = [next((found for found in (category(items[i]) for items in branches) if found), None)
                 for i in range(len(branches[0]))]
    return outer, tainted


def _result_lists(lists):
    """The select lists that make the statement's own result: one, or one per branch of a UNION /
    INTERSECT / EXCEPT - not those of subqueries and WITH queries, which lie deeper in brackets.
    None when one of them holds a * or they differ in length: the positions are not known then."""
    if not lists:
        return None
    top = min(depth for depth, _ in lists)
    branches = [items for depth, items in lists if depth == top]
    if any(item['star'] for items in branches for item in items) or len({len(items) for items in branches}) != 1:
        return None
    return branches


def count_columns(tokens):
    """The positions of the result that are a COUNT(...) and nothing else, in every branch of the
    statement - or an empty set when the positions are not known (a * in the select list).
    Such a column is a number of rows whatever it is called: SQLite and Oracle name an unnamed
    COUNT(salary) after its text, and that name must not get it masked."""
    branches = _result_lists(select_items(tokens))
    if not branches:
        return set()
    return {i for i in range(len(branches[0])) if all(_only_counts(items[i]['tokens']) for items in branches)}


def _strength(category):
    """How much a category hides, strongest first: nothing left at all (pay), every character, the usual mask."""
    rules = personal.RULES
    return 0 if category in rules.length_hidden_categories else 1 if category in rules.fully_masked_categories else 2


# ------------------------------------------------------------------ runner ----

class SqlRunner:
    def __init__(self, workspaces, profiling):
        self.ws, self.prof = workspaces, profiling
        self.store = AnalysisStore(workspaces._db)
        self._results = OrderedDict()               # result id -> what was shown. Memory only.
        self._running = set()                       # workspace ids with a query under way
        self._lock = threading.Lock()
        workspaces.on_change.append(lambda workspace_id, *_: self._forget(workspace_id))
        profiling.on_switch.append(self._switched)

    def _forget(self, workspace_id=None, unmasked_only=False):
        with self._lock:
            for key in [k for k, r in self._results.items() if (workspace_id in (None, r['workspace_id']))
                        and (not unmasked_only or r['unmasked'])]:
                del self._results[key]

    def _switched(self, on):
        if not on:
            self._forget(unmasked_only=True)        # results that were shown unmasked leave memory with the switch

    # ---------------------------------------------------------------- sources
    @staticmethod
    def _dialect(source):
        if source['kind'] == 'database':
            return source['settings'].get('engine')
        return q.DUCKDB if source['kind'] == 'files' else None

    def sources(self, workspace_id):
        """The sources a query can run against: databases and file sources (SQL files hold no data)."""
        out = []
        for s in self.ws.source_rows(workspace_id):
            dialect = self._dialect(s)
            if dialect is None:
                continue
            needs = s['kind'] == 'database' and dialect != 'sqlite' and not self.ws.password(workspace_id, s['id'])
            tables = sorted(t.id for t in s['model'].tables if any(o.kind in ('db', 'file') for o in t.origins))
            out.append({'id': s['id'], 'label': s['label'], 'kind': s['kind'], 'dialect': dialect,
                        'engine': 'DuckDB (files)' if dialect == q.DUCKDB else dialect, 'needs_password': needs,
                        'tables': tables, 'blocked': self.blocked(s)})
        return out

    @classmethod
    def blocked(cls, source):
        """Why no query can run on this source yet, or None. An Oracle source needs the names of what its
        database can call - without them a name cannot be told from a function call, and there is no
        weaker check to fall back to."""
        if cls._dialect(source) != 'oracle':
            return None
        catalog = source['model'].catalog or {}
        if catalog.get('callable') is not None:
            return None
        why = (f'they could not be read from the catalog ({catalog["error"]})' if catalog.get('error')
               else 'this source was added before the app read them')
        return ('The SQL tab cannot run queries on this Oracle source yet. It needs the names of the functions, '
                f'procedures, packages, types and schemas of the database, and {why}. On Oracle a function is '
                'called without brackets and can change data, so without those names no statement can be checked. '
                'Refresh the source to read them (ALL_OBJECTS, ALL_SYNONYMS and ALL_USERS, one statement).')

    def info(self, workspace_id):
        self.ws.get_workspace(workspace_id)
        return {'sources': self.sources(workspace_id), 'history': self.store.queries(workspace_id),
                'max_rows': config.SQL_MAX_ROWS, 'timeout_s': config.SQL_TIMEOUT_S, 'max_chars': config.SQL_MAX_CHARS,
                'history_size': config.SQL_HISTORY, 'show_personal': self.prof.show_personal}

    def clear_history(self, workspace_id):
        self.ws.get_workspace(workspace_id)
        return {'cleared': self.store.clear_queries(workspace_id)}

    def _source(self, workspace_id, source_id):
        rows = self.ws.source_rows(workspace_id)
        source = next((s for s in rows if s['id'] == source_id), None)
        if source is None:
            runnable = [s for s in rows if self._dialect(s)]
            if source_id in (None, '') and len(runnable) == 1:
                return runnable[0]
            raise NotFound('Choose the source to run the query against.')
        if self._dialect(source) is None:
            raise SourceError('SQL files hold the structure only: there is no data to query. Choose a database or '
                              'a files source.')
        return source

    @staticmethod
    def _names(source):
        """What a source knows, for the guard's check of names (Oracle and PostgreSQL): its tables with
        their columns and, on Oracle, what the database can call."""
        return Known.of(source['model'], source['model'].catalog)

    # -------------------------------------------------------------------- run
    def run(self, workspace_id, source_id, sql, password=None):
        """Run one statement. Returns the result as it is shown: columns, at most SQL_MAX_ROWS rows,
        whether more exist, which columns are personal. Raises ReadOnlyViolation for a statement
        the guard refuses, SourceError for one the source refuses."""
        self.ws.get_workspace(workspace_id)
        source = self._source(workspace_id, source_id)
        dialect = self._dialect(source)
        text = (sql or '').strip()
        if password and source['kind'] == 'database':
            self.ws.set_password(workspace_id, source['id'], password)
        with self._lock:
            if workspace_id in self._running:
                raise Busy('A query is already running in this workspace. Wait for it to finish or to time out.')
            self._running.add(workspace_id)
        started = time.monotonic()
        try:
            if self.blocked(source):
                raise SourceError(self.blocked(source))
            try:
                checked = check_read_only_sql(text, dialect, strict=True, names=self._names(source))
            except ReadOnlyViolation:
                if text:
                    self.store.add_query(workspace_id, source['id'], text[:config.SQL_MAX_CHARS], 'blocked', config.SQL_HISTORY)
                raise
            try:
                if source['kind'] == 'database':
                    columns, rows = self._run_database(workspace_id, source, checked)
                else:
                    columns, rows = self._run_files(source, checked)
            except (ReadOnlyViolation, NeedsPassword):
                raise
            except Exception:
                self.store.add_query(workspace_id, source['id'], text, 'failed', config.SQL_HISTORY)
                raise
            self.store.add_query(workspace_id, source['id'], text, 'ok', config.SQL_HISTORY)
        finally:
            with self._lock:
                self._running.discard(workspace_id)
        more = len(rows) > config.SQL_MAX_ROWS
        rows = rows[:config.SQL_MAX_ROWS]
        return self._present(workspace_id, source, checked, columns, rows, more, time.monotonic() - started)

    def _run_database(self, workspace_id, source, checked):
        profile = ConnectionProfile.from_dict(source['settings'])
        secret = self.ws.password(workspace_id, source['id'])
        if profile.engine != 'sqlite' and not secret:
            raise self.prof.needs_password(source)
        db = ReadOnlyDatabase(profile, secret, timeout_s=config.SQL_TIMEOUT_S)
        db.approve(checked.sql)                     # the strict guard passed exactly this text
        started = time.monotonic()
        try:
            with db.connect() as conn:
                # no_parameters: a % in the statement (LIKE 'a%') is not a placeholder
                result = conn.execution_options(stream_results=True, no_parameters=True).exec_driver_sql(checked.sql)
                columns = [str(c) for c in result.keys()]
                rows = [tuple(row) for row in result.fetchmany(config.SQL_MAX_ROWS + 1)]
                if profile.engine == 'mysql' and len(rows) > config.SQL_MAX_ROWS:
                    # PyMySQL reads the rest of a result before it closes a cursor - two million rows, for
                    # the sake of 1,000. Drop the connection instead; the cursor has nothing left to finish.
                    cursor = getattr(result, 'cursor', None)
                    pending = getattr(cursor, '_result', None)
                    conn.invalidate()
                    if pending is not None:
                        pending.unbuffered_active = False
                    if cursor is not None:
                        cursor.connection = None
                try:
                    result.close()
                except Exception:
                    pass
                return columns, rows
        except (ReadOnlyViolation, SourceError):
            raise
        except Exception as exc:
            elapsed = time.monotonic() - started
            if elapsed >= db.timeout_s - 0.5 or any(w in str(exc).lower() for w in TIMEOUT_WORDS):
                raise SourceError(f'The query timed out after {db.timeout_s} s and was stopped.') from None
            raise SourceError(db.clean_error(exc, keep=checked.names())) from None
        finally:
            db.dispose()

    def _run_files(self, source, checked):
        con = duck.open_memory()
        timer = None
        try:
            self._register_files(con, source, checked)
            duck.lock_down(con)                     # from here on: the source's own files and nothing else
            timer = threading.Timer(config.SQL_TIMEOUT_S, con.interrupt)
            timer.daemon = True
            timer.start()
            cursor = con.execute(checked.sql)
            columns = [d[0] for d in cursor.description or []]
            return columns, [tuple(row) for row in cursor.fetchmany(config.SQL_MAX_ROWS + 1)]
        except SourceError:
            raise
        except Exception as exc:
            if type(exc).__name__ == 'InterruptException':
                raise SourceError(f'The query timed out after {config.SQL_TIMEOUT_S} s and was stopped.') from None
            known = checked.names() + [t.name for t in source['model'].tables]
            raise SourceError(sanitize_error(str(exc), keep=known, limit=400)) from None
        finally:
            if timer is not None:
                timer.cancel()
            try:
                con.close()
            except Exception:
                pass

    @staticmethod
    def _register_files(con, source, checked):
        """Every table of a files source as a DuckDB view named like the table."""
        root = source_root(source['settings'].get('path') or '')
        used = {t.up for t in checked.tokens if t.kind in ('ident', 'qident')}
        allowed, schemas, loaded = [], set(), 0
        for table in source['model'].tables:
            origin = next((o for o in table.origins if o.kind == 'file' and o.path), None)
            if origin is None:
                continue
            path = root / Path(origin.path)
            if not path.is_file():
                continue
            if table.schema and table.schema.upper() not in schemas:
                con.execute(f'CREATE SCHEMA IF NOT EXISTS {q.quote_duck(table.schema)}')
                schemas.add(table.schema.upper())
            name = (q.quote_duck(table.schema) + '.' if table.schema else '') + q.quote_duck(table.name)
            ext = path.suffix.lower()
            if ext in CSV_EXT and not origin.encoding:
                # read where it is, when the statement asks for it; typed as the source's structure says
                columns = ', '.join(
                    q.quote_duck(c.name) if (c.type or 'VARCHAR').upper() == 'VARCHAR'
                    else f'TRY_CAST({q.quote_duck(c.name)} AS {_plain_type(c.type)}) AS {q.quote_duck(c.name)}'
                    for c in table.columns)
                file = path.as_posix().replace("'", "''")
                con.execute(f"CREATE VIEW {name} AS SELECT {columns} FROM read_csv('{file}', all_varchar=true, "
                            f'null_padding=true, strict_mode=false)')
                allowed.append(file)
            elif table.name.upper() in used and (ext in EXCEL_EXT or ext in CSV_EXT):
                reader = FileData(path, origin.sheet, origin.encoding)
                rows = reader._excel_rows() if ext in EXCEL_EXT else reader._text_rows()
                loaded += 1
                names, _ = duck.load_text_rows(con, f'__dx_raw{loaded}', rows)
                if not names:
                    continue
                duck.typed_table(con, f'__dx_raw{loaded}', f'__dx_table{loaded}', names)
                con.execute(f'CREATE VIEW {name} AS SELECT * FROM {q.quote_duck(f"__dx_table{loaded}")}')
        if allowed:
            con.execute('SET allowed_paths = [' + ', '.join(f"'{a}'" for a in allowed) + ']')

    # -------------------------------------------------------------- presenting
    def _judge(self, workspace_id, source, marks):
        """function(Origin) -> (category or None, reason, settled) for a result column whose table
        columns are known: judged as the Profile tab judges those columns. settled: its values are
        not looked at any more. The function returns None for a column of a table the workspace's
        model does not have - that one is not resolved after all."""
        model = self.ws.model(workspace_id)
        tables, saved = {}, {}

        def merged(table):                              # the source's own table -> the workspace's table
            if id(table) not in tables:
                mine = {(o.kind, o.schema, o.table, o.path, o.sheet) for o in table.origins if o.table or o.path}

                def same(t):
                    return t is not None and any(o.source_id == source['id'] and (
                        not mine or (o.kind, o.schema, o.table, o.path, o.sheet) in mine) for o in t.origins)
                found = model.find(table.id)
                tables[id(table)] = found if same(found) else next((t for t in model.tables if mine and same(t)), None)
            return tables[id(table)]

        def judged(table, name):
            """(table.column as it is shown, category or None, why, the user's mark)"""
            column = table.column(name)
            name = column.name if column is not None else name
            if table.key not in saved:
                saved[table.key] = self.prof.store.columns(workspace_id, table.key).get(table.key, {})
            detected = (saved[table.key].get(name.upper()) or {}).get('detected')
            mark = marks.get((table.key, name.upper()))
            category = self.prof.personal_category(marks, table, name, detected)
            text = personal.CATEGORY_TEXT.get(category, category)
            why = (None if not category else 'marked personal by you' if category == 'user'
                   else f'looks like {text}' if personal_by_name(name, table.name) == category
                   else f'its values look like {text}')
            twice = sum(1 for t in model.tables if t.name.upper() == table.name.upper()) > 1
            return f'{table.id if twice else table.name}.{name}', category, why, mark

        def strength(found):                            # what hides most decides how the values are masked
            return _strength(found[1])

        def judge(origin):
            if origin.count:
                return None, None, True                 # a number of rows, whatever was counted
            found = []
            for table, name in origin.columns:
                table = merged(table)
                if table is None:
                    return None
                found.append(judged(table, name))
            hits = sorted((f for f in found if f[1]), key=strength)
            if hits:
                label, category, why, _ = hits[0]
                return category, f'{label}: {why}' if origin.plain else f'Built from {label} ({why})', True
            # marked "not personal" by the user: shown, and its values do not overrule the mark
            return None, None, origin.plain and bool(found) and all(f[3] is False for f in found)
        return judge

    def _present(self, workspace_id, source, checked, columns, rows, more, elapsed):
        marks = self.prof.store.marks(workspace_id)
        marked = {column for (_, column), personal in marks.items() if personal}
        holders = {}                                        # NAME -> the workspace's tables that have such a column

        def in_a_table(name):
            """What a column of this name is in a table of the workspace that has one. Some rules need
            the table (pay in a table of staff, a latitude in a table of addresses): the name alone
            says nothing there, and a column that is not resolved must not be shown because of it."""
            if not holders:
                for table in self.ws.model(workspace_id).tables:
                    for column in table.columns:
                        holders.setdefault(column.name.upper(), []).append(table)
            found = [category for category in (self.prof.personal_category(marks, table, str(name))
                                               for table in holders.get(str(name).upper(), ())) if category]
            return min(found, key=_strength) if found else None

        def by_name(name):                                  # without a table: for what could not be resolved
            return 'user' if str(name).upper() in marked else personal_by_name(name) or in_a_table(name)

        known = origins(checked.tokens, self._dialect(source), source['model'], columns)
        judge = self._judge(workspace_id, source, marks)
        outer, tainted = derived_columns(checked.tokens, by_name)
        counts = count_columns(checked.tokens)
        if outer is None or len(outer) != len(columns):
            counts = set()                                  # the select list does not line up with the result
        described, categories = [], []
        for i, name in enumerate(columns):
            values = [row[i] for row in rows]
            verdict = judge(known[i]) if known and known[i] is not None else None
            if verdict is not None:                         # its table columns are known: judged like the Profile tab
                category, reason, settled = verdict
            elif i in counts:                               # a count of a personal column is not personal
                category, reason, settled = None, None, True
            else:                                           # not resolved for sure: by names alone, as before
                settled = False
                category = by_name(name) or tainted.get(str(name).upper())
                reason = None if not category else ('Marked personal by you' if category == 'user'
                                                    else f'Looks like {CATEGORY_TEXT.get(category, category)}')
                if not category and outer is not None and len(outer) == len(columns) and outer[i]:
                    category, reason = outer[i], 'Built from a personal column'
                elif category and tainted.get(str(name).upper()) and not by_name(name):
                    reason = 'Built from a personal column'
            if not category and not settled:
                category = personal_by_values(values, name)
                reason = f'Its values look like {CATEGORY_TEXT.get(category, category)}' if category else None
            categories.append(category)
            described.append({'name': name, 'personal': bool(category), 'reason': reason})
        show = self.prof.show_personal
        shown = [[jsonable(v, CELL_MAX_CHARS) if show or not category else mask(v, category)
                  for v, category in zip(row, categories)] for row in rows]
        result = {
            'id': new_id(), 'workspace_id': workspace_id,
            'source': {'id': source['id'], 'label': source['label'], 'dialect': self._dialect(source)},
            'sql': checked.sql, 'columns': described, 'rows': shown, 'row_count': len(shown), 'more': more,
            'max_rows': config.SQL_MAX_ROWS, 'elapsed_s': round(elapsed, 2),
            'personal': [d['name'] for d in described if d['personal']],
            'masked': not show and any(categories), 'unmasked': show and any(categories), 'show_personal': show,
        }
        with self._lock:
            self._results[result['id']] = result
            while len(self._results) > KEEP_RESULTS:
                self._results.popitem(last=False)
        return result

    def csv(self, workspace_id, result_id):
        """A result as CSV: the same columns and the same (masked) values that were shown.
        Returns (file name, bytes). Text that a spreadsheet would run as a formula gets a leading
        apostrophe."""
        with self._lock:
            result = self._results.get(result_id)
        if result is None or result['workspace_id'] != workspace_id:
            raise NotFound('This result is no longer in memory. Run the query again to export it.')
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\r\n')
        writer.writerow([_cell(c['name']) for c in result['columns']])
        for row in result['rows']:
            writer.writerow([_cell(v) for v in row])
        return f'query-{result_id}.csv', ('﻿' + out.getvalue()).encode('utf-8')


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return value


def _plain_type(type_name):
    """A type from a source's structure as it may stand in TRY_CAST: letters, digits, brackets and commas only."""
    text = ' '.join(str(type_name or 'VARCHAR').upper().split())
    return text if all(ch.isalnum() or ch in ' (),_' for ch in text) else 'VARCHAR'
