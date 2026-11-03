"""The profiling queries, built with SQLAlchemy Core.

Every identifier that comes from a database or a file (schema, table and column names) goes in as
a quoted name, and every value as a bound parameter; nothing is put together as text.

Two kinds of statement are built here:

  * what is sent to a database: read_query() - the ONE read of a table or of a sample of it -,
    bounded_count_query(), count_query() (the "Exact count" action), rows_query() (the 20 sample
    rows) and the catalog's row estimates
  * the statistics, top values and histograms. Those only ever run on the in-memory DuckDB copy
    of that one read (and on files), which is given the SQL compiled for the SQLite dialect
    ("double-quoted" names, ? parameters)

Sampling is part of the FROM clause, in each engine's own syntax:

    Oracle        t SAMPLE (p) SEED (n)               rows: every block is read once, the sample is even
                  t SAMPLE BLOCK (p) SEED (n)         whole blocks, for a table above the size limit: only
                                                      p % of it is read, in a few stretches (BLOCK_SAMPLE)
    PostgreSQL    t TABLESAMPLE SYSTEM (p) REPEATABLE (n)
    SQL Server    t TABLESAMPLE (p PERCENT) REPEATABLE (n)
    SQLite        spread_query(): many short stretches of rows, evenly spaced over the table's row
                  numbers - not random, but not the first rows either (a table filled in time order
                  shows all its years). A view, and a table made WITHOUT ROWID: the first rows
    others        ... FROM t LIMIT n                  the first n rows - not random

The seed is saved with the profile, so a later read ("show personal values") sees the same sample.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import quoted_name
from sqlalchemy.sql.expression import TableClause
from sqlalchemy.sql.functions import FunctionElement

DUCKDB = 'duckdb'
PERCENT_SAMPLING = {'oracle': 'SAMPLE', 'postgresql': 'TABLESAMPLE SYSTEM', 'mssql': 'TABLESAMPLE'}
MIN_PERCENT = 0.0001
# Said wherever an Oracle block sample is shown: the Profile tab, a relationship's evidence, the report.
BLOCK_SAMPLE = 'block sample: a few stretches of the table, may not represent all of it'
SQLITE = sqlite.dialect()          # DuckDB is given SQL compiled for this dialect


# ---------------------------------------------------------------- identifiers ----

def ident(name):
    """A name exactly as the catalog has it; always quoted."""
    return quoted_name(str(name), True)


def col(name):
    return sa.column(ident(name))


def quote_duck(name):
    """A name quoted for DuckDB, for the few internal statements that are not built with Core."""
    return '"' + str(name).replace('"', '""') + '"'


def _schema(dialect, schema):
    if not schema:
        return None
    if dialect == 'mssql' and '.' in schema:        # "database.schema": the dialect splits and quotes it
        return schema
    return ident(schema)


# ---------------------------------------------------------------- column types ----

LOB = {
    'oracle': r'\b(CLOB|NCLOB|BLOB|BFILE|LONG|RAW|XMLTYPE|JSON|SDO_GEOMETRY|ANYDATA)\b',
    'mssql': r'\b(TEXT|NTEXT|IMAGE|XML|VARBINARY|BINARY|TIMESTAMP|ROWVERSION|GEOGRAPHY|GEOMETRY|HIERARCHYID'
             r'|SQL_VARIANT)\b|\(\s*MAX\s*\)',          # VARCHAR(MAX), NVARCHAR(MAX): up to 2 GB a value
    'postgresql': r'\b(BYTEA|XML|JSON|JSONB|TSVECTOR|TSQUERY|OID|POINT|LINE|LSEG|BOX|PATH|POLYGON|CIRCLE)\b|\[\]',
    'mysql': r'\b(TINYBLOB|BLOB|MEDIUMBLOB|LONGBLOB|TINYTEXT|TEXT|MEDIUMTEXT|LONGTEXT|BINARY|VARBINARY|JSON'
             r'|GEOMETRY|POINT|LINESTRING|POLYGON)\b',
    'sqlite': r'\b(BLOB|BINARY|VARBINARY)\b',
    DUCKDB: r'\b(BLOB|BIT|STRUCT|MAP|LIST|UNION)\b|\[\]',
}
NUMBER = (r'\b(U?(TINY|SMALL|MEDIUM|BIG|HUGE)?INT(EGER)?\d*|NUMBER|NUMERIC|DEC(IMAL)?|DOUBLE( PRECISION)?|FLOAT\d*'
          r'|REAL|(SMALL)?MONEY|(BIG|SMALL)?SERIAL|BINARY_FLOAT|BINARY_DOUBLE)\b')
WHOLE = r'\b(U?(TINY|SMALL|MEDIUM|BIG|HUGE)?INT(EGER)?\d*|(BIG|SMALL)?SERIAL)\b|\(\s*\d+\s*,\s*0\s*\)|^NUMBER\(\d+\)$'


def family(type_name, dialect):
    """How a column of this type is profiled:

    lob      nulls only (large or binary values: LOB, BLOB, CLOB, XML ...)
    number   min / max / avg, histogram          date    min / max, histogram by month or year
    text     min / max and lengths               boolean, other   counts and top values only
    """
    u = ' '.join((type_name or '').upper().split())
    if not u:
        return 'other'
    if re.search(r'\b(BINARY_FLOAT|BINARY_DOUBLE)\b', u):
        return 'number'
    if re.search(LOB.get(dialect, LOB[DUCKDB]), u):
        return 'lob'
    if re.search(r'\b(BOOL(EAN)?)\b', u) or (u.startswith('BIT') and dialect == 'mssql'):
        return 'boolean'
    if re.search(r'\bINTERVAL\b', u):
        return 'other'
    if re.search(NUMBER, u):
        return 'number'
    if re.search(r'\b(DATE|DATETIME2?|SMALLDATETIME|DATETIMEOFFSET|TIMESTAMP\w*)\b', u):
        return 'date'
    if re.search(r'CHAR|TEXT|STRING|CLOB', u):
        return 'text'
    return 'other'


def is_whole(type_name):
    """True for integer types, where histogram bins should start and end on whole numbers."""
    return bool(re.search(WHOLE, ' '.join((type_name or '').upper().split())))


@dataclass
class ColumnPlan:
    name: str
    type: str | None                # the type as the source declares it
    family: str
    duck: str | None = None         # the type of the column in the in-memory copy

    @classmethod
    def of(cls, name, type_name, dialect):
        return cls(name, type_name, family(type_name, dialect), type_name if dialect == DUCKDB else None)


# -------------------------------------------------------------------- sources ----

class SampledTable(TableClause):
    """A table whose FROM clause carries the engine's own sampling clause."""
    inherit_cache = False                           # the sample is part of the SQL text

    def __init__(self, name, *columns, schema=None, percent=None, seed=None, block=False):
        super().__init__(name, *columns, schema=schema)
        self.percent, self.seed, self.block = percent, seed, bool(block)


def _number(compiler, value):
    return compiler.render_literal_value(value, sa.Float() if isinstance(value, float) else sa.Integer())


@compiles(SampledTable)
def _table_default(element, compiler, **kw):
    return compiler.visit_table(element, **kw)


@compiles(SampledTable, 'oracle')
def _table_oracle(element, compiler, asfrom=False, **kw):
    text = compiler.visit_table(element, asfrom=asfrom, **kw)
    if asfrom and element.percent is not None:
        text += (f' SAMPLE {"BLOCK " if element.block else ""}({_number(compiler, element.percent)}) '
                 f'SEED ({_number(compiler, element.seed)})')
    return text


@compiles(SampledTable, 'postgresql')
def _table_postgresql(element, compiler, asfrom=False, **kw):
    text = compiler.visit_table(element, asfrom=asfrom, **kw)
    if asfrom and element.percent is not None:
        text += (f' TABLESAMPLE SYSTEM ({_number(compiler, element.percent)}) '
                 f'REPEATABLE ({_number(compiler, element.seed)})')
    return text


@compiles(SampledTable, 'mssql')
def _table_mssql(element, compiler, asfrom=False, **kw):
    text = compiler.visit_table(element, asfrom=asfrom, **kw)
    if asfrom and element.percent is not None:
        text += (f' TABLESAMPLE ({_number(compiler, element.percent)} PERCENT) '
                 f'REPEATABLE ({_number(compiler, element.seed)})')
    return text


@dataclass
class Source:
    """What a statistics statement reads from: a FROM object, and the dialect it is written for."""
    dialect: str
    frm: object


def table_source(dialect, schema, table):
    return Source(dialect, SampledTable(ident(table), schema=_schema(dialect, schema)))


# ----------------------------------------------------- what a database is asked ----

def read_query(dialect, schema, table, columns, percent=None, seed=None, limit=None, block=False):
    """The one read of a table: its columns for at most `limit` rows, or for a sample of it.

    columns: [ColumnPlan]. A large or binary column (family 'lob') is never fetched: only
    whether it holds a value is read (1 or NULL), which is all its null count needs.
    Result columns are named c0, c1 ... in the order given.
    block: Oracle only - sample whole blocks (SAMPLE BLOCK) instead of rows.
    """
    if percent is not None and dialect in PERCENT_SAMPLING:
        percent = round(min(99.0, max(MIN_PERCENT, float(percent))), 4)
        frm = SampledTable(ident(table), schema=_schema(dialect, schema), percent=percent, seed=int(seed or 1),
                           block=block and dialect == 'oracle')
    else:
        frm = SampledTable(ident(table), schema=_schema(dialect, schema))
    items = []
    for i, c in enumerate(columns):
        x = col(c.name)
        if c.family == 'lob':
            x = sa.case((x.is_not(None), sa.literal_column('1')), else_=sa.null())
        items.append(x.label(f'c{i}'))
    statement = sa.select(*items).select_from(frm)
    return statement.limit(int(limit)) if limit else statement


ROWID_NAMES = ('rowid', '_rowid_', 'oid')         # SQLite's names for the hidden row number of a table


def rowid_name(columns):
    """The name under which a SQLite table's row number can be read: the first of rowid, _rowid_ and oid that is
    not also the name of one of its own columns. None when it has a column of each name."""
    taken = {str(c).lower() for c in columns}
    return next((name for name in ROWID_NAMES if name not in taken), None)


def rowid_bounds_query(schema, table, rowid):
    """SQLite: the smallest and the largest row number of a table - two look-ups at the ends of the table, not a
    scan. It fails on a table made WITHOUT ROWID, which is how the caller learns that there is none."""
    plain = sa.table(ident(table), schema=_schema('sqlite', schema))
    number = sa.literal_column(rowid)
    return sa.select(sa.func.min(number), sa.func.max(number)).select_from(plain)


def spread_query(schema, table, columns, rowid, low, span, stretches, rows, limit):
    """SQLite: a sample spread over the whole table, read by row number. The `span` row numbers from `low` on
    are cut into `stretches` equal parts, and the first `rows` rows of each part are read.

    The table is entered through its row numbers for every part (SEARCH ... USING INTEGER PRIMARY KEY), so only
    the rows of the sample are read: no scan of the table, and as fast as reading its first rows. A part gives
    at most `rows` rows however many it holds, so where the row numbers have gaps, or lie in a bunch, the
    sample gets smaller - it never turns into "the first rows" under another name. The parts are worked out
    by the statement itself (a recursive list of numbers): a handful of parameters, however many parts there
    are. Columns as in read_query(): c0, c1 ..., a large or binary one only as "has a value".
    """
    one, big = sa.literal_column('1'), sa.BigInteger()
    count = sa.bindparam('stretches', int(stretches))
    parts = sa.select(sa.literal_column('0').label('i')).cte('s', recursive=True)
    parts = parts.union_all(sa.select(parts.c.i + one).where(parts.c.i + one < count))

    def start(i):
        """The first row number of part i: low + i * span / stretches, in whole numbers and without leaving them
        (the span can be as large as a row number gets)."""
        whole, rest = divmod(int(span), int(stretches))
        return (sa.bindparam('low', int(low), type_=big) + i * sa.bindparam('whole', whole, type_=big)
                + (i * sa.bindparam('rest', rest)) // count)          # "//": whole-number division, also in the SQL
    plain = sa.table(ident(table), *[sa.column(ident(c.name)) for c in columns], schema=_schema('sqlite', schema))
    t, part = plain.alias('t'), plain.alias('x')
    number, inner = sa.literal_column(f't.{rowid}'), sa.literal_column(f'x.{rowid}')
    items = []
    for i, c in enumerate(columns):
        x = t.c[c.name]
        if c.family == 'lob':
            x = sa.case((x.is_not(None), one), else_=sa.null())
        items.append(x.label(f'c{i}'))
    first_rows = (sa.select(inner).select_from(part).where(inner >= start(parts.c.i), inner < start(parts.c.i + one))
                  .order_by(inner).limit(sa.bindparam('rows', int(rows))))
    return sa.select(*items).select_from(parts.join(t, number.in_(first_rows))).limit(int(limit))


def spread_text(stretches, rows):
    """How a spread sample was taken, in words - labelled like the other samples that are not random."""
    return f'{int(stretches):,} stretches of {int(rows):,} rows spread over the table, not random'


def sample_text(dialect, percent=None, limit=None, block=False):
    """How a sample was taken, in words (shown with every sampled result)."""
    if percent is not None and dialect in PERCENT_SAMPLING:
        if dialect == 'oracle' and block:
            return f'SAMPLE BLOCK ({round(float(percent), 4):g}%): {BLOCK_SAMPLE}'
        unit = {'oracle': 'rows', 'postgresql': 'blocks', 'mssql': 'pages'}[dialect]
        return f'{PERCENT_SAMPLING[dialect]} ({round(float(percent), 4):g}%): a random sample of {unit}'
    return f'first {int(limit):,} rows, not random'


def bounded_count_query(dialect, schema, table, limit):
    """How many rows a table has, counting no further than `limit`: COUNT(*) over its first rows."""
    plain = SampledTable(ident(table), schema=_schema(dialect, schema))
    inner = sa.select(sa.literal_column('1').label('x')).select_from(plain).limit(int(limit)).subquery('s')
    return sa.select(sa.func.count().label('n')).select_from(inner)


# ---------------------------------------------------------- relationship checks ----

def lookup_query(dialect, schema, table, columns, keys, types=None):
    """How many rows a parent table has for each of the given key values.

    One key column: a bound IN list. More: (a = ? AND b = ?) OR (...) - row-value IN lists are not
    portable. Every value is a bound parameter; `types` gives each column's bind type.
    Rows: the key columns, then the count.
    """
    frm = SampledTable(ident(table), schema=_schema(dialect, schema))
    cols = [col(c) for c in columns]
    types = list(types or [None] * len(cols))
    if len(cols) == 1:
        where = cols[0].in_(sa.bindparam('k', [key[0] for key in keys], expanding=True, type_=types[0]))
    else:
        where = sa.or_(*[sa.and_(*[c == sa.literal(v, t) for c, v, t in zip(cols, key, types)]) for key in keys])
    return sa.select(*cols, sa.func.count().label('n')).select_from(frm).where(where).group_by(*cols)


def bounds_query(dialect, schema, table, column):
    """The smallest and the largest value of one column of a table: (min, max)."""
    frm = SampledTable(ident(table), schema=_schema(dialect, schema))
    return sa.select(sa.func.min(col(column)).label('low'), sa.func.max(col(column)).label('high')).select_from(frm)


def duplicates_query(dialect, schema, table, columns):
    """One row if some key occurs more than once in the table; no row if every key is unique."""
    frm = SampledTable(ident(table), schema=_schema(dialect, schema))
    cols = [col(c) for c in columns]
    return (sa.select(*cols).select_from(frm).where(*[c.is_not(None) for c in cols]).group_by(*cols)
            .having(sa.func.count() > sa.literal_column('1')).limit(1))


def _keyed(dialect, schema, table, columns, alias):
    """A table under an alias, and its key columns."""
    frm = sa.table(ident(table), *[sa.column(ident(c)) for c in columns], schema=_schema(dialect, schema)).alias(alias)
    return frm, [frm.c[c] for c in columns]


def _orphans(dialect, child, columns, parent, parent_columns):
    """FROM and WHERE of "the child rows that have a key and no parent": an anti-join (NOT EXISTS)."""
    c, keys = _keyed(dialect, *child, columns, 'c')
    p, parent_keys = _keyed(dialect, *parent, parent_columns, 'p')
    has_parent = sa.exists(sa.select(sa.literal_column('1')).select_from(p).where(*[a == b for a, b in zip(parent_keys, keys)]))
    return c, keys, [*[k.is_not(None) for k in keys], ~has_parent]


def orphan_count_query(dialect, child, columns, parent, parent_columns):
    """A relationship counted over the WHOLE child table, inside the database, in one statement.

    child, parent: (schema, table). One row: the child's rows, its rows whose key is empty in some
    column, the different keys that have no parent, and the rows that have no parent. An empty
    key is never an orphan. The two tables are compared by the database itself, so with its own
    rules for letter case and padding - which is why the caller only asks when both key columns
    are of the same kind.
    """
    t, keys = _keyed(dialect, *child, columns, 't')
    one, zero = sa.literal_column('1'), sa.literal_column('0')
    empty = sa.case((sa.or_(*[k.is_(None) for k in keys]), one), else_=zero)
    totals = sa.select(sa.func.count().label('n'), sa.func.coalesce(sa.func.sum(empty), zero).label('e')).select_from(t).subquery('a')
    c, keys, where = _orphans(dialect, child, columns, parent, parent_columns)
    per_key = sa.select(sa.func.count().label('n')).select_from(c).where(*where).group_by(*keys).subquery('g')
    orphans = sa.select(sa.func.count().label('k'), sa.func.coalesce(sa.func.sum(per_key.c.n), zero).label('r')).subquery('o')
    return sa.select(totals.c.n, totals.c.e, orphans.c.k, orphans.c.r).select_from(totals.join(orphans, one == one))


def orphan_examples_query(dialect, child, columns, parent, parent_columns, limit):
    """The keys of the child that have no parent, those with the most rows first: the key columns, then the rows."""
    c, keys, where = _orphans(dialect, child, columns, parent, parent_columns)
    n = sa.func.count().label('n')
    return sa.select(*keys, n).select_from(c).where(*where).group_by(*keys).order_by(n.desc(), *keys).limit(int(limit))


def key_counts_query(columns):
    """On an in-memory copy: (rows, rows whose key is empty in some column)."""
    cols = [col(c) for c in columns]
    empty = sa.case((sa.or_(*[c.is_(None) for c in cols]), 1), else_=0)
    return sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(empty), 0)).select_from(table_source(DUCKDB, None, 'data').frm)


def key_shape_query(columns):
    """On an in-memory copy: (distinct keys, the most rows one key has)."""
    cols = [col(c) for c in columns]
    inner = (sa.select(sa.func.count().label('n')).select_from(table_source(DUCKDB, None, 'data').frm)
             .where(*[c.is_not(None) for c in cols]).group_by(*cols).subquery('k'))
    return sa.select(sa.func.count(), sa.func.coalesce(sa.func.max(inner.c.n), 0)).select_from(inner)


def key_rows_query(columns, limit):
    """On an in-memory copy: its distinct keys with their row counts. When there are more than
    `limit`, an arbitrary but repeatable subset of them (ordered by a hash, not by frequency)."""
    cols = [col(c) for c in columns]
    return (sa.select(*cols, sa.func.count().label('n')).select_from(table_source(DUCKDB, None, 'data').frm)
            .where(*[c.is_not(None) for c in cols]).group_by(*cols)
            .order_by(sa.func.hash(*cols), *cols).limit(int(limit)))


# ------------------------------------------------------------------ expressions ----

class TryCast(FunctionElement):
    """TRY_CAST(x AS type): NULL instead of an error (DuckDB, for the mixed-type check on files)."""
    inherit_cache = False
    name = 'try_cast'

    def __init__(self, expression, kind):
        super().__init__(expression)
        self.kind = kind


@compiles(TryCast)
def _try_cast(element, compiler, **kw):
    kinds = {'number': 'DOUBLE', 'double': 'DOUBLE', 'date': 'DATE', 'timestamp': 'TIMESTAMP', 'whole': 'BIGINT',
             'huge': 'HUGEINT', 'boolean': 'BOOLEAN'}
    return f'TRY_CAST({compiler.process(element.clauses, **kw)} AS {kinds[element.kind]})'


# How a whole number, a number and a boolean are spelled in text (DuckDB regular expressions).
# No leading zeros: '007' and '0550000001' are codes and phone numbers, not numbers.
SPELLING = {
    'whole': r'[-+]?(0|[1-9][0-9]{0,17})',
    'huge': r'[-+]?(0|[1-9][0-9]{0,37})',             # longer than 18 digits: ICCIDs, big keys
    'number': r'[-+]?((0|[1-9][0-9]*)(\.[0-9]*)?|\.[0-9]+)([eE][-+]?[0-9]+)?',
    'boolean': r'(?i)(true|false)',
    'date': r'[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}',        # no time of day: with one it is a timestamp
}


def _as_double(x, dialect):
    # MySQL cannot CAST to DOUBLE before 8.0.17, and does not need to: its AVG and "/" are exact.
    return x if dialect == 'mysql' else sa.cast(x, sa.Double())


def _length(x, dialect):
    name = {'mssql': 'len', 'mysql': 'char_length'}.get(dialect, 'length')
    return getattr(sa.func, name)(x)


def _null_count(x):
    return sa.func.sum(sa.case((x.is_(None), 1), else_=0))      # works for every type, LOBs included


# ----------------------------------------------------------------------- queries ----

def count_query(source):
    return sa.select(sa.func.count().label('n')).select_from(source.frm)


def stats_query(source, columns, mixed=False):
    """One row: the row count, then for each column its null count and - by type family - distinct
    count, min, max, average and text lengths. Returns (statement, [(column, field), ...]) where
    the list names the result columns after the first.

    mixed: also count the text values that would read as numbers and as dates (files only).
    """
    d = source.dialect
    out, fields = [sa.func.count().label('n')], []

    def add(expression, column, name):
        out.append(expression.label(f'c{len(out)}'))
        fields.append((column.name, name))

    for c in columns:
        x = col(c.name)
        add(_null_count(x), c, 'nulls')
        if c.family == 'lob':
            continue
        add(sa.func.count(sa.distinct(x)), c, 'distinct')
        if c.family in ('number', 'text', 'date'):
            add(sa.func.min(x), c, 'min')
            add(sa.func.max(x), c, 'max')
        if c.family == 'number':
            add(sa.func.avg(_as_double(x, d)), c, 'avg')
        if c.family == 'text':
            add(sa.func.min(_length(x, d)), c, 'min_length')
            add(sa.func.max(_length(x, d)), c, 'max_length')
            if mixed and d == DUCKDB:
                spelled = sa.func.regexp_full_match(sa.func.trim(x), SPELLING['number'])
                add(sa.func.count(sa.case((spelled, 1))), c, 'number_like')
                add(sa.func.count(TryCast(x, 'timestamp')), c, 'date_like')
    return sa.select(*out).select_from(source.frm), fields


def top_values_query(source, column, limit=10):
    x = col(column)
    n = sa.func.count().label('n')
    return (sa.select(x.label('v'), n).select_from(source.frm).where(x.is_not(None))
            .group_by(x).order_by(n.desc(), x.asc()).limit(int(limit)))


def number_histogram_query(source, column, low, width):
    """Rows of (bin number, count); bin b covers low + b * width up to low + (b + 1) * width."""
    d = source.dialect
    x = col(column)
    position = (_as_double(x, d) - sa.bindparam('low', float(low))) / sa.bindparam('width', float(width))
    # SQLite may be built without floor(); the position is never negative, so truncating is the same.
    bucket = sa.cast(position, sa.Integer()) if d == 'sqlite' else sa.func.floor(position)
    inner = sa.select(bucket.label('b')).select_from(source.frm).where(x.is_not(None)).subquery('h')
    return (sa.select(inner.c.b, sa.func.count().label('n')).group_by(inner.c.b).order_by(inner.c.b))


def date_histogram_query(source, column, by_month, limit=400):
    """Rows of (year, month, count), or (year, count) when not by_month."""
    x = col(column)
    parts = [sa.extract('year', x).label('y')] + ([sa.extract('month', x).label('m')] if by_month else [])
    inner = sa.select(*parts).select_from(source.frm).where(x.is_not(None)).subquery('h')
    keys = [inner.c.y] + ([inner.c.m] if by_month else [])
    return sa.select(*keys, sa.func.count().label('n')).group_by(*keys).order_by(*keys).limit(int(limit))


def rows_query(dialect, schema, table, columns, limit):
    """The first `limit` rows of the table itself (sample rows, and the values checked for personal data)."""
    plain = SampledTable(ident(table), schema=_schema(dialect, schema))
    return sa.select(*[col(c) for c in columns]).select_from(plain).limit(int(limit))


def minmax_query(source, columns):
    out = []
    for c in columns:
        out += [sa.func.min(col(c)).label(f'a{len(out)}'), sa.func.max(col(c)).label(f'b{len(out)}')]
    return sa.select(*out).select_from(source.frm)


# ------------------------------------------------------------- catalog estimates ----

def estimate_query(dialect, schema):
    """A statement giving (table name, estimated rows) for every table of a schema, from the
    engine's own statistics - or None when the engine has none (SQLite). Oracle also gives the
    table's blocks and average row length: its size decides between row and block sampling."""
    if dialect == 'oracle':
        t = sa.table('all_tables', sa.column('owner'), sa.column('table_name'), sa.column('num_rows'),
                     sa.column('blocks'), sa.column('avg_row_len'))
        return sa.select(t.c.table_name, t.c.num_rows, t.c.blocks, t.c.avg_row_len).where(t.c.owner == schema)
    if dialect == 'postgresql':
        # A partitioned table has no rows of its own (reltuples is -1 or 0): its estimate is the
        # sum over its partitions, however deep they are nested. One statement for the schema.
        columns = [sa.column(name) for name in ('oid', 'relname', 'reltuples', 'relnamespace', 'relkind')]
        c = sa.table('pg_class', *columns, schema='pg_catalog')
        n = sa.table('pg_namespace', sa.column('oid'), sa.column('nspname'), schema='pg_catalog')
        i = sa.table('pg_inherits', sa.column('inhrelid'), sa.column('inhparent'), schema='pg_catalog')
        top, tops = c.alias('top'), n.alias('tops')
        parts = (sa.select(top.c.oid.label('root'), top.c.oid.label('relid'))
                 .select_from(top.join(tops, tops.c.oid == top.c.relnamespace))
                 .where(tops.c.nspname == schema, top.c.relkind == 'p').cte('parts', recursive=True))
        parts = parts.union_all(sa.select(parts.c.root, i.c.inhrelid).select_from(parts.join(i, i.c.inhparent == parts.c.relid)))
        leaf = c.alias('leaf')
        sums = (sa.select(parts.c.root, sa.func.sum(sa.case((leaf.c.reltuples > 0, leaf.c.reltuples), else_=0)).label('n'))
                .select_from(parts.join(leaf, leaf.c.oid == parts.c.relid))
                .where(leaf.c.relkind.in_(['r', 'm', 'f'])).group_by(parts.c.root).subquery('sums'))
        return (sa.select(c.c.relname, sa.case((c.c.relkind == 'p', sums.c.n), else_=c.c.reltuples))
                .select_from(c.join(n, n.c.oid == c.c.relnamespace).outerjoin(sums, sums.c.root == c.c.oid))
                .where(n.c.nspname == schema, c.c.relkind.in_(['r', 'p', 'm'])))
    if dialect == 'mssql':
        database, _, schema_name = (schema or 'dbo').rpartition('.')
        catalog = f'{database}.sys' if database else 'sys'
        t = sa.table('tables', sa.column('object_id'), sa.column('name'), sa.column('schema_id'), schema=catalog)
        s = sa.table('schemas', sa.column('schema_id'), sa.column('name'), schema=catalog)
        p = sa.table('partitions', sa.column('object_id'), sa.column('index_id'), sa.column('rows'), schema=catalog)
        return (sa.select(t.c.name, sa.func.sum(p.c.rows))
                .select_from(t.join(s, s.c.schema_id == t.c.schema_id).join(p, p.c.object_id == t.c.object_id))
                .where(s.c.name == schema_name, p.c.index_id.in_([0, 1])).group_by(t.c.name))
    if dialect == 'mysql':
        t = sa.table('TABLES', sa.column('TABLE_SCHEMA'), sa.column('TABLE_NAME'), sa.column('TABLE_ROWS'),
                     schema='information_schema')
        return sa.select(t.c.TABLE_NAME, t.c.TABLE_ROWS).where(t.c.TABLE_SCHEMA == schema)
    return None


# ------------------------------------------------------------------- for DuckDB ----

def compile_for_duckdb(statement):
    """(sql, parameters) for DuckDB: the SQLite dialect's quoting and ? placeholders."""
    compiled = statement.compile(dialect=SQLITE, compile_kwargs={'render_postcompile': True})
    return str(compiled), [compiled.params[name] for name in compiled.positiontup]
