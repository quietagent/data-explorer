"""Relationship checks: is a relationship true in the data?

For every relationship of the merged model (declared, from joins, from names, accepted value
links), with one column or several:

    coverage      the share of the child's non-empty keys that exist in the parent
    orphans       child rows whose key is not in the parent, with up to 20 example keys
    null %        child rows whose key is empty
    cardinality   1-1, 1-N or N-N, from the data and the catalog, with the evidence

and, on request, links nobody declared: pairs of columns where the values of one are found in a
key of another table. The user accepts such a suggestion (it becomes a relationship of the kind
"values") or rejects it (it is hidden and not suggested again).

How it stays light on a database - the same rules as profiling:

  * a background job (app.jobs): progress, cancel, one job per workspace across profiling and this
  * the child is read ONCE per job: its key columns, with the same sampling as profiling
    (profiler.read_table), into an in-memory copy. Counts, empty keys and "rows per key" come
    from that copy
  * "Find links by values" reads every table once more before that, lightly: a sample of about
    RELATION_PROBE_ROWS rows (2,000) of the columns that could point somewhere, and one look-up
    per parent key for the values of all those columns together (_Run.first_look). Only the
    pairs that share values are then measured on the child's one read
  * when child and parent are tables of the same database and their key columns are of the same
    kind, the database counts the relationship itself, over the WHOLE child table: ONE anti-join
    statement (NOT EXISTS) gives the rows, the empty keys and the orphan rows, exactly; one more
    gives the example keys, only when there are orphans (WholeTable). The copy then only says
    how often a key repeats in the child
  * otherwise - key columns of different kinds, or a count that fails or runs out of time - the
    parent is asked "which of these keys do you have?": bound IN lists of 500 keys (Oracle's
    limit is 1,000), at most RELATION_MAX_KEYS distinct keys per relationship. Files are joined
    in memory, which costs no database anything: there every key is looked up. What is found on
    a sample, or on some of the keys, is a lower bound: the result says "at least" wherever it
    is shown (limits)
  * whether the parent key is unique comes from the catalog first (primary key, unique
    constraints, unique indexes). Only without such proof is the parent asked for a duplicate,
    once, under the time budget; if that does not answer the result says "unique not proven".
    Profiling's sampled distinct counts are never taken as proof
  * every statement goes through the guarded connection (ReadOnlyDatabase.connect()), and every
    relationship has a time budget. Out of time means "not checked (timeout)": nothing is tried
    again on the whole table
  * both tables must come from the same source; otherwise "not checked: tables are in different
    sources"

Values never leave memory, with one exception: the example keys of orphans are saved with the
result, masked when their column is personal - decided at the moment of saving, like profiling.
"""

from __future__ import annotations

import math
import re
import time
from datetime import date, datetime, time as time_of_day
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from sqlalchemy import inspect

from .. import config
from ..jobs import Busy, Cancelled
from ..model import VALUES, type_family
from ..profiling import queries as q
from ..profiling.data import DatabaseData, QueryFailed
from ..profiling.personal import mask, personal_by_values, tokens
from ..profiling.profiler import TimedOut, budget_check, catalog_estimates, jsonable, read_table
from ..security import ReadOnlyViolation, sanitize_error
from ..sources import SourceError
from ..sources.database import auto_indexes
from ..workspace import NeedsPassword, NotFound
from .store import AnalysisStore, rel_key

DIFFERENT_SOURCES = 'tables are in different sources'
NO_MATCH = object()                     # a child value that cannot be a value of the parent's column at all
CASE_BLIND = ('mssql', 'mysql')         # engines whose usual collations compare text without regard to case
GENERIC_WORDS = {'id', 'no', 'num', 'nbr', 'number', 'code', 'key', 'ref', 'fk', 'pk', 'cd'}
MIDNIGHT = time_of_day(0)
# The first look for links by values: a pair goes on to the full check with at least this many values of the child
# column, of which this share exists in the parent key. (The full check then asks for 80% or 95% of 1,000 values.)
FIRST_LOOK_VALUES, FIRST_LOOK_SHARE = 3, 0.5
# A key that "counts up from about 1": it starts no higher than this, and holds this share of 40 numbers taken at
# even distances between its smallest and its largest value.
SERIAL_STARTS_BY, SERIAL_PROBES, SERIAL_DENSITY = 1000, 40, 0.8


def numbers(values, whole=False):
    """True when every value is a number (and there is one): 7, 7.5 and Decimal('7') - not True, not '7'.
    whole: ... and a whole number: 7, 7.0, Decimal('7')."""
    seen = False
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            return False
        if whole and not isinstance(value, int) and (not math.isfinite(value) or value != int(value)):
            return False
        seen = True
    return seen


# ------------------------------------------------------------------- values ----

def to_family(value, family):
    """A child value as a value of the parent's column (so that '123' can be looked up in a number
    column), or NO_MATCH when it cannot be one."""
    if value is None:
        return NO_MATCH
    if family == 'number':
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                return NO_MATCH
            return int(value) if value.is_integer() else Decimal(repr(value))
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else value
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return NO_MATCH
        if not number.is_finite():
            return NO_MATCH
        return int(number) if number == number.to_integral_value() else number
    if family in ('date', 'datetime'):
        moment = _moment(value)
        if moment is None:
            return NO_MATCH
        if family == 'datetime':
            return moment
        # a column of days holds no time of day: 13:00 on that day is not one of its values
        return moment.date() if moment.time() == MIDNIGHT else NO_MATCH
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    if isinstance(value, datetime):
        return value.isoformat(sep=' ')
    return str(value)


def canon(value, family, fold=False):
    """A value in the one form both sides are compared in."""
    value = to_family(value, family)
    if value is NO_MATCH:
        return NO_MATCH
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        value = value.rstrip()                      # CHAR(n) columns come back padded
        return value.casefold() if fold else value
    return value


def bind_type(family, values):
    """The SQL type the looked-up values are bound with (so that 9,999,999,999 is not sent as a 32-bit integer)."""
    if family == 'number':
        return sa.BigInteger() if all(isinstance(v, int) and abs(v) < 2 ** 63 for v in values) else sa.Numeric()
    if family == 'date':
        return sa.Date()                            # a DATE column is asked for days, not for moments
    if family == 'datetime':
        return sa.DateTime()
    return sa.String()


def _moment(value):
    """A date-like value as a datetime without a time zone, or None."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value).strip()).replace(tzinfo=None)
    except ValueError:
        return None


def key_kind(type_name, engine):
    """How the values of a key column are compared, from its type as its source declares it:

    number     any number
    text       text - and whatever the app does not know
    date       days: DATE on SQL Server, PostgreSQL, MySQL and in files
    datetime   a day and a time: timestamps, and Oracle's DATE (it holds a time of day)

    SQLite has no date type. A column declared DATE or DATETIME there keeps the text (or the
    number) it was given, and two such columns are equal when what they store is equal - so they
    are compared as stored, as text. A key is always bound in the form its parent column stores.
    """
    kind = ' '.join((type_name or '').upper().split())
    family = type_family(kind) or {'number': 'number', 'date': 'date'}.get(q.family(kind, engine), 'text')
    if family != 'date':
        return family
    if engine == 'sqlite':
        return 'text'
    return 'date' if kind == 'DATE' and engine != 'oracle' else 'datetime'


def compare_class(type_name, engine):
    """What a key column may be compared with INSIDE the database, or None when the app is not sure.

    Two columns of the same class compare without an error and without a conversion nobody asked
    for, on every engine. Columns of different classes do not: comparing a number with text fails
    on SQL Server, Oracle and PostgreSQL and converts silently on MySQL and SQLite. Padded CHAR
    columns only count as the same class when they are of the very same type.
    """
    kind = ' '.join((type_name or '').upper().split())
    if not kind:
        return None
    if re.search(r'\b(UUID|UNIQUEIDENTIFIER)\b', kind):
        return 'uuid'
    family = q.family(kind, engine)
    if family == 'number':
        return 'number'
    if family == 'date':
        if engine == 'sqlite':
            return 'date'                           # stored as given, on both sides
        if re.search(r'\bWITH (LOCAL )?TIME ZONE|TIMESTAMPTZ|DATETIMEOFFSET', kind):
            return 'moment'                         # with a time zone: never mixed with one that has none
        return key_kind(kind, engine)
    if family == 'text':
        padded = engine != 'sqlite' and re.match(r'(NATIONAL )?(N?CHAR|CHARACTER|BPCHAR)\b(?! VARYING)', kind)
        return f'char:{kind}' if padded else 'text'
    return None


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ------------------------------------------------------------------- parents ----

class DatabaseParent:
    """The parent side in a live database: asked which keys it has, in bound IN lists."""
    whole = True                                    # the whole table is asked, not a sample of it

    def __init__(self, conn, schema, table, families, unique_sets, rows=None):
        self.conn, self.schema, self.table = conn, schema, table
        self.dialect = conn.dialect
        self.families = families                    # {COLUMN: family}
        self.unique_sets = unique_sets              # [(frozenset of COLUMNS, text)] from the catalog
        self.rows = rows
        self.statements = 0

    def family(self, column):
        return self.families.get(column.upper()) or 'text'

    def lookup(self, columns, keys, check):
        """{position in keys: rows the parent has for that key} - only for the keys it has."""
        families = [self.family(c) for c in columns]
        fold = self.dialect in CASE_BLIND
        wanted, index = [], {}
        for position, key in enumerate(keys):
            bound = tuple(to_family(v, f) for v, f in zip(key, families))
            if any(v is NO_MATCH for v in bound):
                continue                            # e.g. 'ABC' looked up in a number column: it is not there
            wanted.append(bound)
            index.setdefault(tuple(canon(v, f, fold) for v, f in zip(bound, families)), []).append(position)
        types = [bind_type(f, [k[i] for k in wanted]) for i, f in enumerate(families)]
        size = max(1, min(config.RELATION_IN_CHUNK, 2000 // max(1, len(columns))))
        found = {}
        for part in chunks(wanted, size):
            check()
            self.statements += 1
            for row in self.conn.run(q.lookup_query(self.dialect, self.schema, self.table, columns, part, types)):
                key = tuple(canon(v, f, fold) for v, f in zip(row[:-1], families))
                for position in index.get(key, ()):
                    found[position] = found.get(position, 0) + int(row[-1])
        return found

    def has_duplicates(self, columns, check):
        """True / False: whether some key occurs twice. One statement over the parent's key."""
        check()
        self.statements += 1
        return bool(self.conn.run(q.duplicates_query(self.dialect, self.schema, self.table, columns)))

    def bounds(self, column, check):
        """(smallest, largest) value of a key column: one statement, answered from the key's index."""
        check()
        self.statements += 1
        return tuple(self.conn.run(q.bounds_query(self.dialect, self.schema, self.table, column))[0])

    def close(self):
        pass                                        # the connection belongs to the job


class FileParent:
    """The parent side in a file: its key columns in an in-memory copy, joined there."""
    dialect = q.DUCKDB
    unique_sets = ()
    statements = 0
    max_keys = 2 ** 31 - 1                          # looking a key up costs no database anything: every key is looked up

    def __init__(self, copy):
        self.copy = copy
        self.whole = not copy.sampled
        self.rows = copy.total_rows
        self.families = {name.upper(): q.family(kind, q.DUCKDB) for name, kind in copy.columns}

    def family(self, column):
        return self.families.get(column.upper()) or 'text'

    def lookup(self, columns, keys, check):
        check()
        return self.copy.count_matches(columns, keys)

    def has_duplicates(self, columns, check):
        check()
        by_name = {name.lower(): name for name, _ in self.copy.columns}
        return self.copy.run(q.key_shape_query([by_name[c.lower()] for c in columns]))[0][1] > 1

    def bounds(self, column, check):
        check()
        by_name = {name.lower(): name for name, _ in self.copy.columns}
        return tuple(self.copy.run(q.minmax_query(q.table_source(q.DUCKDB, None, 'data'), [by_name[column.lower()]]))[0])

    def close(self):
        self.copy.close()


class WholeTable:
    """A relationship counted by the database itself, over the whole child table.

    Only made for a child and a parent in the same database whose key columns are of the same
    kind (compare_class). Both statements are read-only, go through the guarded connection and
    run under the statement timeout; the caller checks its time budget before each."""

    def __init__(self, conn, child, columns, parent, parent_columns, on_slow=None):
        self.conn, self.dialect = conn, conn.dialect
        self.child, self.columns = child, list(columns)                 # (schema, table), key columns
        self.parent, self.parent_columns = parent, list(parent_columns)
        self.on_slow = on_slow                      # called when the count ran out of time
        self.statements = 0

    def counts(self, check):
        """(rows, rows with an empty key, different keys without a parent, rows without a parent)."""
        check()
        self.statements += 1
        try:
            row = self.conn.run(q.orphan_count_query(self.dialect, self.child, self.columns, self.parent, self.parent_columns))[0]
        except QueryFailed as exc:
            if exc.timeout and self.on_slow is not None:
                self.on_slow()
            raise
        return tuple(int(v or 0) for v in row)

    def examples(self, check, limit):
        """[(key values, rows)] for the keys without a parent that have the most rows."""
        check()
        self.statements += 1
        rows = self.conn.run(q.orphan_examples_query(self.dialect, self.child, self.columns, self.parent, self.parent_columns,
                                                     limit))
        return [(list(row[:-1]), int(row[-1])) for row in rows]


def catalog_keys(db, schema):
    """{TABLE: [(frozenset of COLUMNS, what proves it)]} - what the catalog says is unique in a schema:
    primary keys, unique constraints and unique indexes. A few catalog reads, no table is touched."""
    out = {}

    def add(table, columns, text):
        columns = [c for c in columns or [] if c]
        if columns:
            out.setdefault(str(table).upper(), []).append((frozenset(c.upper() for c in columns), text))

    with db.connect() as conn:
        insp = inspect(conn)
        show = db._namer(conn.dialect)
        try:
            for (_, table), pk in insp.get_multi_pk_constraint(schema=schema).items():
                add(show(table), [show(c) for c in pk.get('constrained_columns') or []], 'primary key')
        except ReadOnlyViolation:
            raise
        except Exception:
            conn.rollback()
            db._begin_read_only(conn)
        try:
            for (_, table), constraints in insp.get_multi_unique_constraints(schema=schema).items():
                for constraint in constraints or []:
                    name = constraint.get('name')
                    add(show(table), [show(c) for c in constraint.get('column_names') or []],
                        f'unique constraint {show(name)}' if name else 'unique constraint')
        except ReadOnlyViolation:
            raise
        except Exception:
            conn.rollback()
            db._begin_read_only(conn)
        try:
            for (_, table), indexes in insp.get_multi_indexes(schema=schema, **auto_indexes(conn.dialect)).items():
                for index in indexes or []:
                    options = index.get('dialect_options') or {}
                    partial = any(key.endswith('_where') and value is not None for key, value in options.items())
                    columns = index.get('column_names') or []
                    if not index.get('unique') or partial or index.get('expressions') or None in columns:
                        continue                    # a partial or expression index proves nothing about the column
                    name = index.get('name')
                    add(show(table), [show(c) for c in columns], f'unique index {show(name)}' if name else 'unique index')
        except ReadOnlyViolation:
            raise
        except Exception:
            pass
    return out


def proof(unique_sets, columns):
    """The catalog fact that makes these columns unique together, or None."""
    wanted = {c.upper() for c in columns}
    for members, text in unique_sets or ():
        if members <= wanted:
            return text
    return None


# ----------------------------------------------------------------- one check ----

def measure(copy, columns, parent, parent_columns, check, child_unique=None, max_keys=None, examples=None, whole=None,
            not_whole=None):
    """Check one relationship on the child's in-memory copy against its parent.

    copy            the child's copy (its key columns are in it under the names in `columns`)
    parent          DatabaseParent / FileParent
    child_unique    the catalog fact that makes the child key unique, if there is one
    whole           a WholeTable when the database can count the relationship itself, over the whole
                    child table; not_whole says in words why it cannot
    Returns the result as a dict of numbers and texts, plus 'orphan_keys_raw': the example keys
    as they are (the caller masks them before anything is saved).

    With `whole` the rows, the empty keys and the orphan rows are those of the whole table, and
    nothing is looked up. Without it - or when its statement fails or runs out of time - they are
    those of the copy and of the keys that were looked up: a lower bound when the copy is a
    sample or holds more keys than are looked up, and the result says so ('exact' is False).
    """
    max_keys = max_keys or getattr(parent, 'max_keys', None) or config.RELATION_MAX_KEYS
    examples = config.RELATION_ORPHAN_EXAMPLES if examples is None else examples
    check()
    rows, empty = (int(v or 0) for v in copy.run(q.key_counts_query(columns))[0])
    check()
    distinct, child_max = (int(v or 0) for v in copy.run(q.key_shape_query(columns))[0])
    check()
    keyed = copy.run(q.key_rows_query(columns, max_keys))
    keys = [tuple(row[:-1]) for row in keyed]
    counts = [int(row[-1]) for row in keyed]

    counted = None
    if whole is not None:
        try:
            counted = whole.counts(check)
        except QueryFailed as exc:                  # the look-ups below still answer, for what was read
            not_whole = ('the count over the whole table ran out of time' if exc.timeout
                         else f'the count over the whole table failed ({exc})')
    found, raw, examples_from_copy = {}, None, counted is None
    if counted is not None:
        rows, empty, orphan_keys, orphan_rows = counted
        rows_checked = rows - empty
        if orphan_rows and examples:
            try:
                raw = whole.examples(check, examples)
            except QueryFailed:
                examples_from_copy = True           # the counts stand; the examples come from the keys that were read
        else:
            raw = []
    if raw is None:
        found = parent.lookup(parent_columns, keys, check) if keys else {}
        orphans = [(n, i) for i, n in enumerate(counts) if i not in found]
        try:                                        # the keys with the most rows first, then in the keys' own order
            orphans.sort(key=lambda item: (-item[0], keys[item[1]]))
        except TypeError:                           # values that cannot be compared with each other
            orphans.sort(key=lambda item: (-item[0], item[1]))
        raw = [(list(keys[i]), n) for n, i in orphans[:examples]]
        if counted is None:
            rows_checked = sum(counts)
            orphan_rows, orphan_keys = rows_checked - sum(n for i, n in enumerate(counts) if i in found), len(orphans)

    # is the parent key unique?  1. the catalog  2. what the look-ups saw  3. one question to the parent
    unique, how = None, 'unique not proven'
    fact = proof(parent.unique_sets, parent_columns)
    if fact:
        unique, how = True, f'unique: {fact} in the catalog'
    elif any(n > 1 for n in found.values()):
        unique, how = False, 'not unique: some of the looked-up keys occur more than once in the parent'
    else:
        try:
            if parent.has_duplicates(parent_columns, check):
                unique, how = False, 'not unique: a key occurs more than once in the parent'
            elif parent.whole:
                unique, how = True, 'unique: no key occurs twice in the parent (checked now; no constraint says so)'
            else:
                how = 'unique not proven: the parent was read on a sample'
        except (QueryFailed, TimedOut) as exc:
            if isinstance(exc, QueryFailed) and not exc.timeout:
                raise
            how = 'unique not proven: the check for duplicates ran out of time'

    whole_child = not copy.sampled
    child_fact = (f'{child_unique} on the child, in the catalog' if child_unique
                  else 'no key repeats in the child, and all its rows were read' if whole_child and child_max <= 1 else None)
    if not distinct:
        cardinality, proven, note = 'unknown', False, 'the child has no key value to judge by'
    elif unique is True and child_max > 1:
        cardinality, proven, note = '1-N', True, f'one parent row, up to {child_max:,} child rows for a key'
    elif unique is True and child_fact:
        cardinality, proven, note = '1-1', True, f'one parent row and one child row for a key ({child_fact})'
    elif unique is True:
        cardinality, proven, note = '1-1', False, ('no key repeats in the sample of the child; the whole table was '
                                                   'not read, so 1-N is not ruled out')
    elif unique is False and child_max > 1:
        cardinality, proven, note = 'N-N', True, f'keys repeat on both sides (up to {child_max:,} child rows for a key)'
    elif unique is False and child_fact:
        cardinality, proven, note = '1-N', True, 'reversed: the key repeats in the parent and not in the child'
    elif unique is False:
        cardinality, proven, note = 'unknown', False, 'the key repeats in the parent; none repeats in the sample of the child'
    else:
        cardinality, proven, note = 'unknown', False, (f'unique not proven; up to {child_max:,} child rows for a key'
                                                       if child_max > 1 else 'unique not proven')
    in_database = counted is not None
    sampled = bool(copy.sampled) and not in_database        # the counts are those of a sample
    partial = len(keys) < distinct and not in_database      # ... or of some of the keys
    return {
        'status': 'checked', 'reason': None,
        # 'database': counted inside the database over the whole child table. 'lookup': the child's keys were
        # read (all of them, or a sample) and looked up in the parent
        'method': 'database' if in_database else 'lookup', 'exact': not (sampled or partial), 'not_whole': not_whole,
        'sampled': sampled, 'sample_method': copy.method if sampled else None, 'rows': rows,
        'label': f'measured on a sample of {rows:,} rows' if sampled else f'measured on all {rows:,} rows',
        'null_rows': empty, 'null_share': (empty / rows) if rows else None,
        'keys_total': distinct, 'keys_checked': None if in_database else len(keys), 'rows_checked': rows_checked,
        'coverage': ((rows_checked - orphan_rows) / rows_checked) if rows_checked else None,
        'orphan_rows': orphan_rows, 'orphan_keys': orphan_keys,
        'orphan_keys_raw': raw, 'examples_from_copy': bool(in_database and examples_from_copy and orphan_rows),
        # what the key values themselves look like (phone numbers, IDs ...): decides masking, never saved
        'detected_raw': [personal_by_values([key[i] for key in keys], column) for i, column in enumerate(columns)],
        'parent_unique': unique, 'parent_unique_how': how, 'child_max_per_key': child_max,
        'cardinality': cardinality, 'cardinality_proven': proven, 'cardinality_note': note,
        # how often a key repeats in the child comes from the copy, also when the database did the counting
        'cardinality_on_sample': bool(copy.sampled), 'child_sample_method': copy.method if copy.sampled else None,
        'reversed': bool(unique is False and child_fact and cardinality == '1-N'),
        'partial_keys': partial, 'max_keys': max_keys,
    }


def not_checked(reason, code):
    """A relationship that was not measured, and why: "not checked: ..." / "not checked (timeout)"."""
    return {'status': 'not_checked', 'reason': reason, 'reason_code': code,
            'label': 'not checked (timeout)' if code == 'timeout' else f'not checked: {reason}'}


def share_text(part, whole):
    """A share as the Relations tab and the report both print it: one decimal where it says
    something, and never "0%" or "100%" unless it is exactly that - 54,986 of 55,406 rows is
    ">99.9%" and not "100%"."""
    if not whole or part is None:
        return ''
    if part <= 0:
        return '0%'
    if part >= whole:
        return '100%'
    return fraction_text(part / whole)


def fraction_text(share):
    """A share given as a fraction (0.9877, an accepted link's confidence), printed by the rule of
    share_text: "98.8%". shareText() in static/app.js is the same rule for the page."""
    if share is None:
        return ''
    if share <= 0:
        return '0%'
    if share >= 1:
        return '100%'
    if share < 0.001:
        return '<0.1%'
    if share > 0.999:
        return '>99.9%'
    text = f'{100 * share:.1f}'
    return (text[:-2] if text.endswith('.0') else text) + '%'


def sample_tag(method, rows):
    """A sample in three or four words, for a list row."""
    method = method or ''
    if method.startswith('first '):
        return f'first {rows:,} rows'
    if 'spread over' in method:
        return f'{rows:,} rows spread over the table'
    return f'sample of {rows:,} rows'


def limits(check):
    """Why the counts of a measured result are not those of the whole table: (short tag for the
    list row, the full sentences for the evidence). ('', []) when they are exact."""
    tags, notes = [], []
    rows = check.get('rows') or 0
    if check.get('partial_keys'):
        looked, total = check.get('keys_checked') or 0, check.get('keys_total') or 0
        tags.append(f'{looked:,} of {total:,} keys looked up')
        notes.append(f'{looked:,} of {total:,} distinct keys were looked up in the parent - at most '
                     f'{check.get("max_keys") or config.RELATION_MAX_KEYS:,} per relationship '
                     f'(DATA_EXPLORER_RELATION_KEYS). The orphan rows and the coverage are those of the keys looked up: '
                     f'the table has at least that many orphan rows.')
    if check.get('sampled'):
        tags.append(sample_tag(check.get('sample_method'), rows))
        notes.append(f'The child table was read on a sample of {rows:,} rows ({check.get("sample_method") or "a sample"}). '
                     f'Orphan rows, coverage and empty keys are those of the sample: the table has at least that many '
                     f'orphan rows.')
    if notes and check.get('not_whole'):
        notes.append(f'The database was not asked to count the whole table: {check["not_whole"]}.')
    return ' · '.join(tags), notes


def shown(check):
    """A measured result as it is printed in a list: the SAME texts in the Relations tab and in the
    report. None for a relationship that was not measured.

    Worked out when a result is shown, not when it is saved, so results saved by an older
    version (which had no 'exact') are printed by the same rules.
    """
    if (check or {}).get('status') != 'checked':
        return None
    rows, checked = check.get('rows') or 0, check.get('rows_checked') or 0
    orphans, empty = check.get('orphan_rows') or 0, check.get('null_rows') or 0
    tag, notes = limits(check)
    at_least = bool(tag)
    if check.get('method') == 'database':
        how = 'Counted inside the database over the whole child table (one statement)'
    elif check.get('sampled'):
        how = "The child's keys were read on a sample and looked up in the parent"
    else:
        how = "Every row of the child's keys was read, and the keys were looked up in the parent"
    return {
        'coverage': share_text(checked - orphans, checked) if checked else '',
        'coverage_title': f'{checked - orphans:,} of {checked:,} rows with a key have a parent' if checked else
                          'Every row of the child has an empty key',
        'orphans': ('≥ ' if at_least and orphans else '') + f'{orphans:,}',
        'empty_keys': share_text(empty, rows) if rows else '',
        'empty_title': f'{empty:,} of {rows:,} rows have an empty key',
        'exact': not at_least, 'tag': tag, 'notes': notes, 'how': how,
        'measured': check.get('label') or '',
    }


def probe_values(copy, columns, copied):
    """{COLUMN: up to RELATION_PROBE_VALUES different values} of a child's copy (copied: the names in the
    copy), for the first look. The copy is closed."""
    try:
        return {c.upper(): [row[0] for row in copy.run(q.key_rows_query([n], config.RELATION_PROBE_VALUES))]
                for c, n in zip(columns, copied)}
    finally:
        copy.close()


def overlap_text(link):
    """The overlap of a link found by values, printed by the same rule as a coverage."""
    evidence = link.get('evidence') or {}
    looked, found = evidence.get('values_checked'), evidence.get('values_found')
    if looked:
        return share_text(found or 0, looked)
    share = link.get('confidence')
    return '' if share is None else share_text(round(share * 10000), 10000)


def overlap(copy, column, parent, parent_column, check, limit=None):
    """How many of up to `limit` distinct values of a child column exist in a parent column.
    Returns (values looked up, values found, the most parent rows one value has)."""
    limit = limit or config.RELATION_SUGGEST_VALUES
    check()
    keyed = copy.run(q.key_rows_query([column], limit))
    keys = [tuple(row[:-1]) for row in keyed]
    found = parent.lookup([parent_column], keys, check) if keys else {}
    return len(keys), len(found), max(found.values(), default=0), keys


# ------------------------------------------------------------------- service ----

class Relations:
    def __init__(self, workspaces, profiling, jobs):
        self.ws, self.prof, self.jobs = workspaces, profiling, jobs
        self.store = AnalysisStore(workspaces._db)
        workspaces.extend_model.append(self._accepted_links)
        workspaces.on_change.append(self._model_changed)
        profiling.on_clear.append(self._cleared)
        profiling.on_mark.append(self._marked)

    # ---------------------------------------------------------------- hooks
    def _accepted_links(self, workspace_id, model):
        """Links the user accepted become relationships of the kind "values" in the merged model."""
        for link in self.store.links(workspace_id, 'accepted'):
            child, parent = model.find(link['from']), model.find(link['to'])
            if child is None or parent is None:
                continue
            if any(child.column(c) is None for c in link['from_columns']) or any(parent.column(c) is None for c in link['to_columns']):
                continue
            # The share as measured (values found of values checked), not rounded: the suggestion said 98.8%,
            # so the Kind column, the graph and the report say 98.8% too (fraction_text, shareText in app.js).
            evidence = link.get('evidence') or {}
            looked, found = evidence.get('values_checked'), evidence.get('values_found')
            share = (found or 0) / looked if looked else (link['confidence'] or 0.0)
            model.relate(child, link['from_columns'], parent, link['to_columns'], VALUES, share,
                         link['cardinality'] or 'unknown',
                         evidence=[f'{overlap_text(link)} of the values checked exist in {parent.name}; accepted by you'])

    def _model_changed(self, workspace_id, removed_source=None, deleted=False):
        """Results of relationships that no longer exist go; so do suggestions nobody decided on."""
        if deleted:
            return
        try:
            model = self.ws.model(workspace_id)
        except NotFound:
            return
        keys = {self.key_of(r) for r in model.relationships}
        self.store.drop_checks(workspace_id, keep=keys)
        gone = [link['key'] for link in self.store.links(workspace_id, 'suggested')
                if not self._link_exists(model, link)]
        self.store.drop_suggestions(workspace_id, gone)

    @staticmethod
    def _link_exists(model, link):
        child, parent = model.find(link['from']), model.find(link['to'])
        return (child is not None and parent is not None
                and all(child.column(c) is not None for c in link['from_columns'])
                and all(parent.column(c) is not None for c in link['to_columns']))

    def _cleared(self, workspace_id):
        """"Clear saved statistics": the check results hold example keys, so they go too."""
        self.store.drop_checks(workspace_id)
        self.store.drop_suggestions(workspace_id)

    def _marked(self, workspace_id, table, column, category):
        """A column was marked personal: mask the example keys saved for it."""
        if not category:
            return
        for key, result in self.store.checks(workspace_id).items():
            if not key.startswith(table.key + '('):
                continue
            columns = [c.upper() for c in result.get('orphan_columns') or []]
            if column.upper() not in columns:
                continue
            at = columns.index(column.upper())

            def masker(saved, at=at):
                for example in saved.get('orphan_examples') or []:
                    example['key'][at] = mask(example['key'][at], category)
                saved['examples_masked'] = True
                return saved
            self.store.mask_check(workspace_id, key, masker)

    # -------------------------------------------------------------- reading
    @staticmethod
    def key_of(r):
        return rel_key(r.from_table.id, r.from_columns, r.to_table.id, r.to_columns)

    def _targets(self, workspace_id, model):
        return {t.table.key: t for t in self.prof.targets(workspace_id, model.tables)}

    @staticmethod
    def _reason(child, parent):
        """Why a relationship cannot be checked, before anything is read; None if it can."""
        for side, target in (('child', child), ('parent', parent)):
            if target.source is None:
                return not_checked(f'{target.table.name} has no data ({target.reason})', 'no_data')
        if child.source['id'] != parent.source['id']:
            return not_checked(DIFFERENT_SOURCES, 'different_sources')
        return None

    def overview(self, workspace_id):
        """Every relationship with its last check, the suggestions, and the workspace's job."""
        model = self.ws.model(workspace_id)
        targets = self._targets(workspace_id, model)
        saved = self.store.checks(workspace_id)
        relationships = []
        for i, r in enumerate(model.relationships):
            key = self.key_of(r)
            blocked = self._reason(targets[r.from_table.key], targets[r.to_table.key])
            check = saved.get(key)
            if blocked is not None:
                check = blocked                     # e.g. a source was removed since: say why, not old numbers
            if check is not None:
                check = {**check, 'shown': shown(check)}    # what the list prints: the tab and the report, the same
            relationships.append({
                'id': f'r{i}', 'key': key, 'from': r.from_table.id, 'from_columns': list(r.from_columns),
                'to': r.to_table.id, 'to_columns': list(r.to_columns), 'kind': r.source, 'confidence': r.confidence,
                'model_cardinality': r.cardinality, 'name': r.name, 'checkable': blocked is None, 'check': check})
        links = [{**x, 'overlap_text': overlap_text(x)} for x in self.store.links(workspace_id)]
        job = self.jobs.running(workspace_id) or self.jobs.latest(workspace_id, 'relations')
        return {'job': job.to_dict() if job else None, 'relationships': relationships,
                'suggestions': [x for x in links if x['status'] == 'suggested' and self._link_exists(model, x)],
                'accepted': [x for x in links if x['status'] == 'accepted'],
                'rejected': [x for x in links if x['status'] == 'rejected'],
                'summary': self._summary(relationships),
                'settings': {'check_budget_s': config.RELATION_CHECK_BUDGET_S, 'max_keys': config.RELATION_MAX_KEYS,
                             'max_pairs': config.RELATION_MAX_PAIRS, 'in_chunk': config.RELATION_IN_CHUNK,
                             'suggest_values': config.RELATION_SUGGEST_VALUES,
                             'min_overlap': config.RELATION_SUGGEST_MIN_OVERLAP,
                             'sample_target_rows': config.SAMPLE_TARGET_ROWS}}

    @staticmethod
    def _summary(relationships):
        checked = [r for r in relationships if (r['check'] or {}).get('status') == 'checked']
        return {'relationships': len(relationships), 'checked': len(checked),
                'not_checked': sum(1 for r in relationships if (r['check'] or {}).get('status') == 'not_checked'),
                'never': sum(1 for r in relationships if not r['check']),
                'with_orphans': sum(1 for r in checked if r['check'].get('orphan_rows')),
                'sampled': sum(1 for r in checked if r['check'].get('sampled')),
                # counts that are a lower bound: measured on a sample, or on some of the keys
                'not_exact': sum(1 for r in checked if not (r['check'].get('shown') or shown(r['check']))['exact'])}

    def decide(self, workspace_id, key, action):
        """Accept or reject a suggested link, or put a decided one back ('reset')."""
        self.ws.get_workspace(workspace_id)
        status = {'accept': 'accepted', 'reject': 'rejected', 'reset': 'suggested'}.get(action)
        if status is None:
            raise SourceError('action must be accept, reject or reset.')
        if not self.store.decide(workspace_id, str(key or ''), status):
            raise NotFound('Suggestion not found.')
        self.ws.forget_model(workspace_id)          # an accepted link is part of the model from now on
        self._model_changed(workspace_id)
        link = next(x for x in self.store.links(workspace_id) if x['key'] == key)
        if status == 'accepted':
            link = {**link, **self._check_accepted(workspace_id, key)}
        return link

    def _check_accepted(self, workspace_id, key):
        """An accepted link is a relationship from now on: measure it at once, so that the list and
        the report do not show it as "never checked" until the next "Check all".
        {'job': the check that was started} - or {'job': None, 'not_checked_now': why not}."""
        try:
            return {'job': self.start(workspace_id, [key])}
        except Busy:
            why = 'another job is running in this workspace'
        except NeedsPassword as exc:
            why = str(exc)
        except (NotFound, SourceError) as exc:
            why = str(exc)
        return {'job': None, 'not_checked_now': why}

    # ----------------------------------------------------------------- jobs
    def start(self, workspace_id, relationships='all', suggest=False, passwords=None):
        """Start a check in the background. relationships: 'all', a list of keys or ids, or [] to
        only look for suggestions. Returns the job."""
        model = self.ws.model(workspace_id)
        for source_id, password in (passwords or {}).items():
            self.ws.set_password(workspace_id, str(source_id), password)
        if relationships in (None, 'all', '*'):
            wanted = list(model.relationships)
        else:
            if isinstance(relationships, str):
                relationships = [relationships]
            by_key = {self.key_of(r): r for r in model.relationships}
            by_key.update({f'r{i}': r for i, r in enumerate(model.relationships)})
            wanted = []
            for item in relationships:
                if str(item) not in by_key:
                    raise NotFound(f'Relationship not found: {item}')
                if by_key[str(item)] not in wanted:
                    wanted.append(by_key[str(item)])
        targets = self._targets(workspace_id, model)
        if not wanted and not suggest:
            raise SourceError('There is no relationship to check. Add sources whose tables relate, or look for '
                              'links by values.')
        used = [targets[t.key] for r in wanted for t in (r.from_table, r.to_table)]
        if suggest:
            used += [t for t in targets.values() if t.source is not None]
        self.prof.require_passwords(workspace_id, [t for t in used if t.source is not None])
        sources = {t.source['id'] for t in used if t.source is not None}
        job = self.jobs.start(workspace_id, 'relations', len(wanted), lambda job: self._run(job, model, wanted, targets),
                              sources=sources, phase='keys', suggest=bool(suggest))
        return job.to_dict()

    def _run(self, job, model, wanted, targets):
        run = _Run(self, job, model, targets)
        try:
            run.check_all(wanted, run.pairs() if job.options.get('suggest') else [])
        finally:
            run.close()


class _Run:
    """One job: its connections, what the catalog said, and the copies it has in memory."""

    def __init__(self, service, job, model, targets):
        self.service, self.job, self.model, self.targets = service, job, model, targets
        self.ws_id = job.workspace_id
        self.prof, self.store = service.prof, service.store
        self.dbs, self.conns = {}, {}               # source id -> ReadOnlyDatabase / the job's DatabaseData
        self.catalog = {}                           # (source id, schema) -> {TABLE: unique sets}
        self.estimates = {}                         # (source id, schema) -> {table: rows}
        self.file_parents = {}                      # table key -> FileParent
        self.slow = set()                           # child tables whose count over the whole table ran out of time
        self.serials = {}                           # (table key, COLUMN) -> whether that key counts up from about 1
        self.counts = service.prof.row_counts(self.ws_id)
        self.timeout_s = max(1, min(config.STATEMENT_TIMEOUT_S, config.RELATION_CHECK_BUDGET_S))

    # ------------------------------------------------------------ plumbing
    def close(self):
        for parent in self.file_parents.values():
            parent.close()
        for conn in self.conns.values():
            conn.close()
        for db in self.dbs.values():
            db.dispose()

    def db(self, source):
        return self.prof.database(self.ws_id, source, self.dbs, self.timeout_s)

    def conn(self, source):
        if source['id'] not in self.conns:
            self.conns[source['id']] = DatabaseData(self.db(source), None, source['label'])
        return self.conns[source['id']]

    def schema_facts(self, target):
        """The catalog's unique keys and row estimates of a database table's schema, read once per job."""
        source = target.source
        schema, _ = self.prof.names(target)
        key = (source['id'], schema)
        if key not in self.catalog:
            self.job.check()
            db = self.db(source)
            try:
                self.catalog[key] = catalog_keys(db, schema)
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                self.catalog[key] = {}
                self.job.notes.append(f'{source["label"]}: the keys of {schema or "the default schema"} could not be read '
                                      f'from the catalog ({db.clean_error(exc)}); uniqueness is checked on the data.')
            try:
                self.estimates[key] = catalog_estimates(db, schema) if q.estimate_query(db.profile.engine, 'x') is not None else {}
            except ReadOnlyViolation:
                raise
            except Exception:
                self.estimates[key] = {}
        return self.catalog[key], self.estimates[key]

    def unique_sets(self, target):
        """What makes columns of this table unique: the catalog (a database), or declared keys (DDL)."""
        table = target.table
        sets = []
        declared = [c.name for c in table.columns if c.pk]
        if declared:
            sets.append((frozenset(c.upper() for c in declared), 'primary key'))
        if target.source is not None and target.source['kind'] == 'database':
            catalog, _ = self.schema_facts(target)
            _, name = self.prof.names(target)
            sets = list(catalog.get(name.upper(), [])) or sets
        elif target.source is not None:
            sets = []                               # a file declares nothing; a name is not a proof
        return sets

    def source_names(self, target, columns):
        """The columns as the source itself spells them (the merged model may carry another spelling)."""
        out = []
        for column in columns:
            found = target.source_table.column(column)
            if found is None:
                raise QueryFailed(f'{target.table.name}.{column} is not in {target.source["label"]}; read the source again')
            out.append(found.name)
        return out

    @staticmethod
    def engine(target):
        return target.source['settings'].get('engine') if target.source['kind'] == 'database' else q.DUCKDB

    def families(self, target):
        """{COLUMN: how its values are compared} - the form a key is bound in when this table is the parent."""
        engine = self.engine(target)
        return {c.name.upper(): key_kind(c.types.get('db') or c.type, engine) for c in target.source_table.columns}

    def whole_table(self, r, child, parent_target):
        """(WholeTable, None) when the database can count this relationship itself over the whole child
        table - or (None, why not, in words)."""
        if child.source['kind'] != 'database':
            return None, None                       # files: there is no database to ask; every key is looked up in memory
        if not config.RELATION_WHOLE_TABLE:
            return None, 'counting inside the database is switched off (DATA_EXPLORER_RELATION_WHOLE_TABLE)'
        if child.table.key in self.slow:
            return None, 'an earlier count over this table ran out of time in this run'
        engine = self.engine(child)
        names, parent_names = self.source_names(child, r.from_columns), self.source_names(parent_target, r.to_columns)
        for a, b in zip(names, parent_names):
            kinds = [found.types.get('db') or found.type for found in (child.source_table.column(a), parent_target.source_table.column(b))]
            classes = [compare_class(kind, engine) for kind in kinds]
            if classes[0] is None or classes[0] != classes[1]:
                return None, (f'{child.table.name}.{a} is {kinds[0] or "of an unknown type"} and {parent_target.table.name}.{b} is '
                              f'{kinds[1] or "of an unknown type"}, and columns of different types are not compared inside '
                              f'the database')
        return WholeTable(self.conn(child.source), self.prof.names(child), names, self.prof.names(parent_target), parent_names,
                          on_slow=lambda: self.slow.add(child.table.key)), None

    def key_family(self, target, column):
        """'number' or 'text' for a column that can be compared as a key; None for large, binary, date
        and other columns."""
        found = target.source_table.column(column.name)
        kind = (found.types.get('db') or found.type) if found is not None else column.type
        if q.family(kind, self.engine(target)) == 'lob':
            return None
        family = type_family(kind)
        return family if family in ('number', 'text') else None

    def child_copy(self, target, columns, check, sample_rows=None, blocks=False):
        """The child's key columns, read once: the same sampling as profiling.
        sample_rows: read only about this many rows (the first look for links by values).
        blocks: on Oracle, sample whole blocks (SAMPLE BLOCK) whatever the table's size (the first look)."""
        names = self.source_names(target, columns)
        if target.source['kind'] != 'database':
            return self.prof.file_table(target, only=names, limit=sample_rows).open(check), names
        data, plans = self.prof.database_table(self.ws_id, target, self.dbs, self.timeout_s)
        if data.dialect == 'sqlite':
            for plan in plans:                      # a key is compared as SQLite stores it (key_kind): a date stays the
                if plan.family == 'date' and plan.name in names:    # text it is, and is not read as a timestamp
                    plan.family = 'text'
        _, estimates = self.schema_facts(target)
        _, table = self.prof.names(target)
        rows, kind = self.counts.get(target.table.key, (None, None))
        try:
            # a counted SQLite table is read on a sample spread over it, like its profile (read_table)
            copy = read_table(data, plans, estimates.get(table), check, only=set(names),
                              settings={'sample_rows': sample_rows, 'block_sample': blocks} if sample_rows else None,
                              size=getattr(estimates, 'sizes', {}).get(table), exact=rows if kind == 'exact' else None)
        finally:
            data.close()
        by_source = {plan.name: copy_plan for plan, copy_plan in zip(copy.source_plans, copy.plans)}
        large = [n for n in names if by_source[n].family == 'lob']
        if large:
            copy.close()
            raise QueryFailed(f'{target.table.name}.{large[0]} is a large or binary column; it cannot be a key')
        return copy, [by_source[n].name for n in names]

    def parent(self, target, columns, check):
        if target.source['kind'] != 'database':
            if target.table.key not in self.file_parents:
                copy = self.prof.file_table(target, threshold=float('inf')).open(check)     # the whole file: every key counts
                self.file_parents[target.table.key] = FileParent(copy)
            return self.file_parents[target.table.key]
        schema, table = self.prof.names(target)
        return DatabaseParent(self.conn(target.source), schema, table, self.families(target), self.unique_sets(target),
                              self.counts.get(target.table.key, (None,))[0])

    # -------------------------------------------------------------- checking
    def check_all(self, wanted, pairs=()):
        """The job's loop. Every child table is read ONCE: the key columns of its relationships and
        the columns of its candidate pairs together. Then its relationships are checked and its
        pairs tried, each under its own time budget."""
        job = self.job
        by_child = {}
        for r in wanted:
            by_child.setdefault(r.from_table.key, ([], []))[0].append(r)
        for pair in pairs:
            by_child.setdefault(pair[1].table.key, ([], []))[1].append(pair)
        job.phase = 'checking'
        for child_key, (relationships, candidates) in by_child.items():
            job.check()
            child = self.targets[child_key]
            ready, columns = [], []

            def need(names):
                columns.extend(c for c in names if c.upper() not in {x.upper() for x in columns})
            for r in relationships:
                blocked = self.service._reason(child, self.targets[r.to_table.key])
                if blocked is not None:
                    self.save(r, blocked)
                    job.done += 1
                    continue
                ready.append(r)
                need(r.from_columns)
            for pair in candidates:
                need([pair[2].name])
            if not ready and not candidates:
                continue
            job.current = child.table.id
            started = time.monotonic()
            check = budget_check(config.RELATION_CHECK_BUDGET_S, also=job.check)
            try:
                copy, copied = self.child_copy(child, columns, check)
            except Cancelled:
                raise
            except Exception as exc:
                for r in ready:
                    self.save(r, self.failure(r, exc))
                if candidates and not ready:
                    job.fail(child.table.id, self.message(exc), timeout=self.message(exc) == 'not checked (timeout)')
                job.done += len(ready) + len(candidates)
                continue
            names = {c.upper(): n for c, n in zip(columns, copied)}
            try:
                read_s = time.monotonic() - started
                for r in ready:
                    job.check()
                    job.phase = 'checking'
                    job.current = f'{r.from_table.name} → {r.to_table.name}'
                    self.save(r, self.check_one(r, child, copy, [names[c.upper()] for c in r.from_columns], read_s))
                    job.done += 1
                for score, _, column, parent_target, key_column, text, why, key in candidates:
                    job.check()
                    job.phase = 'suggesting'
                    job.current = f'{child.table.name}.{column.name} ~ {parent_target.table.name}.{key_column.name}'
                    check = budget_check(config.RELATION_CHECK_BUDGET_S, also=job.check)
                    try:
                        self.try_pair(copy, names[column.name.upper()], child, column, parent_target, key_column, text, why,
                                      score, key, check)
                    except Cancelled:
                        raise
                    except (TimedOut, QueryFailed, SourceError, ReadOnlyViolation) as exc:
                        if self.message(exc) != 'not checked (timeout)':        # a pair that ran out of time is just not suggested
                            job.fail(job.current, self.message(exc))
                    job.done += 1
            finally:
                copy.close()

    def check_one(self, r, child, copy, columns, read_s):
        """One relationship, under its own time budget."""
        started = time.monotonic()
        check = budget_check(config.RELATION_CHECK_BUDGET_S, also=self.job.check)
        parent_target = self.targets[r.to_table.key]
        try:
            parent = self.parent(parent_target, r.to_columns, check)
            statements = parent.statements
            whole, not_whole = self.whole_table(r, child, parent_target)
            result = measure(copy, columns, parent, self.source_names(parent_target, r.to_columns), check,
                             child_unique=proof(self.unique_sets(child), r.from_columns), whole=whole, not_whole=not_whole)
            # the child's one read, then the count inside the database and / or the look-ups
            result['statements'] = (parent.statements - statements) + (whole.statements if whole else 0) + 1
            result['elapsed_s'] = round(time.monotonic() - started + read_s, 2)
            return result
        except Cancelled:
            raise
        except Exception as exc:
            return self.failure(r, exc)

    def failure(self, r, exc):
        """A check that did not finish, as a result. A timeout is "not checked (timeout)"."""
        if isinstance(exc, TimedOut) or (isinstance(exc, QueryFailed) and exc.timeout):
            self.job.fail(f'{r.from_table.id} → {r.to_table.id}', 'not checked (timeout)', timeout=True)
            return not_checked('timeout', 'timeout')
        if isinstance(exc, ReadOnlyViolation):
            message = f'Blocked by the read-only guard: {exc}'
        elif isinstance(exc, (QueryFailed, SourceError, NeedsPassword)):
            message = str(exc)
        else:
            message = f'{type(exc).__name__}: {exc}'
        message = sanitize_error(message, limit=300)
        self.job.fail(f'{r.from_table.id} → {r.to_table.id}', message)
        return not_checked(message, 'error')

    def save(self, r, result):
        """Mask the example keys of personal columns - with the marks as they are now - and save."""
        result = dict(result)
        raw, seen = result.pop('orphan_keys_raw', None), result.pop('detected_raw', None)
        with self.service.jobs.saving(self.job):
            with self.prof._marks:
                if raw is not None:
                    marks = self.prof.store.marks(self.ws_id)
                    stats = self.prof.store.columns(self.ws_id, r.from_table.key).get(r.from_table.key, {})
                    categories = [self.prof.personal_category(
                        marks, r.from_table, c, (stats.get(c.upper()) or {}).get('detected') or (seen or [None] * 9)[i])
                        for i, c in enumerate(r.from_columns)]
                    result['orphan_columns'] = list(r.from_columns)
                    result['examples_masked'] = any(categories)
                    result['orphan_examples'] = [
                        {'key': [mask(v, category) if category else jsonable(v) for v, category in zip(key, categories)],
                         'rows': n} for key, n in raw]
                self.store.save_check(self.ws_id, self.service.key_of(r), result)

    # ------------------------------------------------------------ suggestions
    def candidates(self):
        """Column pairs worth trying: (score, child target, child column, parent target, parent column, what makes
        the parent a key, the hint in the names, the pair's key).

        The parent column is a key or proven unique; both columns hold numbers or both hold text;
        the pair is not related already and was not decided on before."""
        model = self.model
        related, pointing = set(), set()
        for r in model.relationships:
            for a, b in zip(r.from_columns, r.to_columns):
                related.add(frozenset(((r.from_table.key, a.upper()), (r.to_table.key, b.upper()))))
            pointing.update((r.from_table.key, c.upper()) for c in r.from_columns)
        decided = {link['key'] for link in self.store.links(self.ws_id) if link['status'] != 'suggested'}
        profiles = self.prof.store.tables(self.ws_id)
        parents = []
        for target in self.targets.values():
            if target.source is None:
                continue
            table = target.table
            keys = {}
            for members, text in self.unique_sets(target):
                if len(members) == 1:
                    keys[next(iter(members))] = text
            if target.source['kind'] == 'database':
                rows = self.counts.get(table.key, (None,))[0]
                if rows is not None and rows <= config.SAMPLE_THRESHOLD_ROWS:
                    for c in table.columns:         # a key by its name: only on tables small enough to ask without an index
                        if c.pk_inferred and c.name.upper() not in keys:
                            keys[c.name.upper()] = 'looks like the key by its name'
            else:
                profile = (profiles.get(table.key) or {}).get('profile') or {}
                saved = self.prof.store.columns(self.ws_id, table.key).get(table.key, {})
                for c in table.columns:
                    st = saved.get(c.name.upper()) or {}
                    if c.pk_inferred:
                        keys.setdefault(c.name.upper(), 'looks like the key by its name')
                    elif profile and not profile.get('sampled') and st.get('rows') and st.get('nulls') == 0 \
                            and st.get('distinct') == st.get('rows') and st.get('rows') > 1:
                        keys.setdefault(c.name.upper(), 'every row of the file has a different value')
            for column_key, text in keys.items():
                column = table.column(column_key)
                family = self.key_family(target, column) if column is not None else None
                if family:
                    parents.append((target, column, family, text))
        out = []
        prefixes = table_prefixes(t.name for t in model.tables)      # SUB_, CRM_ ...: no name hint by themselves
        for child in self.targets.values():
            if child.source is None:
                continue
            table = child.table
            own_keys = [c for c in table.columns if c.pk or c.pk_inferred]
            for column in table.columns:
                family = self.key_family(child, column)
                if not family or column.fk or (table.key, column.name.upper()) in pointing:
                    continue
                if len(own_keys) == 1 and own_keys[0] is column:
                    continue                        # the table's own key: its values match other keys by accident
                for parent, key_column, key_family, text in parents:
                    if parent.source['id'] != child.source['id'] or key_family != family:
                        continue
                    if parent.table is table and key_column is column:
                        continue
                    pair = frozenset(((table.key, column.name.upper()), (parent.table.key, key_column.name.upper())))
                    if pair in related or len(pair) == 1:
                        continue
                    key = rel_key(table.id, [column.name], parent.table.id, [key_column.name])
                    if key in decided:
                        continue
                    score, why = affinity(column.name, parent.table.name, key_column.name, prefixes)
                    if parent.table is table and not score:
                        continue                    # a table's own key and one of its other columns, with no hint
                    if (column.type or '').upper() == (key_column.type or '').upper():
                        score += 0.5
                    out.append((score, child, column, parent, key_column, text, why, key))
        out.sort(key=lambda c: (-c[0], c[1].table.id.lower(), c[2].name.lower(), c[3].table.id.lower(), c[4].name.lower()))
        return out

    def pairs(self):
        """The pairs this run measures: at most RELATION_MAX_PAIRS, chosen by their VALUES.

        Every candidate pair gets a first look (first_look): a few dozen values of the child column, looked
        up in the parent key. Only the pairs that share values go on to the full check, the ones with a hint
        in their names first and then by how much they share. (Until 13 Oct 2026 the 200 likeliest BY NAME
        were tried, of 6,655 on the demo database: a link that only its values show was never reached.)
        """
        job = self.job
        found = self.candidates()
        with self.service.jobs.saving(job):
            self.store.drop_suggestions(self.ws_id)  # what the last run suggested is measured again, or goes
        looked = found[:config.RELATION_LOOK_PAIRS]
        passed = self.first_look(looked) if looked else []
        passed.sort(key=lambda item: (-(item[0][0] >= 1), -item[1], -item[0][0], item[0][1].table.id.lower(), item[0][2].name.lower(),
                                      item[0][3].table.id.lower(), item[0][4].name.lower()))
        pairs = [pair for pair, _ in passed[:config.RELATION_MAX_PAIRS]]
        if found:
            note = (f'{len(found):,} column pairs could hold the same values. A first look at {config.RELATION_PROBE_VALUES} '
                    f'values of each column found {len(passed):,} that share values')
            if len(looked) < len(found):
                note = (f'{len(found):,} column pairs could hold the same values; the {len(looked):,} likeliest by their names '
                        f'got a first look at {config.RELATION_PROBE_VALUES} values of each column (DATA_EXPLORER_RELATION_LOOK_PAIRS), '
                        f'which found {len(passed):,} that share values')
            note += ('; all of them were measured.' if len(pairs) == len(passed) else
                     f'; the {len(pairs):,} that share the most were measured (DATA_EXPLORER_RELATION_PAIRS).')
            job.notes.append(note)
        job.total += len(pairs)
        job.phase = 'suggesting'
        return pairs

    def first_look(self, found):
        """Which of the candidate pairs share values at all: [(pair, share of the looked-up values found)].

        What it costs: ONE small read a child table (RELATION_PROBE_ROWS rows of its candidate columns,
        spread over the table where the sample can be), and for each parent key ONE look-up of the values
        of all the child columns that might point at it, in bound lists of 500. On the demo database that
        is 67 small reads and about 400 look-ups for 6,655 pairs. On Oracle the small read samples whole
        blocks (SAMPLE BLOCK), which reads only those blocks: a sample of rows, SAMPLE (p), reads every
        block of the table. A table whose blocks give a column fewer than FIRST_LOOK_VALUES values is read
        once more the usual way (look_values), and the job's notes name it.
        A pair passes with at least 3 values of which half are found. A column of numbers is not passed
        on to a key that counts up from about 1 unless its name points there (serial): its values are
        in any such key, by accident.
        """
        job = self.job
        job.phase = 'looking'
        by_child, probes, read_twice = {}, {}, []
        for index, pair in enumerate(found):
            by_child.setdefault(pair[1].table.key, []).append(index)
        for child_key, indexes in by_child.items():
            job.check()
            child = self.targets[child_key]
            job.current = child.table.id
            columns = list(dict.fromkeys(found[i][2].name for i in indexes))
            check = budget_check(config.RELATION_CHECK_BUDGET_S, also=job.check)
            try:
                values, twice = self.look_values(child, columns, check)
            except Cancelled:
                raise
            except Exception as exc:
                job.notes.append(f'{child.table.id} was not looked at for links by values: {self.message(exc)}.')
                continue
            if twice:
                read_twice.append(child.table.id)
            for i in indexes:
                probes[i] = values[found[i][2].name.upper()]
        if read_twice:
            job.notes.append(f'Read twice for the first look: {", ".join(read_twice)}. A sample of whole blocks '
                             f'(SAMPLE BLOCK) gave a column fewer than {FIRST_LOOK_VALUES} values, so '
                             f'{"the table was" if len(read_twice) == 1 else "each was"} read again the usual way: '
                             f'a sample of rows, SAMPLE (p), up to DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB.')
        by_parent = {}
        for index, pair in enumerate(found):
            if len(probes.get(index) or ()) >= FIRST_LOOK_VALUES:
                by_parent.setdefault((pair[3].table.key, pair[4].name.upper()), []).append(index)
        passed = []
        for (parent_key, _), indexes in by_parent.items():
            job.check()
            parent_target, key_column = self.targets[parent_key], found[indexes[0]][4]
            job.current = f'{parent_target.table.id}.{key_column.name}'
            check = budget_check(config.RELATION_CHECK_BUDGET_S, also=job.check)
            position = {}                           # each different value is looked up once, whichever columns hold it
            for i in indexes:
                for value in probes[i]:
                    position.setdefault(value, len(position))
            try:
                parent = self.parent(parent_target, [key_column.name], check)
                (name,) = self.source_names(parent_target, [key_column.name])
                hit = parent.lookup([name], [(value,) for value in position], check)
                for i in indexes:
                    share = sum(1 for value in probes[i] if position[value] in hit) / len(probes[i])
                    if share < FIRST_LOOK_SHARE:
                        continue
                    if found[i][0] < 1 and numbers(probes[i]) and self.serial(parent_target, key_column, parent, name, check):
                        continue                    # e.g. a count, against a key that holds every number up to its highest
                    passed.append((found[i], share))
            except Cancelled:
                raise
            except Exception as exc:
                job.notes.append(f'{parent_target.table.id}.{key_column.name} was not looked at for links by values: '
                                 f'{self.message(exc)}.')
        return passed

    def look_values(self, child, columns, check):
        """({COLUMN: up to RELATION_PROBE_VALUES values}, read twice) of a child table, for the first look.

        The read asks for whole blocks (child_copy blocks=True: SAMPLE BLOCK on Oracle, nothing changes
        elsewhere). A small table can give few blocks or none: when the blocks gave a column fewer than
        FIRST_LOOK_VALUES values, the table is read again the usual way (no blocks asked: rows sampled up to
        the size limit, as profiling does)."""
        copy, copied = self.child_copy(child, columns, check, sample_rows=config.RELATION_PROBE_ROWS, blocks=True)
        by_blocks = bool((getattr(copy, 'plan', None) or {}).get('block'))
        values = probe_values(copy, columns, copied)
        if not by_blocks or all(len(found) >= FIRST_LOOK_VALUES for found in values.values()):
            return values, False
        copy, copied = self.child_copy(child, columns, check, sample_rows=config.RELATION_PROBE_ROWS)
        return probe_values(copy, columns, copied), True

    def serial(self, parent_target, key_column, parent, name, check):
        """True when a key counts up from about 1 with few gaps: 1, 2, 3 ... 48,979.

        Any whole number up to its highest value is in such a key, so a column of counts, of sort orders or
        of minutes "matches" it completely without having anything to do with it. Asked once a key: its
        smallest and largest value, then 40 numbers at even distances between them - a key that holds four
        in five of those is one in which a number is found by accident."""
        known = (parent_target.table.key, key_column.name.upper())
        if known not in self.serials:
            self.serials[known] = False
            low, high = parent.bounds(name, check)
            if numbers([low, high], whole=True) and 0 <= low <= SERIAL_STARTS_BY and high > low:
                low, high = int(low), int(high)
                asked = sorted({low + (high - low) * i // (SERIAL_PROBES - 1) for i in range(SERIAL_PROBES)})
                self.serials[known] = len(parent.lookup([name], [(v,) for v in asked], check)) >= SERIAL_DENSITY * len(asked)
        return self.serials[known]

    @staticmethod
    def message(exc):
        if isinstance(exc, TimedOut) or getattr(exc, 'timeout', False):
            return 'not checked (timeout)'
        if isinstance(exc, ReadOnlyViolation):
            return f'Blocked by the read-only guard: {exc}'
        return sanitize_error(str(exc) if isinstance(exc, (QueryFailed, SourceError)) else f'{type(exc).__name__}: {exc}',
                              limit=300)

    def try_pair(self, copy, copy_column, child, column, parent_target, key_column, key_text, why, score, key, check):
        parent = self.parent(parent_target, [key_column.name], check)
        (parent_name,) = self.source_names(parent_target, [key_column.name])
        looked, found, most, keys = overlap(copy, copy_column, parent, parent_name, check)
        if not looked:
            return
        share = found / looked
        # Without a hint in the names, small whole numbers match any key by accident: ask for more.
        named = score >= 1
        minimum = config.RELATION_SUGGEST_MIN_OVERLAP if named else max(config.RELATION_SUGGEST_MIN_OVERLAP, 0.95)
        enough = 3 if named else 20
        if looked < enough or share < minimum:
            return
        if not named and all(isinstance(k[0], (int, float)) and abs(k[0]) < 10_000 for k in keys):
            return
        if not named and numbers([k[0] for k in keys]) and self.serial(parent_target, key_column, parent, parent_name, check):
            return                                  # numbers in a key that counts up from 1: found there by accident
        child_max = int(copy.run(q.key_shape_query([copy_column]))[0][1] or 0)
        proven = proof(parent.unique_sets, [parent_name])
        if most > 1:
            cardinality = 'N-N' if child_max > 1 else 'unknown'
        elif proven:
            cardinality = '1-N' if child_max > 1 else ('1-1' if not copy.sampled else 'unknown')
        else:
            cardinality = 'unknown'
        evidence = {
            'overlap': round(share, 4), 'values_checked': looked, 'values_found': found,
            'sampled': bool(copy.sampled), 'rows': copy.loaded_rows, 'sample_method': copy.method,
            'label': (f'measured on a sample of {copy.loaded_rows:,} rows' if copy.sampled
                      else f'measured on all {copy.loaded_rows:,} rows'),
            'parent_key': key_text if not proven else f'{proven} in the catalog', 'parent_unique_proven': bool(proven),
            'child_max_per_key': child_max, 'name_hint': why, 'score': score,
            'text': f'{found:,} of {looked:,} distinct values of {child.table.name}.{column.name} exist in '
                    f'{parent_target.table.name}.{key_column.name}'}
        with self.service.jobs.saving(self.job):
            self.store.suggest(self.ws_id, key, child.table.id, [column.name], parent_target.table.id, [key_column.name],
                               round(share, 4), cardinality, evidence)


def table_prefixes(names):
    """The first words that several table names share: SUB in SUB_DEVICE, SUB_SIM_CARD and SUB_SUBSCRIPTION.
    Such a word names the part of the system a table belongs to, not what the table holds - so it is no name
    hint: ACTIVE_SUBS does not point at every table that starts with SUB_."""
    first = {}
    for name in names:
        words = tokens(str(name).rsplit('.', 1)[-1])
        if len(words) > 1:
            first[words[0]] = first.get(words[0], 0) + 1
    return {word for word, n in first.items() if n > 1}


def _same_word(word, other, short=4):
    """True when two words are one word, or one is short for the other (cust / customer, sales / sal)."""
    a, b = _stem(word), _stem(other)
    return a == b or (len(a) >= 3 and b.startswith(a)) or (len(b) >= short and a.startswith(b))


def pointer_words(column):
    """The words of a column's name that can name what the column points at.

    CUSTOMER_ID, CUST, PARENT_CODE   the one word that is left without "id", "code", "no" ...
    BILLING_CUSTOMER_ID              a word that such a key word follows directly
    Not the ORDER of SORT_ORDER and not the SUBS of ACTIVE_SUBS: there the word is part of the name of
    something else - a sort order, a number of active subscribers - and no reference to an order or a
    subscription.
    """
    words = tokens(column)
    significant = [w for w in words if w not in GENERIC_WORDS]
    if len(significant) == 1:
        return significant
    return [w for w, following in zip(words, words[1:]) if w not in GENERIC_WORDS and following in GENERIC_WORDS]


def affinity(child_column, parent_table, parent_column, prefixes=()):
    """How much a child column's name points at a parent column: (score, the reason in words or None).

    prefixes: first words that several tables share (table_prefixes) - never a hint by themselves.
    """
    child, key = tokens(child_column), tokens(parent_column)
    table = [t for t in tokens(parent_table.rsplit('.', 1)[-1])]
    own = [t for t in table if t not in prefixes] or table        # what the table holds, without the part-of-the-system word
    if child_column.upper() == parent_column.upper() and child_column.upper() not in ('ID', 'CODE', 'KEY', 'NO'):
        return 3, 'the two columns have the same name'
    for word in pointer_words(child_column):
        if any(_same_word(word, t) for t in own):
            return 2, f'"{word}" in the column name points at {parent_table}'
    # the whole name is the table's name: SALES_CHANNEL and SAL_CHANNEL (every word matches, one of them an own word)
    significant = [w for w in child if w not in GENERIC_WORDS]
    if len(significant) > 1 and all(any(_same_word(w, t, short=3) for t in table) for w in significant) \
            and any(_same_word(w, t) for w in significant for t in own):
        return 2, f'the column is named like {parent_table}'
    # A word both column names hold: CONTACT_MSISDN and MSISDN. Not when it is the parent table's own word - the
    # ORDER of ORDER_ID in the table of orders: whether a column points at that table was decided above.
    shared = [w for w in significant if w in key and (w in pointer_words(child_column) or not any(_same_word(w, t) for t in table))]
    if shared:
        return 1.5, f'both column names hold "{shared[0]}"'
    return 0, None                                  # "id", "code", "no" ... in both names says nothing


def _stem(word):
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    if word.endswith('s') and not word.endswith('ss') and len(word) > 3:
        return word[:-1]
    return word
