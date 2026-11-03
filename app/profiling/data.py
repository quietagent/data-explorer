"""Where profiling reads from, and where it works.

  DatabaseData   one table of a live database. Every statement goes through
                 ReadOnlyDatabase.connect(), so through the read-only guard, the read-only
                 session and the statement timeout. One connection, one statement at a time; it
                 is rolled back and closed when the table is done, and after any error.
                 read() is THE read of a table: its rows come back in chunks.
  SampleData     the in-memory DuckDB copy of that one read. Statistics, top values, histograms
                 and the scan for personal values all run on this copy - never on the database.
  FileData       one CSV file or Excel sheet in an in-memory DuckDB table. UTF-8 CSV / TSV files
                 are read where they are; Excel sheets and text in another encoding are read by
                 Python and handed over in memory.

Both copies are tables called "data" and answer run(statement) with a SQLAlchemy Core statement,
so the profiler does not care which one it has. Neither ever writes to disk: DuckDB has no
temporary directory here, and what does not fit in memory fails instead (app.sources.duck).
"""

from __future__ import annotations

import codecs
import csv
import math
import time
from datetime import date, datetime, time as time_of_day
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from .. import config
from ..security import ReadOnlyViolation, sanitize_error
from ..sources import SourceError, duck
from ..sources.files import CSV_EXT, EXCEL_EXT, sheet_rows
from . import queries as q

STREAMED = ('mysql', 'oracle')          # drivers that can hand rows over as they arrive without extra statements


class QueryFailed(Exception):
    """One query failed. `timeout` tells a statement timeout from any other error."""

    def __init__(self, message, timeout=False):
        super().__init__(message)
        self.timeout = timeout


TIMEOUT_WORDS = ('timeout', 'timed out', 'time out', 'interrupted', 'maximum statement execution time',
                 'dpy-4024', 'dpy-4011', 'ora-01013', 'ora-03156', '20003', 'query_canceled', 'canceling statement')


def short_error(exc, keep=()):
    """A DuckDB or Python error as it may be shown: one line, nothing quoted from the data."""
    return sanitize_error(str(exc), keep, limit=300) or type(exc).__name__


# ------------------------------------------------------------------ database ----

class DatabaseData:
    """One table of a live database."""

    def __init__(self, db, schema, table, is_view=False):
        self.db = db
        self.dialect = db.profile.engine
        self.schema, self.table = schema, table
        self.is_view = is_view          # views are never sampled with SAMPLE / TABLESAMPLE
        self.label = f'{schema}.{table}' if schema else table
        self._context = self._conn = None
        self.queries = 0                # statements sent for this table

    def _connection(self):
        if self._conn is None:
            self._context = self.db.connect()
            self._conn = self._context.__enter__()
        return self._conn

    def _failed(self, exc, started):
        if isinstance(exc, ReadOnlyViolation):
            self.close()
            return exc
        if isinstance(exc, SourceError):                # cannot connect, or read-only mode was refused
            self.close()
            return QueryFailed(str(exc))
        message = self.db.clean_error(exc, keep=(self.schema, self.table))
        elapsed = time.monotonic() - started
        timeout = elapsed >= self.db.timeout_s - 0.5 or any(w in str(exc).lower() for w in TIMEOUT_WORDS)
        self.close()                                    # some drivers leave the connection unusable
        return QueryFailed(f'timed out after {self.db.timeout_s} s' if timeout else message, timeout)

    def run(self, statement):
        """Run one statement and return all its rows. Raises QueryFailed."""
        started = time.monotonic()
        try:
            conn = self._connection()
            self.queries += 1
            return [tuple(row) for row in conn.execute(statement).fetchall()]
        except Exception as exc:
            raise self._failed(exc, started) from None

    def read(self, statement, chunk_rows=None):
        """Run one statement and yield its rows in chunks, as they arrive where the driver can."""
        started = time.monotonic()
        result = None
        try:
            conn = self._connection()
            self.queries += 1
            if self.dialect in STREAMED:
                conn = conn.execution_options(stream_results=True)
            result = conn.execute(statement)
            while True:
                rows = result.fetchmany(chunk_rows or config.READ_CHUNK_ROWS)
                if not rows:
                    break
                yield rows
        except GeneratorExit:
            raise
        except Exception as exc:
            raise self._failed(exc, started) from None
        finally:
            if result is not None:
                try:
                    result.close()
                except Exception:
                    pass

    def close(self):
        context, self._context, self._conn = self._context, None, None
        if context is not None:
            try:
                context.__exit__(None, None, None)      # rollback, then close
            except Exception:
                pass


# ------------------------------------------------------------ in-memory copies ----

class DuckTable:
    """An in-memory DuckDB table called "data"."""
    dialect = q.DUCKDB
    schema = None
    table = 'data'

    def __init__(self):
        self._con = None
        self.columns = []               # [(name, DuckDB type)] of the table
        self.queries = 0                # statements run on the copy (none of them reaches a database)
        self.total_rows = None          # rows in the file / rows the read returned
        self.loaded_rows = None         # rows in the table
        self.sampled, self.method = False, None
        self.plans = []                 # [ColumnPlan] of the table
        self.notes = []

    def run(self, statement):
        sql, parameters = q.compile_for_duckdb(statement)
        self.queries += 1
        try:
            return [tuple(row) for row in self._con.execute(sql, parameters).fetchall()]
        except Exception as exc:
            raise QueryFailed(short_error(exc, [c for c, _ in self.columns])) from None

    def count_matches(self, columns, keys):
        """For each of the given keys (tuples of values), how many rows of this table hold it:
        {index of the key: rows}. The keys are handed over as one JSON text and joined inside
        DuckDB, each value read as the type of the column it is compared with."""
        if not keys:
            return {}
        types = dict(self.columns)
        by_name = {name.lower(): name for name, _ in self.columns}
        names = [by_name[c.lower()] for c in columns]
        # A key for a column of days is read as a moment: DuckDB would cut "2025-10-01 13:00" down to the day
        # and call it a match. Compared as a moment, only midnight of that day is that day.
        kinds = ['TIMESTAMP' if types[name].upper() == 'DATE' else types[name] for name in names]
        typed = ', '.join(f'TRY_CAST(r[{i + 2}] AS {kind}) AS k{i}' for i, kind in enumerate(kinds))
        on = ' AND '.join(f'p.{q.quote_duck(name)} = k.k{i}' for i, name in enumerate(names))
        sql = (f'SELECT k.i, COUNT(*) FROM (SELECT CAST(r[1] AS INTEGER) AS i, {typed} '
               f'FROM (SELECT unnest(from_json(?, \'["VARCHAR[]"]\')) AS r)) k JOIN "data" p ON {on} GROUP BY k.i')
        rows = [[str(i)] + [as_text(v) for v in key] for i, key in enumerate(keys)]
        self.queries += 1
        try:
            import json
            return {int(i): int(n) for i, n in self._con.execute(sql, [json.dumps(rows, ensure_ascii=False)]).fetchall()}
        except Exception as exc:
            raise QueryFailed(short_error(exc, [c for c, _ in self.columns])) from None

    def _finish(self):
        """After loading: note what is there, and cut the copy off from files and network."""
        self.columns = [(r[0], r[1]) for r in self._con.execute('DESCRIBE "data"').fetchall()]
        self.loaded_rows = self._con.execute('SELECT COUNT(*) FROM "data"').fetchone()[0]
        if not self.plans:
            self.plans = [q.ColumnPlan.of(name, kind, q.DUCKDB) for name, kind in self.columns]
        duck.lock_down(self._con)

    def close(self):
        con, self._con = self._con, None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def as_text(value):
    """A database value as the text it is loaded with (typed again inside DuckDB)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if math.isfinite(value) and value.is_integer() and abs(value) < 1e17 else repr(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return str(int(value)) if value == value.to_integral_value() else format(value, 'f')
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=' ')
    if isinstance(value, (date, time_of_day)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return '0x' + raw[:32].hex() + ('…' if len(raw) > 32 else '')
    return str(value)


class SampleData(DuckTable):
    """The in-memory copy of one read of a database table."""

    def __init__(self, plans):
        super().__init__()
        self.source_plans = list(plans)             # [ColumnPlan] as the database has them
        self.names = duck.unique_names([p.name for p in plans])     # the copy's column names (DuckDB ignores case)
        self.row_count = self.row_count_kind = None # filled in by profiler.read_table()
        self.random = self.plan = self.count_note = None
        self.statements = 0

    def load(self, chunks, limit=None, check=None):
        """Fill the copy from chunks of rows. Stops after `limit` rows.
        Returns (rows stored, True if the read had more rows than the limit)."""
        self._con = duck.open_memory()
        width = len(self.names)
        try:
            duck.create_text_table(self._con, 'raw', self.names)
            stored, more = 0, False
            for rows in chunks:
                if check:
                    check()
                if limit is not None and stored + len(rows) > limit:
                    rows, more = rows[:limit - stored], True
                duck.insert_rows(self._con, 'raw', width, [[as_text(v) for v in row] for row in rows])
                stored += len(rows)
                if more:
                    break
            self._type_columns()
            self._finish()
            self.total_rows = stored
            return stored, more
        except Exception as exc:
            self.close()
            if isinstance(exc, duck_error()):
                raise QueryFailed(short_error(exc, self.names)) from None
            raise                                       # cancelled, out of time, or the read itself failed
        finally:
            done = getattr(chunks, 'close', None)       # end the read: nothing more is fetched
            if done is not None:
                done()

    def _type_columns(self):
        """Give each column of the copy the type its values came with."""
        candidates, empty = {}, {}
        for name, plan in zip(self.names, self.source_plans):
            kinds = {'number': ['huge', 'double'], 'date': ['timestamp'], 'boolean': ['boolean'],
                     'lob': ['whole']}.get(plan.family, [])
            candidates[name] = kinds
            empty[name] = kinds[-1] if kinds else None
        described, chosen = duck.typed_table(self._con, 'raw', 'data', self.names, candidates, empty)
        types = dict(described)
        for name, plan in zip(self.names, self.source_plans):
            family = plan.family
            if candidates[name] and chosen[name] is None:       # e.g. text kept in a SQLite DATE column
                family = 'text'
                self.notes.append(f'{plan.name}: its values are not all {plan.family}s, so it was read as text.')
            self.plans.append(q.ColumnPlan(name, plan.type, family, types.get(name)))


def duck_error():
    import duckdb
    return duckdb.Error


# ---------------------------------------------------------------------- files ----

class FileData(DuckTable):
    """One CSV file or Excel sheet, loaded into the in-memory table "data".

    threshold / sample_rows   a file with more rows than the threshold is loaded on a random sample
    limit                     load only the first rows (type detection when a source is added)
    only                      load only these columns (relationship checks need the keys alone)
    """

    def __init__(self, path, sheet=None, encoding=None, threshold=None, sample_rows=None, limit=None, only=None):
        super().__init__()
        self.path, self.sheet, self.encoding = Path(path), sheet, encoding
        self.label = self.path.name + (f' · sheet {sheet}' if sheet else '')
        self.threshold = config.SAMPLE_THRESHOLD_ROWS if threshold is None else threshold
        self.sample_rows = config.SAMPLE_TARGET_ROWS if sample_rows is None else sample_rows
        self.limit, self.only = limit, only
        self.more = False               # with `limit`: the file has more rows than were loaded
        self.all_columns = []           # every column of the file, also with `only`

    # ---------------------------------------------------------------- loading
    def open(self, check=None):
        """Read the file. Raises QueryFailed with a short reason if it cannot be read."""
        if not self.path.is_file():
            raise QueryFailed(f'file not found: {self.path.name}')
        self._con = duck.open_memory()
        try:
            ext = self.path.suffix.lower()
            if ext in EXCEL_EXT:
                names = self._load_rows(self._excel_rows(), check)
            elif ext in CSV_EXT and self._is_utf8():
                names = self._load_csv_in_place()
            elif ext in CSV_EXT:
                names = self._load_rows(self._text_rows(), check)
            else:
                raise QueryFailed(f'not a CSV or Excel file: {self.path.name}')
            if not names:
                raise QueryFailed('no data found')
            self.all_columns = list(names)
            if check:
                check()
            self._type_columns(self._wanted(names))
            self._finish()
        except QueryFailed:
            self.close()
            raise
        except Exception as exc:
            self.close()
            if not isinstance(exc, (duck_error(), OSError, ValueError, KeyError, csv.Error)):
                raise
            raise QueryFailed(short_error(exc)) from None
        return self

    def count_rows(self):
        """How many rows the file has. Nothing of it is kept."""
        if not self.path.is_file():
            raise QueryFailed(f'file not found: {self.path.name}')
        ext = self.path.suffix.lower()
        try:
            if ext in CSV_EXT and self._is_utf8():
                self._con = duck.open_memory()
                duck.csv_view(self._con, self.path, 'src')
                return int(self._con.execute('SELECT COUNT(*) FROM "src"').fetchone()[0])
            if ext in EXCEL_EXT:
                rows = self._excel_rows()
            elif ext in CSV_EXT:
                rows = self._text_rows()
            else:
                raise QueryFailed(f'not a CSV or Excel file: {self.path.name}')
            return max(0, sum(1 for row in rows if any(v not in (None, '') for v in row)) - 1)   # less the header
        except QueryFailed:
            raise
        except Exception as exc:
            if not isinstance(exc, (duck_error(), OSError, ValueError, KeyError, csv.Error)):
                raise
            raise QueryFailed(short_error(exc)) from None
        finally:
            self.close()

    def _wanted(self, names):
        if not self.only:
            return list(names)
        by_key = {n.lower(): n for n in names}
        missing = [c for c in self.only if c.lower() not in by_key]
        if missing:
            raise QueryFailed(f'column not found in {self.path.name}; read the source again')
        return [by_key[c.lower()] for c in self.only]

    def _is_utf8(self):
        if self.encoding:
            return self.encoding.lower().replace('-', '') in ('utf8', 'utf8sig')
        with open(self.path, 'rb') as f:
            head = f.read(4_000_000)
        if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            self.encoding = 'utf-16'
            return False
        try:
            head.decode('utf-8')
            return True
        except UnicodeDecodeError as exc:
            if exc.start >= len(head) - 4:
                return True                             # only the cut-off last character failed
            self.encoding = 'cp1256'
            return False

    def _load_csv_in_place(self):
        """DuckDB reads the file where it is, every column as text; only what is needed is kept."""
        names = duck.csv_view(self._con, self.path, 'src')
        self.all_columns = list(names)
        columns = ', '.join(q.quote_duck(c) for c in self._wanted(names))
        if self.limit is not None:
            self._con.execute(f'CREATE TABLE "raw" AS SELECT {columns} FROM "src" LIMIT ?', [int(self.limit) + 1])
            loaded = self._con.execute('SELECT COUNT(*) FROM "raw"').fetchone()[0]
            self.more = loaded > self.limit
            if self.more:
                self._con.execute('DELETE FROM "raw" WHERE rowid >= ?', [int(self.limit)])
            self.total_rows = None if self.more else loaded
            return names
        self.total_rows = self._con.execute('SELECT COUNT(*) FROM "src"').fetchone()[0]
        if self.total_rows > self.threshold:
            share = min(1.0, self.sample_rows / self.total_rows)
            self._con.execute(f'CREATE TABLE "raw" AS SELECT {columns} FROM "src" WHERE random() < ?', [share])
            self.sampled, self.method = True, f'random sample of about {share:.2%} of the rows (DuckDB)'
        else:
            self._con.execute(f'CREATE TABLE "raw" AS SELECT {columns} FROM "src"')
        return names

    def _excel_rows(self):
        from openpyxl import load_workbook
        wb = load_workbook(self.path, read_only=True, data_only=True)
        try:
            sheets = [ws for ws in wb.worksheets if ws.title == self.sheet] if self.sheet else wb.worksheets[:1]
            if not sheets:
                raise QueryFailed(f'sheet "{self.sheet}" is no longer in {self.path.name}')
            yield from sheet_rows(sheets[0])
        finally:
            wb.close()

    def _text_rows(self):
        """Rows of a text file that is not UTF-8, decoded by Python as they are read."""
        for encoding in dict.fromkeys([self.encoding, 'cp1256', 'cp1252']):
            if not encoding:
                continue
            try:
                with open(self.path, 'r', encoding=encoding, newline='') as f:
                    sample = f.read(64_000)
            except (UnicodeDecodeError, LookupError):
                continue
            self.encoding = encoding
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
            except csv.Error:
                dialect = csv.excel_tab if self.path.suffix.lower() == '.tsv' else csv.excel
            return self._decoded(encoding, dialect)
        raise QueryFailed(f'could not decode {self.path.name}')

    def _decoded(self, encoding, dialect):
        with open(self.path, 'r', encoding=encoding, errors='replace', newline='') as f:
            yield from csv.reader(f, dialect)

    def _load_rows(self, rows, check=None):
        """Rows of text (header first) -> the text table "raw". Returns the column names."""
        if self.limit is not None:
            rows = self._first(rows, int(self.limit))
        names, total = duck.load_text_rows(self._con, 'raw', rows, check=check)
        if self.limit is None:
            self.total_rows = total
            if total > self.threshold:
                share = min(1.0, self.sample_rows / total)
                self.sampled, self.method = True, f'random sample of about {share:.2%} of the rows (DuckDB)'
                self._share = share
        else:
            self.total_rows = None if self.more else total
        return names

    def _first(self, rows, limit):
        """The header and the first `limit` rows with something in them; notes whether more follow."""
        seen = -1
        for row in rows:
            if seen >= 0 and all(v is None or v == '' for v in row):
                continue
            if seen >= limit:
                self.more = True
                return
            seen += 1
            yield row

    def _type_columns(self, wanted):
        where = sa.func.random() < self._share if getattr(self, '_share', None) else None
        duck.typed_table(self._con, 'raw', 'data', wanted, where=where)
        try:
            self._con.execute('DROP VIEW IF EXISTS "src"')
        except Exception:
            pass
