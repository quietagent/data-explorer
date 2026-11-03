"""Profile one table: row count, sampling, per-column statistics, top values, histograms.

What a database table costs its database:

    1. a row-count step     the catalog's estimate (one statement per schema, read before the job
                            starts on its tables), or - when there is none - the bounded read below
    2. ONE read             read_table(): the columns that can be profiled, for the whole table when
                            it is small, for a sample when it is big, never more than a fixed
                            number of rows. Large and binary columns are not fetched.
                            (Oracle only: one more read when a block sample came back nearly empty.)

Everything else - counts, min / max, top values, histograms, the scan for personal values - is
worked out by profile_copy() on the in-memory copy of that read. Nothing is retried against the
database: not column by column, and not after a timeout.

The results are plain dictionaries. They come out of profile_copy() unmasked; mask_result() hides
the values of personal columns, and the service calls it at the moment of saving, with the marks
as they are then. Sample rows are never part of a result.
"""

from __future__ import annotations

import math
import random
import time
from datetime import date, datetime, time as time_of_day
from decimal import Decimal

from .. import config
from ..jobs import Cancelled
from . import queries as q
from .data import DatabaseData, DuckTable, QueryFailed, SampleData
from .personal import mask_stats, personal_by_values


class TimedOut(Exception):
    """The table's time budget is used up; raised between two steps. The table is marked "timed out"."""


def default_settings(**overrides):
    return {'sample_rows': config.SAMPLE_TARGET_ROWS, 'cap_factor': config.SAMPLE_CAP_FACTOR,
            'retry_below': config.SAMPLE_RETRY_BELOW, 'chunk_rows': config.READ_CHUNK_ROWS,
            'row_sample_max_bytes': config.ORACLE_ROW_SAMPLE_MAX_BYTES,
            'block_sample': False,      # Oracle: sample whole blocks whatever the size (the first look for links by values)
            'stretch_rows': config.SAMPLE_STRETCH_ROWS, 'min_stretches': config.SAMPLE_MIN_STRETCHES,
            'scan_rows': config.PERSONAL_SCAN_ROWS, 'top': config.PROFILE_TOP_VALUES,
            'bins': config.PROFILE_HISTOGRAM_BINS, 'chunk': config.PROFILE_STATS_CHUNK, **overrides}


def budget_check(seconds, also=None, clock=time.monotonic):
    """A check() for one table: raises TimedOut when `seconds` have passed (and runs `also` first)."""
    deadline = clock() + seconds

    def check():
        if also is not None:
            also()
        if clock() > deadline:
            raise TimedOut(f'timed out after {seconds} s')
    return check


# ----------------------------------------------------------------- values ----

def jsonable(value, max_chars=None):
    """A database value as something JSON can hold."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 53 else str(value)     # a browser cannot hold more digits than that
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return jsonable(int(value)) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S') if value.time() != time_of_day(0) else value.strftime('%Y-%m-%d')
    if isinstance(value, (date, time_of_day)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f'({len(bytes(value)):,} bytes)'
    text = str(value)
    limit = max_chars or config.PROFILE_VALUE_MAX_CHARS
    return text if len(text) <= limit else text[:limit] + '…'


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _year_month(value):
    """(year, month) of a date-like value: a date, a datetime, or text that starts with YYYY-MM."""
    if isinstance(value, (date, datetime)):
        return value.year, value.month
    text = str(value or '')
    try:
        return int(text[:4]), int(text[5:7])
    except ValueError:
        return None


def known(estimate):
    """A catalog estimate that says something: 0, -1 and NULL all mean "not known"."""
    number = _number(estimate)
    return int(number) if number is not None and number > 0 else None


# --------------------------------------------------------------- the one read ----

def spread_plan(data, plans, target, check, settings):
    """SQLite: how to read about `target` rows spread over a table - or None when the table has no row number
    to go by (made WITHOUT ROWID, or it has columns called rowid, _rowid_ and oid).

    ONE small statement asks for the smallest and the largest row number (two look-ups at the ends of the
    table). The row numbers between the two are cut into equal parts, and the sample is the first few rows
    of each part: many short stretches at even distances (queries.spread_query).
    """
    rowid = q.rowid_name(p.name for p in plans)
    if rowid is None:
        return None
    check()
    try:
        low, high = data.run(q.rowid_bounds_query(data.schema, data.table, rowid))[0]
    except QueryFailed as exc:
        if exc.timeout:
            raise
        return None                                     # no such column: the table was made WITHOUT ROWID
    if low is None or high is None:
        return None
    low, span = int(low), int(high) - int(low) + 1
    per = max(1, min(int(settings['stretch_rows']), target // int(settings['min_stretches'])))
    stretches = max(1, target // per)
    return {'kind': 'spread', 'percent': None, 'seed': None, 'rowid': rowid, 'low': low, 'span': span,
            'stretches': stretches, 'stretch_rows': per, 'limit': stretches * per}


def read_table(data, plans, estimate=None, check=None, settings=None, only=None, plan=None, size=None, exact=None,
               count_note=None):
    """Read a database table, or a sample of it, into an in-memory copy. Returns the SampleData.

    data      DatabaseData
    plans     [ColumnPlan] of the table
    estimate  the catalog's row estimate (0, -1 and None mean: not known)
    only      read only these columns (a later look at some of them)
    plan      read the sample a stored profile describes again, instead of deciding anew
    size      Oracle: the table's size on disk in bytes, from the catalog (None: not known). Up to
              ORACLE_ROW_SAMPLE_MAX_BYTES - and when it is not known - a sample is taken by rows,
              SAMPLE (p): even, at the cost of reading every block once. Above it by blocks,
              SAMPLE BLOCK (p): cheap, but a few stretches of the table, and labelled so.
              settings['block_sample'] asks for blocks whatever the size, with no second read
              when they come back nearly empty (the caller decides what to do then).
    exact     the table's rows when they were counted (SQLite, whose catalog has no estimates: the
              service counts every table before it profiles). A SQLite table with more rows than
              the sample size is then read on a sample SPREAD over the whole table (spread_plan)
              instead of on its first rows.
    count_note  why the rows could not be counted, when that was tried (the count timed out)

    The copy carries what was learnt on the way: row_count and row_count_kind ('exact',
    'estimate', or 'more_than' when the table has more rows than were read and no usable
    estimate), sampled, method, random, plan, notes and count_note - what to say while only
    "more than" is known.
    """
    s = default_settings(**(settings or {}))
    check = check or (lambda: None)
    target, dialect = int(s['sample_rows']), data.dialect
    wanted = [p for p in plans if only is None or p.name in only]
    estimate = known(estimate)
    exact = None if exact is None else int(exact)
    can_sample = dialect in q.PERCENT_SAMPLING and not data.is_view
    notes = []

    def load(statement, limit):
        check()
        copy = SampleData(wanted)
        try:
            rows, more = copy.load(data.read(statement, s['chunk_rows']), limit, check)
        except BaseException:
            data.close()                                # an unfinished read leaves the connection mid-result
            raise
        return copy, rows, more

    def read(percent=None, seed=None, limit=None, block=False):
        return load(q.read_query(dialect, data.schema, data.table, wanted, percent, seed, limit + 1 if limit else None, block),
                    limit)

    def read_spread(p):
        return load(q.spread_query(data.schema, data.table, wanted, p['rowid'], p['low'], p['span'], p['stretches'],
                                   p['stretch_rows'], int(p['limit'])), int(p['limit']))

    seed = random.randrange(1, 2_000_000_000)
    if plan and plan.get('kind') == 'percent' and can_sample:
        cap = int(plan.get('limit') or target * s['cap_factor'])
        # a plan saved before the fix round has no 'block': those samples were block samples
        block = dialect == 'oracle' and bool(plan.get('block', True))
        copy, rows, more = read(plan['percent'], plan.get('seed') or 1, cap, block)
        copy.row_count, copy.row_count_kind = estimate, 'estimate' if estimate else None
        copy.sampled, copy.random = True, True
        copy.method = q.sample_text(dialect, plan['percent'], block=block)
        copy.plan = dict(plan)
    elif estimate is not None and estimate > target and can_sample and not plan:
        cap = target * int(s['cap_factor'])
        percent = round(min(99.0, max(q.MIN_PERCENT, 100.0 * target / estimate)), 4)
        by_size = dialect == 'oracle' and size is not None and size > s['row_sample_max_bytes']
        block = dialect == 'oracle' and (by_size or bool(s['block_sample']))
        copy, rows, more = read(percent, seed, cap, block)
        if by_size and rows < target * s['retry_below'] and percent < 99.0:
            # Block sampling picks whole blocks: on a table with few or uneven blocks it can come back
            # nearly empty. One more read with more blocks; never a third.
            higher = round(min(99.0, percent * (4.0 if rows == 0 else min(20.0, 1.2 * target / rows))), 4)
            copy.close()
            notes.append(f'SAMPLE BLOCK ({percent:g}%) gave only {rows:,} rows; read once more with {higher:g}%.')
            percent = higher
            copy, rows, more = read(percent, seed, cap, block)
        if by_size:
            notes.append(f'This table is about {size_text(size)}, more than the {size_text(s["row_sample_max_bytes"])} '
                         f'up to which rows are sampled (DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB), so whole blocks were '
                         f'sampled: {q.BLOCK_SAMPLE}.')
        elif block:
            notes.append(f'Whole blocks were sampled, as asked: {q.BLOCK_SAMPLE}.')
        if more:
            # Not proof that the estimate is wrong: a sample of blocks varies a lot in size (Oracle takes
            # whole runs of blocks - on the 2,000,000-row test table 5% gave anything from 0 to 205,000 rows).
            notes.append(f'The sample was cut at {cap:,} rows: {q.sample_text(dialect, percent, block=block).split(":")[0]} '
                         f'gave more than the about {target:,} rows asked for. '
                         + ('A sample of blocks varies in size; or the table ' if dialect != 'oracle' or block else 'The table ')
                         + f'is larger than its estimate of {estimate:,} rows says.')
        copy.row_count, copy.row_count_kind = estimate, 'estimate'
        copy.sampled, copy.random = True, True
        copy.method = q.sample_text(dialect, percent, block=block)
        copy.plan = {'kind': 'percent', 'percent': percent, 'limit': cap, 'seed': seed}
        if dialect == 'oracle':
            copy.plan['block'] = block
    else:
        copy = spread = None
        if plan and plan.get('kind') == 'spread' and dialect == 'sqlite' and not data.is_view:
            spread = dict(plan)                         # the sample a stored profile describes, read again
        elif not plan and dialect == 'sqlite' and not data.is_view and exact is not None and exact > target:
            # SQLite has no sampling clause. Its first rows are the oldest ones of a table filled in time order:
            # a sample spread over the table's row numbers shows all of it, and reads no more rows.
            spread = spread_plan(data, plans, target, check, s)
        if spread:
            copy, rows, more = read_spread(spread)
            if not plan and rows < target * s['retry_below']:
                # Row numbers that lie in a bunch (not spread from the first to the last) leave most parts
                # empty. One more read, of the first rows, labelled as that; never a third.
                copy.close()
                copy = None
                notes.append(f'A sample spread over the table gave only {rows:,} rows: its row numbers are bunched together. '
                             f'The first rows were read instead.')
        if copy is not None:
            copy.row_count, copy.row_count_kind = (exact, 'exact') if exact is not None else (rows, 'more_than')
            copy.sampled, copy.random = True, False
            copy.method = q.spread_text(spread['stretches'], spread['stretch_rows'])
            copy.plan = spread
        else:
            # Small, of unknown size, a view, or an engine with no sampling clause: the first rows.
            limit = int((plan or {}).get('limit') or target)
            copy, rows, more = read(limit=limit)
            if not more:
                copy.row_count, copy.row_count_kind = rows, 'exact'     # the read reached the end of the table
                copy.sampled, copy.random, copy.method = False, None, None
                copy.plan = {'kind': 'table', 'percent': None, 'limit': limit, 'seed': None}
            else:
                copy.sampled, copy.random = True, False
                copy.method = q.sample_text(dialect, limit=limit)
                copy.plan = {'kind': 'first', 'percent': None, 'limit': limit, 'seed': None}
                if exact is not None and exact > limit:
                    copy.row_count, copy.row_count_kind = exact, 'exact'        # counted before the read
                elif estimate is not None and estimate > limit:
                    copy.row_count, copy.row_count_kind = estimate, 'estimate'
                else:
                    # Kept apart from the notes: it is only true until somebody counts the table ("Exact count").
                    copy.row_count, copy.row_count_kind = limit, 'more_than'
                    if count_note:
                        copy.count_note = f'The table has more than {limit:,} rows; {count_note}'
                    elif estimate is None:
                        copy.count_note = (f'The table has more than {limit:,} rows; how many is not known (the database '
                                           f'has no row estimate for it). "Exact count" counts them.')
                    else:
                        copy.count_note = (f'The table has more than {limit:,} rows; the estimate of {estimate:,} rows '
                                           f'is out of date. "Exact count" counts them.')
    copy.notes = notes + copy.notes
    copy.statements = data.queries
    return copy


# ------------------------------------------------------------------ profile ----

def profile_copy(copy, check=None, settings=None):
    """Statistics of an in-memory copy (a database sample, or a file).

    Returns {name: stats} for every column of copy.plans, with plain values that are NOT masked
    yet and 'detected' - the personal category the values point to, if any.
    """
    s = default_settings(**(settings or {}))
    check = check or (lambda: None)
    plans = copy.plans
    source = q.table_source(q.DUCKDB, None, copy.table)
    is_file = not isinstance(copy, SampleData)

    def run(statement):
        check()
        return copy.run(statement)

    # 1. counts, min / max, lengths - a few columns per statement
    stats = {c.name: {'type': c.type, 'family': c.family} for c in plans}
    seen_rows = None

    def read_stats(chunk):
        nonlocal seen_rows
        statement, fields = q.stats_query(source, chunk, mixed=is_file)
        row = run(statement)[0]
        if seen_rows is None:
            seen_rows = int(row[0] or 0)
        for (name, what), value in zip(fields, row[1:]):
            stats[name][what] = value

    for position in range(0, len(plans), s['chunk']):
        chunk = plans[position:position + s['chunk']]
        try:
            read_stats(chunk)
        except QueryFailed:
            for column in chunk:                        # one odd column must not hide the others
                try:
                    read_stats([column])
                except QueryFailed as one:
                    stats[column.name]['error'] = str(one)
    if seen_rows is None:
        seen_rows = int(run(q.count_query(source))[0][0])

    # 2. some rows of the copy: checked for values that look personal (never stored, never returned)
    readable = [c for c in plans if c.family != 'lob']
    detected = {}
    if readable and seen_rows:
        try:
            scan = run(q.rows_query(q.DUCKDB, None, copy.table, [c.name for c in readable], s['scan_rows']))
            for i, c in enumerate(readable):
                detected[c.name] = personal_by_values([row[i] for row in scan], c.name, c.family)
            del scan
        except QueryFailed as exc:
            copy.notes.append(f'The rows could not be checked for personal values ({exc}).')

    # 3. top values and histograms, one column at a time
    for c in plans:
        st = stats[c.name]
        nulls = int(st['nulls'] or 0) if st.get('nulls') is not None else (seen_rows if 'error' not in st else None)
        st['nulls'] = nulls
        st['rows'] = seen_rows
        st['detected'] = detected.get(c.name)
        if c.family == 'lob':
            st['note'] = 'Large or binary values: only the empty ones are counted.'
            continue
        if 'error' in st or not seen_rows:
            continue
        filled = seen_rows - (nulls or 0)
        st['distinct'] = int(st.get('distinct') or 0)
        if c.family == 'number':
            st['avg'] = _number(st.get('avg'))
        if filled and st['distinct'] == filled and filled > s['top']:
            st['unique'] = True                         # every value appears once: no top values to show
        elif filled:
            try:
                rows = run(q.top_values_query(source, c.name, s['top']))
                st['top'] = [[row[0], int(row[1])] for row in rows]
            except QueryFailed as exc:
                st['top_error'] = str(exc)
        if not filled:
            continue
        try:
            if c.family == 'number':
                st['histogram'] = _number_histogram(run, source, c, st, s['bins'])
            elif c.family == 'date':
                st['histogram'] = _date_histogram(run, source, c, st)
        except QueryFailed as exc:
            st['histogram_error'] = str(exc)

    # 4. plain values that JSON can hold
    for c in plans:
        st = stats[c.name]
        for key in ('min', 'max'):
            if key in st:
                st[key] = jsonable(st[key])
        if 'top' in st:
            st['top'] = [[jsonable(v), n] for v, n in st['top']]
        for key in ('min_length', 'max_length', 'number_like', 'date_like'):
            if st.get(key) is not None:
                st[key] = int(st[key])
        if st.get('histogram') is None:
            st.pop('histogram', None)
    return stats


def mask_result(columns, flagged):
    """Hide the values of personal columns in a profile, in place.

    columns  {name: stats} from profile_copy()
    flagged  {name: category or None} - decided by the caller at the moment the result is saved
    """
    for name, st in columns.items():
        category = flagged.get(name)
        hide = bool(category)
        st['personal'], st['masked'] = category, hide
        if hide:
            mask_stats(st, category)
    return columns


def table_summary(copy, started, database=True):
    """What is saved about the table itself."""
    return {
        'row_count': copy.row_count if database else copy.total_rows,
        'row_count_kind': copy.row_count_kind if database else 'exact',
        'rows_profiled': copy.loaded_rows, 'sampled': bool(copy.sampled), 'sample_method': copy.method,
        'sample_random': (copy.random if database else True) if copy.sampled else None,
        'sample_plan': copy.plan if database else {'kind': 'table'},
        'queries': getattr(copy, 'statements', 0),      # statements sent to the database for this table
        'local_queries': copy.queries,                  # statements run on the in-memory copy
        'elapsed_s': round(time.monotonic() - started, 2), 'notes': list(getattr(copy, 'notes', []) or []),
        # what to say while the table's rows are only known as "more than": shown until an exact count is there
        'count_note': getattr(copy, 'count_note', None),
    }


def profile_table(data, columns, estimate=None, personal=None, check=None, settings=None):
    """Profile one table from start to end: the one read (for a database), the statistics, the masks.

    data      DatabaseData, or a FileData that is already open
    columns   [ColumnPlan]
    estimate  the catalog's row estimate, if it has one
    personal  function(column name, category found from the values or None) -> category or None;
              the columns it names have their values masked in the result
    check     called before every step; raises Cancelled or TimedOut to stop
    Returns {'table': {...}, 'columns': {name: {...}}}.

    The service does these steps itself, to decide about personal columns at the moment of saving.
    """
    started = time.monotonic()
    personal = personal or (lambda name, found: found)
    database = isinstance(data, DatabaseData)
    copy = read_table(data, columns, estimate, check, settings) if database else data
    try:
        if not database:
            copy.plans = list(columns)
            copy.notes = []
        found = profile_copy(copy, check, settings)
        summary = table_summary(copy, started, database)
    finally:
        if database:
            copy.close()
    stats = {source.name: found[plan.name] for source, plan in zip(columns, copy.plans)}
    mask_result(stats, {name: personal(name, st.get('detected')) for name, st in stats.items()})
    return {'table': summary, 'columns': stats}


def _number_histogram(run, source, column, st, bins):
    low, high = _number(st.get('min')), _number(st.get('max'))
    if low is None or high is None or high <= low:
        return None
    whole = q.is_whole(column.duck or column.type)      # whole numbers: bins start and end on whole numbers
    if whole:
        width = max(1, math.ceil((high - low + 1) / bins))
        count = math.ceil((high - low + 1) / width)
    else:
        width, count = (high - low) / bins, bins
    if width <= 0 or not math.isfinite(width):
        return None
    counts = [0] * count
    for bucket, n in run(q.number_histogram_query(source, column.name, low, width)):
        index = _number(bucket)
        if index is None:
            continue
        counts[max(0, min(count - 1, int(index)))] += int(n)
    out = []
    for i, n in enumerate(counts):
        start = low + i * width
        end = start + width - 1 if whole else start + width
        out.append({'from': jsonable(int(start) if whole else start), 'to': jsonable(int(end) if whole else end),
                    'count': n})
    return {'kind': 'number', 'whole': whole, 'bins': out}


def _date_histogram(run, source, column, st):
    first, last = _year_month(st.get('min')), _year_month(st.get('max'))
    if first is None or last is None:
        return None
    months = (last[0] - first[0]) * 12 + last[1] - first[1]
    by_month = months <= 36                             # over three years: one bar per year
    rows = run(q.date_histogram_query(source, column.name, by_month))
    counts = {}
    for row in rows:
        year = _number(row[0])
        month = _number(row[1]) if by_month else 1
        if year is None or month is None:
            continue
        counts[(int(year), int(month))] = counts.get((int(year), int(month)), 0) + int(row[-1])
    if not counts:
        return None
    out = []
    if by_month:
        year, month = first
        while (year, month) <= last and len(out) < 60:
            out.append({'label': f'{year:04d}-{month:02d}', 'count': counts.get((year, month), 0)})
            year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    elif last[0] - first[0] < 200:
        out = [{'label': str(year), 'count': counts.get((year, 1), 0)} for year in range(first[0], last[0] + 1)]
    else:
        out = [{'label': str(year), 'count': n} for (year, _), n in sorted(counts.items())]
    return {'kind': 'month' if by_month else 'year', 'bins': out}


# ------------------------------------------------------------- live reads ----

def sample_rows(data, columns, limit=None):
    """The first rows of a table, for display. Returns (column names, rows of plain values).
    Large and binary columns are left out. The caller masks the personal ones."""
    names = [c.name for c in columns if c.family != 'lob']
    if not names:
        return [], []
    rows = data.run(q.rows_query(data.dialect, data.schema, data.table, names, limit or config.PROFILE_SAMPLE_ROWS))
    return names, [list(row) for row in rows]


def live_values(copy, top=None, no_top=()):
    """Min, max and top values of every column of an in-memory copy, NOT masked - for the
    "show personal values" switch. no_top: columns whose values are all different."""
    source = q.table_source(q.DUCKDB, None, copy.table)
    plans = copy.plans
    out = {c.name: {} for c in plans}
    ordered = [c for c in plans if c.family in ('number', 'text', 'date')]
    if ordered:
        row = copy.run(q.minmax_query(source, [c.name for c in ordered]))[0]
        for i, c in enumerate(ordered):
            out[c.name]['min'], out[c.name]['max'] = jsonable(row[2 * i]), jsonable(row[2 * i + 1])
    for c in plans:
        if c.family == 'lob' or c.name in no_top:
            continue
        try:
            rows = copy.run(q.top_values_query(source, c.name, top or config.PROFILE_TOP_VALUES))
            out[c.name]['top'] = [[jsonable(v), int(n)] for v, n in rows]
        except QueryFailed as exc:
            out[c.name]['top_error'] = str(exc)
    return out


def exact_count(data):
    """COUNT(*) of the whole table, under the statement timeout. The only full count the app runs,
    and only when the user asks for it."""
    return int(data.run(q.count_query(q.table_source(data.dialect, data.schema, data.table)))[0][0])


def bounded_count(data, limit):
    """Rows of a table, counted no further than limit + 1. Returns (rows, True if there are more)."""
    rows = int(data.run(q.bounded_count_query(data.dialect, data.schema, data.table, int(limit) + 1))[0][0])
    return min(rows, int(limit)), rows > int(limit)


class Estimates(dict):
    """{table name: estimated rows}. sizes: {table name: bytes on disk} where the same catalog read
    says so (Oracle) - what decides between row and block sampling. exact: {table: rows} for the
    tables an engine without estimates had to count (SQLite); count_notes: {table: why not} for
    those whose count did not finish."""

    def __init__(self, *args):
        super().__init__(*args)
        self.sizes, self.exact, self.count_notes = {}, {}, {}


def size_text(size):
    """A size on disk in words: 110 MB, 1.5 GB."""
    return f'{size / 1024 ** 3:.1f} GB' if size >= 1024 ** 3 else f'{size / 1024 ** 2:,.0f} MB'


def table_bytes(rows, blocks, row_length, block_bytes=None):
    """An Oracle table's size from its statistics: its blocks, or its rows x their average length -
    the larger of the two, so that a doubt ends with the cheaper read. None when neither is known."""
    block_bytes = block_bytes or config.ORACLE_BLOCK_BYTES
    by_blocks = (known(blocks) or 0) * block_bytes
    by_rows = (known(rows) or 0) * (known(row_length) or 0)
    return max(by_blocks, by_rows) or None


def catalog_estimates(db, schema):
    """{table name: estimated rows} from the engine's own statistics; {} when it has none.
    Tables whose estimate says nothing (0, -1, NULL: never analysed, or just created) are left out.
    Oracle: the answer also carries each table's size (see Estimates) - still the one statement."""
    out = Estimates()
    statement = q.estimate_query(db.profile.engine, schema)
    if statement is None:
        return out
    with db.connect() as conn:
        for name, rows, *more in conn.execute(statement).fetchall():
            number = known(rows)
            if number is not None:
                out[str(name)] = number
            if len(more) == 2:
                size = table_bytes(rows, *more)
                if size is not None:
                    out.sizes[str(name)] = size
    return out


__all__ = ['Cancelled', 'TimedOut', 'profile_table', 'profile_copy', 'read_table', 'mask_result', 'table_summary',
           'sample_rows', 'live_values', 'exact_count', 'bounded_count', 'catalog_estimates', 'jsonable', 'mask',
           'budget_check', 'default_settings', 'known', 'DuckTable']
