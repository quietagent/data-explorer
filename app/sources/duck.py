"""In-memory DuckDB: where rows from files and database samples are held while they are worked on.

Everything that looks at data - type detection when a files source is added, profiling,
relationship checks, the SQL runner on files, the report - goes through the helpers here, so the
same rules hold everywhere:

  * memory only: no temporary directory, so what does not fit fails instead of spilling to disk
  * no extension is downloaded or loaded, and Python variables are never read as tables
  * files are read as text (every column VARCHAR) and each column then gets the first type that
    EVERY non-empty value can be read as. A bad row far down a file can therefore not fail the
    load, and '0550000001' keeps its leading zero
  * rows that come from Python (Excel sheets, text in another encoding, database rows) are handed
    over as one JSON text per chunk. Binding Python values one by one is several hundred times
    slower in DuckDB's Python client
  * lock_down() ends all file and network access once the data is in
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.schema import CreateTable

from .. import config
from ..profiling import queries as q

# Kinds a text column of a file can turn out to be, most specific first.
FILE_KINDS = ('whole', 'huge', 'number', 'boolean', 'date', 'timestamp')
CSV_OPTIONS = {'all_varchar': True, 'null_padding': True, 'strict_mode': False}


def open_memory():
    """An in-memory DuckDB that cannot spill to disk, load an extension or read Python variables."""
    import duckdb
    return duckdb.connect(':memory:', config={
        'autoinstall_known_extensions': 'false', 'autoload_known_extensions': 'false',
        'temp_directory': '', 'python_enable_replacements': 'false',
        'memory_limit': f'{max(64, int(config.MEMORY_LIMIT_MB))}MB'})


def lock_down(con):
    """From here on: no file, no network, and no setting can be changed back."""
    con.execute('SET enable_external_access = false')
    con.execute('SET lock_configuration = true')


def execute(con, statement):
    """Run a SQLAlchemy Core statement on DuckDB."""
    sql, parameters = q.compile_for_duckdb(statement)
    return con.execute(sql, parameters)


def unique_names(names):
    """Column names as a table can hold them: blanks named, repeats numbered."""
    seen, out = {}, []
    for i, raw in enumerate(names):
        name = str(raw if raw is not None else '').strip() or f'column{i + 1}'
        key = name.lower()
        seen[key] = seen.get(key, 0) + 1
        out.append(name if seen[key] == 1 else f'{name}_{seen[key]}')
    return out


def text_table(name, columns):
    """A Core table whose columns are all text."""
    return sa.Table(name, sa.MetaData(), *[sa.Column(q.ident(c), sa.String()) for c in columns])


def create_text_table(con, name, columns):
    table = text_table(name, columns)
    con.execute(str(CreateTable(table).compile(dialect=q.SQLITE)))
    return table


def _clean(value):
    """Text that JSON and DuckDB both accept: no NUL, no half of a surrogate pair."""
    if value is None:
        return None
    return str(value).replace('\x00', '').encode('utf-8', 'replace').decode('utf-8')


def insert_rows(con, name, width, rows):
    """Append rows (lists of text or None) to a text table. One JSON document per call."""
    if not rows:
        return
    items = ', '.join(f'r[{i + 1}]' for i in range(width))
    sql = f'INSERT INTO {q.quote_duck(name)} SELECT {items} FROM (SELECT unnest(from_json(?, \'["VARCHAR[]"]\')) AS r)'
    try:
        con.execute(sql, [json.dumps(rows, ensure_ascii=False)])
    except Exception as exc:                            # an odd character: clean every value and try once more
        if type(exc).__name__ in ('OutOfMemoryException', 'InterruptException'):
            raise
        con.execute(sql, [json.dumps([[_clean(v) for v in row] for row in rows], ensure_ascii=False)])


def load_text_rows(con, name, rows, chunk_rows=None, keep=None, check=None):
    """Rows of text, header first -> text table `name`. Returns (column names, rows loaded).

    keep:  function(row number from 0) -> bool; rows it refuses are counted but not stored
    check: called between chunks (cancel, time budget)
    """
    rows = iter(rows)
    header = next(rows, None)
    if not header:
        return [], 0
    names = unique_names(header)
    width = len(names)
    create_text_table(con, name, names)
    chunk_rows = chunk_rows or config.READ_CHUNK_ROWS
    total, chunk = 0, []
    for row in rows:
        if all(v is None or v == '' for v in row):
            continue
        if keep is None or keep(total):
            cells = [None if v is None or v == '' else str(v) for v in row[:width]]
            chunk.append(cells + [None] * (width - len(cells)))
        total += 1
        if len(chunk) >= chunk_rows:
            if check:
                check()
            insert_rows(con, name, width, chunk)
            chunk = []
    insert_rows(con, name, width, chunk)
    return names, total


def csv_view(con, path, name='src'):
    """A view over a delimited text file, every column as text, read where the file is."""
    con.read_csv(str(path), **CSV_OPTIONS).create_view(name)
    return [r[0] for r in con.execute(f'DESCRIBE {q.quote_duck(name)}').fetchall()]


def _fits(column, kind):
    """How many values of a text column can be read as `kind`."""
    x = q.col(column)
    if kind == 'date':                                  # spelled as a date AND a real one: a text with a time is a timestamp
        return sa.func.count(sa.case((sa.func.regexp_full_match(sa.func.trim(x), q.SPELLING[kind]), q.TryCast(x, kind))))
    if kind in q.SPELLING:
        return sa.func.count(sa.case((sa.func.regexp_full_match(sa.func.trim(x), q.SPELLING[kind]), 1)))
    return sa.func.count(q.TryCast(x, kind))


def typed_table(con, raw, name, columns, candidates=None, empty=None, where=None, drop=True):
    """Text table `raw` -> table `name` with typed columns.

    candidates: {column: [kinds to try, in order]}; default: FILE_KINDS for every column. A column
                gets the first kind that every one of its non-empty values can be read as.
    empty:      the kind of a column with no value at all (None: it stays text)
    where:      a Core condition on the raw table (a random sample of its rows)
    Returns ([(column, DuckDB type)], {column: kind or None}).
    """
    table = text_table(raw, columns)
    candidates = candidates if candidates is not None else {c: FILE_KINDS for c in columns}
    checks, layout = [], []
    for c in columns:
        kinds = [k for k in candidates.get(c) or ()]
        layout.append((c, kinds, len(checks)))
        checks.append(sa.func.count(q.col(c)))
        checks += [_fits(c, kind) for kind in kinds]
    counts = execute(con, sa.select(*checks).select_from(table)).fetchone() if checks else ()
    chosen, select = {}, []
    for c, kinds, at in layout:
        filled = counts[at]
        best = None
        if not filled:
            best = (empty.get(c) if isinstance(empty, dict) else empty) if kinds else None
        else:
            best = next((kind for i, kind in enumerate(kinds) if counts[at + 1 + i] == filled), None)
        chosen[c] = best
        select.append(q.col(c) if best is None else q.TryCast(sa.func.trim(q.col(c)), best).label(q.ident(c)))
    statement = sa.select(*select).select_from(table)
    if where is not None:
        statement = statement.where(where)
    sql, parameters = q.compile_for_duckdb(statement)
    con.execute(f'CREATE TABLE {q.quote_duck(name)} AS ' + sql, parameters)
    if drop:
        con.execute(f'DROP TABLE {q.quote_duck(raw)}')
    described = [(r[0], r[1]) for r in con.execute(f'DESCRIBE {q.quote_duck(name)}').fetchall()]
    return described, chosen
