"""Session 3, part B: relationship checks - coverage, orphans, cardinality, links suggested by values."""

import io
import json
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from app import config
from app.analysis import relations as relations_module
from app.analysis.relations import (DatabaseParent, NO_MATCH, WholeTable, affinity, bind_type, canon, catalog_keys,
                                    compare_class, fraction_text, key_kind, limits, measure, proof, share_text, shown,
                                    to_family)
from app.analysis.store import rel_key
from app.profiling import queries as q
from app.profiling import service as service_module
from app.profiling.data import DatabaseData, QueryFailed
from app.profiling.profiler import read_table
from app.security import check_read_only_sql
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from scripts import live_engines as le

ROWS = le.extra_rows()
CHILDREN = ROWS['x_children'][1]
ITEMS = ROWS['x_pair_items'][1]
# what the generated rows hold (see scripts/live_engines.extra_rows)
EMPTY = sum(1 for r in CHILDREN if r[1] is None)                          # 50 of 1,000
ORPHAN_ROWS = sum(1 for r in CHILDREN if r[1] is not None and r[1] > 200)
KEYS = len({r[1] for r in CHILDREN if r[1] is not None})                                # 209 different values
ORPHAN_KEYS = len({r[1] for r in CHILDREN if r[1] is not None and r[1] > 200})          # 19 of them are not parents
ITEM_EMPTY = sum(1 for r in ITEMS if r[1] is None)
ITEM_ORPHANS = sum(1 for r in ITEMS if r[1] == 11)

SCHEMA = '''
CREATE TABLE x_parents (parent_id INTEGER PRIMARY KEY, code VARCHAR(10) NOT NULL, grp INTEGER NOT NULL, label VARCHAR(20),
    CONSTRAINT uq_x_parents_code UNIQUE (code));
CREATE TABLE x_children (child_id INTEGER PRIMARY KEY, parent_id INTEGER, parent_code VARCHAR(10), grp INTEGER, msisdn VARCHAR(12));
CREATE TABLE x_profiles (parent_id INTEGER PRIMARY KEY, bio VARCHAR(20), FOREIGN KEY (parent_id) REFERENCES x_parents (parent_id));
CREATE TABLE x_pairs (a INTEGER NOT NULL, b INTEGER NOT NULL, note VARCHAR(20), PRIMARY KEY (a, b));
CREATE TABLE x_pair_items (item_id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, qty INTEGER,
    CONSTRAINT fk_x_pair_items_pair FOREIGN KEY (a, b) REFERENCES x_pairs (a, b));
CREATE TABLE x_labels (label VARCHAR(20), colour VARCHAR(10));
CREATE UNIQUE INDEX ux_labels_colour ON x_labels (colour);
CREATE UNIQUE INDEX ux_labels_partial ON x_labels (label) WHERE colour = 'red';
'''
# two JOINs on columns that are no keys (a many-to-many, and a link to a table without a key), and one
# from a phone number to a key it never matches
JOINS = b'''SELECT c.child_id, p.label FROM x_children c JOIN x_parents p ON p.grp = c.grp;
SELECT * FROM x_children c JOIN x_labels l ON l.label = c.parent_code;
SELECT * FROM x_children c JOIN x_pairs p ON p.a = c.msisdn;'''
N = 6            # relationships of the fixture: 2 declared, 3 from the JOINs, 1 from the names


def extras_db(tmp_path, name='extras.db'):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    for table in ('x_parents', 'x_children', 'x_profiles', 'x_pairs', 'x_pair_items'):
        columns, rows = ROWS[table]
        con.executemany(f'INSERT INTO {table} ({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))})', rows)
    con.executemany('INSERT INTO x_labels VALUES (?, ?)', [(f'parent {i}', f'c{i}') for i in range(1, 41)])
    con.commit()
    con.close()
    return path


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Relations'}).json()['id']


@pytest.fixture
def extras(client, ws, tmp_path):
    """The extra schema in SQLite, plus two JOIN queries (a many-to-many and a link to a table with no key)."""
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(extras_db(tmp_path))})
    assert added.status_code == 200, added.text
    assert client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('joins.sql', JOINS, 'text/plain'))]).status_code == 200
    return ws


def wait(client, job):
    assert 'id' in job, job
    for _ in range(6000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def check(client, ws, **body):
    started = client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': 'all', **body})
    assert started.status_code == 200, started.text
    return wait(client, started.json())


def overview(client, ws):
    response = client.get(f'/api/workspaces/{ws}/relations/check')
    assert response.status_code == 200, response.text
    return response.json()


def by_pair(view):
    return {(r['from'], tuple(r['from_columns']), r['to']): r for r in view['relationships']}


class Recorder(ReadOnlyDatabase):
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Recorder.opened.append(self)

    @classmethod
    def reads(cls):
        return [s for db in cls.opened for s in db.statements if s.lstrip().upper().startswith(('SELECT', 'WITH'))]


@pytest.fixture
def recorder(monkeypatch):
    Recorder.opened = []
    monkeypatch.setattr(service_module, 'ReadOnlyDatabase', Recorder)
    return Recorder


@pytest.fixture
def lookups(monkeypatch):
    """The database is not asked to count a relationship itself: the child's keys are read and looked
    up in the parent - the method for files, for key columns of different kinds, and after a count
    that ran out of time."""
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', False)


# ------------------------------------------------------------------ the parts

def test_values_are_compared_in_the_parents_type():
    assert to_family('123', 'number') == 123 and to_family(' 12.50 ', 'number') == Decimal('12.50')
    assert to_family(5.0, 'number') == 5 and to_family(Decimal('7.00'), 'number') == 7 and to_family(True, 'number') == 1
    assert to_family('ABC', 'number') is NO_MATCH and to_family(float('nan'), 'number') is NO_MATCH
    assert to_family(None, 'text') is NO_MATCH                      # an empty key is never looked up
    assert to_family(123, 'text') == '123' and to_family(5.0, 'text') == '5' and to_family('x', 'text') == 'x'
    assert canon(Decimal('12.50'), 'number') == canon('12.5', 'number') == 12.5
    assert canon('AB  ', 'text') == 'AB' and canon('Ab', 'text', fold=True) == canon('aB', 'text', fold=True)
    assert canon('Ab', 'text') != canon('aB', 'text')
    assert bind_type('number', [1, 2 ** 40]).__class__.__name__ == 'BigInteger'      # never a 32-bit integer
    assert bind_type('number', [1, Decimal('1.5')]).__class__.__name__ == 'Numeric'
    assert bind_type('text', ['a']).__class__.__name__ == 'String'


def test_a_date_key_is_bound_in_the_form_its_parent_column_stores():
    """A column of days is asked for days and a timestamp for moments. (Until 13 Oct 2026 every date key was
    bound as a timestamp, and SQLite - which keeps '2025-10-01' as that text - never found one.)"""
    day, moment = date(2025, 1, 2), datetime(2025, 1, 2)
    for value in ('2025-01-02', ' 2025-01-02 00:00:00 ', day, moment, datetime(2025, 1, 2, tzinfo=None)):
        assert to_family(value, 'date') == day and type(to_family(value, 'date')) is date
        assert to_family(value, 'datetime') == moment and type(to_family(value, 'datetime')) is datetime
    # 13:00 on that day is a moment, and not one of the values of a column of days: no silent cut
    assert to_family(datetime(2025, 1, 2, 13), 'date') is NO_MATCH and to_family('2025-01-02 13:00:00', 'date') is NO_MATCH
    assert to_family('2025-01-02 13:00:00', 'datetime') == datetime(2025, 1, 2, 13)
    assert to_family('soon', 'date') is NO_MATCH and to_family('soon', 'datetime') is NO_MATCH
    assert to_family(None, 'date') is NO_MATCH
    assert canon('2025-01-02', 'date') == canon(moment, 'date') == day != canon(moment, 'datetime')
    assert bind_type('date', [day]).__class__.__name__ == 'Date' and bind_type('datetime', [moment]).__class__.__name__ == 'DateTime'
    # which of the two a column is, per engine: Oracle's DATE holds a time of day, SQLite keeps what it was given
    kinds = {engine: [key_kind(t, engine) for t in ('DATE', 'DATETIME', 'TIMESTAMP', 'DATETIME2', 'TIMESTAMP WITH TIME ZONE')]
             for engine in ('oracle', 'mssql', 'postgresql', 'mysql', 'duckdb', 'sqlite')}
    assert kinds['oracle'] == ['datetime'] * 5 and kinds['sqlite'] == ['text'] * 5
    assert kinds['mssql'] == kinds['postgresql'] == kinds['mysql'] == kinds['duckdb'] == ['date'] + ['datetime'] * 4
    assert [key_kind(t, 'postgresql') for t in ('INTEGER', 'NUMERIC(10, 2)', 'VARCHAR(20)', 'UUID', 'BOOLEAN', None)] == [
        'number', 'number', 'text', 'text', 'text', 'text']


@pytest.mark.parametrize('engine, a, b, same', [
    ('postgresql', 'INTEGER', 'NUMERIC(12, 0)', True), ('oracle', 'NUMBER(10, 0)', 'NUMBER', True),
    ('mssql', 'VARCHAR(10)', 'NVARCHAR(20)', True), ('postgresql', 'VARCHAR(10)', 'TEXT', True),
    ('postgresql', 'DATE', 'DATE', True), ('mysql', 'DATETIME', 'TIMESTAMP', True), ('sqlite', 'DATE', 'DATE', True),
    ('postgresql', 'UUID', 'UUID', True), ('mssql', 'CHAR(3)', 'CHAR(3)', True), ('sqlite', 'CHAR(3)', 'VARCHAR(3)', True),
    # what the database would refuse, or convert without being asked
    ('postgresql', 'INTEGER', 'VARCHAR(10)', False), ('mysql', 'VARCHAR(10)', 'INTEGER', False),
    ('sqlite', 'TEXT', 'INTEGER', False), ('postgresql', 'DATE', 'TIMESTAMP WITHOUT TIME ZONE', False),
    ('postgresql', 'TIMESTAMP WITHOUT TIME ZONE', 'TIMESTAMP WITH TIME ZONE', False), ('postgresql', 'UUID', 'VARCHAR(36)', False),
    ('oracle', 'CHAR(3)', 'VARCHAR(3 CHAR)', False), ('mssql', 'CHAR(3)', 'CHAR(5)', False), ('sqlite', 'DATE', 'TEXT', False),
    ('postgresql', 'BOOLEAN', 'BOOLEAN', False), ('mysql', "ENUM('a','b')", "ENUM('a','b')", False), ('sqlite', '', '', False),
    ('postgresql', 'BYTEA', 'BYTEA', False), ('oracle', 'DATE', 'TIMESTAMP', True), ('mssql', 'DATE', 'DATETIME2', False),
])
def test_the_database_only_compares_columns_of_the_same_kind(engine, a, b, same):
    """What may be put into one anti-join: no type error, and no conversion nobody asked for."""
    classes = compare_class(a, engine), compare_class(b, engine)
    assert (classes[0] is not None and classes[0] == classes[1]) is same, classes


@pytest.mark.parametrize('part, whole, text', [
    (0, 10, '0%'), (10, 10, '100%'), (5, 10, '50%'), (1, 3, '33.3%'), (2, 3, '66.7%'), (16824, 48979, '34.3%'),
    (54986, 55406, '99.2%'), (55096, 55406, '99.4%'), (55366, 55406, '>99.9%'), (1999953, 2000373, '>99.9%'),
    (99996, 100000, '>99.9%'),
    (999, 1000, '99.9%'), (9995, 10000, '>99.9%'), (1, 1000, '0.1%'), (1, 1001, '<0.1%'), (4, 100000, '<0.1%'),
    (174489, 177414, '98.4%'), (0, 0, ''), (None, 5, ''), (3, 2, '100%'),
])
def test_a_share_is_never_rounded_to_all_or_nothing(part, whole, text):
    """The Relations tab and the report print a coverage by this one rule: "100%" only when nothing is missing."""
    assert share_text(part, whole) == text


SHARES = [0, 1, 0.5, 1 / 3, 2 / 3, 190 / 209, 0.98765, 0.9877, 0.999, 0.9995, 0.99996, 0.001, 0.0004, 0.12345, 0.0994, 1.2, -1]


def test_a_share_given_as_a_fraction_follows_the_same_rule():
    """An accepted link's confidence is its share as measured: printed like a coverage, never rounded to 99% or 100%."""
    assert [fraction_text(x) for x in SHARES] == [
        '0%', '100%', '50%', '33.3%', '66.7%', '90.9%', '98.8%', '98.8%', '99.9%', '>99.9%', '>99.9%', '0.1%', '<0.1%',
        '12.3%', '9.9%', '100%', '0%']
    assert fraction_text(None) == ''
    for part, whole in ((54986, 55406), (55366, 55406), (1, 3), (16824, 48979), (1, 1001)):
        assert fraction_text(part / whole) == share_text(part, whole)


def test_the_page_prints_a_values_share_by_the_same_rule():
    """shareText() in static/app.js, run with Node.js: the same text as fraction_text for every share; kindShare()
    uses it for a link found by values only."""
    app_js = (Path(__file__).resolve().parent.parent / 'static' / 'app.js').read_text(encoding='utf-8')
    share = re.search(r'\nfunction shareText\(x\) \{\n.*?\n\}\n', app_js, re.S)
    kind = re.search(r'\nconst kindShare = .*?;\n', app_js)
    assert share and kind
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    run = ('const percent = (x) => `${Math.round(x * 100)}%`;' + share.group(0) + kind.group(0) +
           'const xs = JSON.parse(process.argv[1]);'
           "console.log(JSON.stringify([xs.map(shareText), kindShare('values', 0.9877), kindShare('names', 0.9877)]));")
    out = subprocess.run([node, '-e', run, json.dumps(SHARES)], capture_output=True, text=True, timeout=60, check=True).stdout
    texts, values, names = json.loads(out)
    assert texts == [fraction_text(x) for x in SHARES]
    assert (values, names) == ('98.8%', '99%')                  # other kinds print as before


def test_lookups_are_bound_lists_of_at_most_500_keys():
    one = q.lookup_query('oracle', 'T', 'PARENTS', ['ID'], [(i,) for i in range(3)], [bind_type('number', [1])])
    text = ' '.join(str(one.compile(compile_kwargs={'render_postcompile': True})).split())
    assert text == ('SELECT "ID", count(*) AS n FROM "T"."PARENTS" WHERE "ID" IN (:k_1, :k_2, :k_3) GROUP BY "ID"')
    two = q.lookup_query('mssql', 'dbo', 'pairs', ['a', 'b'], [(1, 2), (3, 4)], [bind_type('number', [1])] * 2)
    text = ' '.join(str(two.compile()).split())
    assert 'WHERE a = :param_1 AND b = :param_2 OR a = :param_3 AND b = :param_4' in text.replace('"', '')
    for statement in (one, two, q.duplicates_query('postgresql', 's', 't', ['a', 'b'])):
        sql = str(statement.compile(compile_kwargs={'render_postcompile': True}))
        check_read_only_sql(sql, dialect='postgresql')
        assert "'" not in sql                                       # no value is ever part of the text
    assert config.RELATION_IN_CHUNK == 500
    duplicates = ' '.join(str(q.duplicates_query('postgresql', 's', 't', ['a', 'b']).compile()).split())
    assert 'GROUP BY "a", "b" HAVING count(*) > 1' in duplicates and 'LIMIT' in duplicates


def test_the_catalog_says_what_is_unique(tmp_path):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(extras_db(tmp_path))))
    keys = catalog_keys(db, None)
    assert proof(keys['X_PARENTS'], ['parent_id']) == 'primary key'
    assert proof(keys['X_PARENTS'], ['CODE']) in ('unique constraint uq_x_parents_code', 'unique index sqlite_autoindex_x_parents_1',
                                                    'unique constraint')
    assert proof(keys['X_PARENTS'], ['grp']) is None and proof(keys['X_PARENTS'], ['grp', 'label']) is None
    assert proof(keys['X_PAIRS'], ['a', 'b']) == 'primary key' and proof(keys['X_PAIRS'], ['a']) is None
    assert proof(keys['X_PAIRS'], ['b', 'a', 'note']) == 'primary key'             # a superset of a key is unique too
    assert proof(keys['X_LABELS'], ['colour']) == 'unique index ux_labels_colour'
    assert proof(keys['X_LABELS'], ['label']) is None                             # a partial index proves nothing
    assert proof(None, ['x']) is None and 'X_CHILDREN' in keys
    assert db.blocked == [] and all(s.startswith(('SELECT', 'PRAGMA')) for s in db.statements)
    db.dispose()


@pytest.mark.parametrize('child, table, parent, score', [
    ('msisdn', 'subscriptions', 'msisdn', 3), ('parent_code', 'x_parents', 'code', 2), ('cust', 'customers', 'id', 2),
    ('customer_ref', 'crm.customers', 'customer_id', 2), ('plan_code', 'tariffs', 'tariff_code', 0),
    ('billing_account_no', 'ledgers', 'account_no', 1.5), ('qty', 'x_parents', 'parent_id', 0), ('a', 'x_parents', 'parent_id', 0),
    ('id', 'customers', 'id', 0), ('score', 'scores', 'id', 2),
])
def test_name_hints_rank_the_pairs_to_try(child, table, parent, score):
    assert affinity(child, table, parent)[0] == score
    assert (affinity(child, table, parent)[1] is None) is (score == 0)


# ---------------------------------------------------- coverage, orphans, nulls

def test_coverage_orphans_and_null_share(client, extras, recorder, lookups):
    ws = extras
    recorder.opened = []
    job = check(client, ws)
    assert (job['kind'], job['state'], job['done'], job['total'], job['errors']) == ('relations', 'done', N, N, [])
    view = overview(client, ws)
    assert view['job']['id'] == job['id'] and view['summary'] == {
        'relationships': N, 'checked': N, 'not_checked': 0, 'never': 0, 'with_orphans': 4, 'sampled': 0, 'not_exact': 0}
    rel = by_pair(view)[('x_children', ('parent_id',), 'x_parents')]
    assert (rel['kind'], rel['to_columns'], rel['checkable']) == ('names', ['parent_id'], True)
    c = rel['check']
    assert (c['status'], c['rows'], c['sampled'], c['label']) == ('checked', 1000, False, 'measured on all 1,000 rows')
    # every row was read and every key looked up: the numbers are those of the table, and the list says so
    assert (c['method'], c['exact'], c['shown']['exact'], c['shown']['tag'], c['shown']['notes']) == ('lookup', True, True, '', [])
    assert c['shown']['how'] == "Every row of the child's keys was read, and the keys were looked up in the parent"
    assert c['not_whole'] == 'counting inside the database is switched off (DATA_EXPLORER_RELATION_WHOLE_TABLE)'
    assert (c['null_rows'], c['null_share']) == (EMPTY, EMPTY / 1000) == (50, 0.05)
    assert (c['keys_total'], c['keys_checked'], c['rows_checked'], c['partial_keys']) == (KEYS, KEYS, 1000 - EMPTY, False)
    assert (c['orphan_rows'], c['orphan_keys']) == (ORPHAN_ROWS, ORPHAN_KEYS) and (KEYS, ORPHAN_KEYS) == (209, 19)
    assert c['coverage'] == pytest.approx(1 - ORPHAN_ROWS / (1000 - EMPTY)) and 0.90 < c['coverage'] < 0.93
    examples = c['orphan_examples']                                   # up to 20 example keys, the most frequent first
    assert len(examples) == 19 and config.RELATION_ORPHAN_EXAMPLES == 20 and c['orphan_columns'] == ['parent_id']
    assert {e['key'][0] for e in examples} == {r[1] for r in CHILDREN if r[1] and r[1] > 200} and c['examples_masked'] is False
    assert [e['rows'] for e in examples] == sorted((e['rows'] for e in examples), reverse=True)
    assert sum(e['rows'] for e in examples) == ORPHAN_ROWS
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique']) == ('1-N', True, True)
    assert c['parent_unique_how'] == 'unique: primary key in the catalog' and c['child_max_per_key'] >= 4
    assert c['statements'] == 2 and c['checked_at']                   # the child's one read, and one look-up of 209 keys

    # what the database was asked: the child's key columns once, then "which of these keys do you have?"
    reads = recorder.reads()
    (child_read,) = [r for r in reads if 'FROM "x_children"' in r and ' AS c0' in r]      # ONE read for its two relationships: the keys only
    assert child_read.endswith('\nFROM "x_children"\n LIMIT ? OFFSET ?') and child_read.count(' AS c') == 2
    assert {'"parent_id"', '"msisdn"'} <= set(child_read.replace(',', ' ').split()) and '"child_id"' not in child_read
    lookups = [r for r in reads if 'FROM "x_parents"' in r and ' IN (' in r]
    assert lookups[0].startswith('SELECT "parent_id", count(*) AS n \nFROM "x_parents" \nWHERE "parent_id" IN (?, ?')
    assert sorted(r.count('?') for r in lookups) == [150, KEYS]       # x_profiles' keys, and x_children's
    assert not any('JOIN' in r.upper() for r in reads)
    own = [r for r in reads if 'sqlite_master' not in r and 'pragma' not in r.lower()]   # (not the inspector's catalog reads)
    assert len(own) >= 10 and all("'" not in r for r in own)                             # every value is bound
    for database in recorder.opened:
        assert database.blocked == []
        for statement in database.statements:
            check_read_only_sql(statement, dialect='sqlite')


def test_a_relationship_with_several_columns(client, extras, recorder, lookups):
    recorder.opened = []
    check(client, extras)
    rel = by_pair(overview(client, extras))[('x_pair_items', ('a', 'b'), 'x_pairs')]
    assert (rel['kind'], rel['to_columns'], rel['name']) == ('declared', ['a', 'b'], 'fk_x_pair_items_pair')
    c = rel['check']
    assert (c['rows'], c['null_rows'], c['orphan_rows']) == (500, ITEM_EMPTY, ITEM_ORPHANS)      # declared, never validated
    assert c['coverage'] == pytest.approx(1 - ITEM_ORPHANS / (500 - ITEM_EMPTY)) and c['orphan_keys'] == 10
    assert all(e['key'][0] == 11 and 1 <= e['key'][1] <= 10 for e in c['orphan_examples']) and len(c['orphan_examples']) == 10
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique_how']) == ('1-N', True, 'unique: primary key in the catalog')
    (lookup,) = [r for r in recorder.reads() if 'FROM "x_pairs"' in r and '"b" = ?' in r]
    assert lookup.count('("a" = ? AND "b" = ?)') + lookup.count('"a" = ? AND "b" = ?') >= 110 and ' IN (' not in lookup


def test_results_on_a_sample_say_so(client, extras, monkeypatch, lookups):
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 300)             # the same sampling as profiling
    check(client, extras)
    view = overview(client, extras)
    c = by_pair(view)[('x_children', ('parent_id',), 'x_parents')]['check']
    assert (c['sampled'], c['rows'], c['sample_method']) == (True, 300, 'first 300 rows, not random')
    assert c['label'] == 'measured on a sample of 300 rows' and c['null_rows'] == 15
    # ... in the row of the list itself: the orphans found in a sample are a lower bound for the table
    in_sample = sum(1 for r in CHILDREN[:300] if r[1] is not None and r[1] > 200)
    assert (c['exact'], c['orphan_rows']) == (False, in_sample) and 0 < in_sample < ORPHAN_ROWS
    assert c['shown']['orphans'] == f'≥ {in_sample}' and c['shown']['tag'] == 'first 300 rows' and c['shown']['exact'] is False
    assert c['shown']['notes'] == [
        'The child table was read on a sample of 300 rows (first 300 rows, not random). Orphan rows, coverage and empty '
        'keys are those of the sample: the table has at least that many orphan rows.',
        'The database was not asked to count the whole table: counting inside the database is switched off '
        '(DATA_EXPLORER_RELATION_WHOLE_TABLE).']
    assert c['shown']['how'] == "The child's keys were read on a sample and looked up in the parent"
    small = by_pair(view)[('x_profiles', ('parent_id',), 'x_parents')]['check']        # 150 rows: read whole
    assert (small['sampled'], small['label'], small['exact'], small['shown']['orphans']) == (False, 'measured on all 150 rows', True, '0')
    assert view['summary']['sampled'] == 3 == view['summary']['not_exact'] and view['settings']['sample_target_rows'] == 300


def test_at_most_so_many_keys_are_looked_up_in_chunks(client, extras, recorder, monkeypatch, lookups):
    monkeypatch.setattr(config, 'RELATION_MAX_KEYS', 90)
    monkeypatch.setattr(config, 'RELATION_IN_CHUNK', 40)
    recorder.opened = []
    check(client, extras, relationships=[rel_key('x_children', ['parent_id'], 'x_parents', ['parent_id'])])
    view = overview(client, extras)
    c = by_pair(view)[('x_children', ('parent_id',), 'x_parents')]['check']
    assert (c['keys_total'], c['keys_checked'], c['partial_keys']) == (KEYS, 90, True)
    assert c['rows_checked'] < 950 and 0 < c['coverage'] <= 1 and c['statements'] == 4
    sent = [r for r in recorder.reads() if ' IN (' in r]
    assert [r.count('?') for r in sent] == [40, 40, 10]                # three bound lists, none longer than the chunk
    # 90 of 209 keys: what was found is a lower bound, and the row of the list says which keys it stands for
    assert (c['exact'], c['sampled'], c['label']) == (False, False, 'measured on all 1,000 rows')
    assert 0 < c['orphan_rows'] < ORPHAN_ROWS and c['shown']['orphans'] == f'≥ {c["orphan_rows"]}'
    assert c['shown']['tag'] == '90 of 209 keys looked up' and view['summary']['not_exact'] == 1
    assert c['shown']['notes'][0] == (
        '90 of 209 distinct keys were looked up in the parent - at most 90 per relationship (DATA_EXPLORER_RELATION_KEYS). '
        'The orphan rows and the coverage are those of the keys looked up: the table has at least that many orphan rows.')
    assert c['shown']['coverage'] != '100%'                            # never "100%" next to orphan rows


# ------------------------------------------------------------------ cardinality

def test_cardinality_from_catalog_and_data(client, extras, lookups):
    check(client, extras)
    rels = by_pair(overview(client, extras))
    one_to_one = rels[('x_profiles', ('parent_id',), 'x_parents')]['check']
    assert (one_to_one['cardinality'], one_to_one['cardinality_proven'], one_to_one['coverage']) == ('1-1', True, 1.0)
    assert one_to_one['cardinality_note'] == 'one parent row and one child row for a key (primary key on the child, in the catalog)'
    assert (one_to_one['child_max_per_key'], one_to_one['orphan_rows'], one_to_one['null_rows']) == (1, 0, 0)
    many = next(r for r in rels.values() if r['from_columns'] == ['grp'])  # from a JOIN on two columns that are no keys
    assert (many['kind'], many['model_cardinality']) == ('joins', 'unknown')
    c = many['check']
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique']) == ('N-N', True, False)
    assert c['parent_unique_how'] == 'not unique: some of the looked-up keys occur more than once in the parent'
    assert c['cardinality_note'].startswith('keys repeat on both sides (up to ') and c['coverage'] == 1.0
    labels = next(r for r in rels.values() if 'x_labels' in (r['from'], r['to']))['check']    # labels and codes never match
    assert labels['coverage'] == 0.0 and labels['orphan_keys'] in (40, 220) and len(labels['orphan_examples']) == 20


def copy_of(path, table, columns, plans=None):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(path)))
    data = DatabaseData(db, None, table)
    copy = read_table(data, plans or [q.ColumnPlan(c, 'INTEGER', 'number') for c in columns])
    data.close()
    return db, copy


def test_uniqueness_without_a_constraint_is_checked_on_the_data(tmp_path):
    path = tmp_path / 'u.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE parent (k INTEGER, dup INTEGER); CREATE TABLE child (k INTEGER, dup INTEGER);')
    con.executemany('INSERT INTO parent VALUES (?, ?)', [(i, i % 50) for i in range(1, 101)])        # no key declared
    con.executemany('INSERT INTO child VALUES (?, ?)', [(1 + i % 10, 60 + i % 5) for i in range(40)])
    con.commit()
    con.close()
    db, copy = copy_of(path, 'child', ['k', 'dup'])
    conn = DatabaseData(db, None, 'lookups')
    parent = DatabaseParent(conn, None, 'parent', {'K': 'number', 'DUP': 'number'}, [])
    sent = len(db.statements)
    unique = measure(copy, ['k'], parent, ['k'], lambda: None)
    assert unique['parent_unique'] is True and unique['cardinality'] == '1-N' and unique['cardinality_proven']
    assert unique['parent_unique_how'] == 'unique: no key occurs twice in the parent (checked now; no constraint says so)'
    asked = [s for s in db.statements[sent:] if s.startswith('SELECT')]
    assert len(asked) == 2 and 'HAVING count(*) > 1' in asked[1] and 'LIMIT' in asked[1]      # one question to the parent
    # the looked-up keys do not exist (60..64), so they show no duplicate; the question to the parent does
    not_unique = measure(copy, ['dup'], parent, ['dup'], lambda: None)
    assert (not_unique['parent_unique'], not_unique['coverage'], not_unique['cardinality']) == (False, 0.0, 'N-N')
    assert not_unique['parent_unique_how'] == 'not unique: a key occurs more than once in the parent'
    conn.close()
    copy.close()
    db.dispose()


def test_when_the_duplicate_check_runs_out_of_time_unique_is_not_proven(tmp_path, monkeypatch):
    path = tmp_path / 'u.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE parent (k INTEGER); CREATE TABLE child (k INTEGER);')
    con.executemany('INSERT INTO parent VALUES (?)', [(i,) for i in range(1, 101)])
    con.executemany('INSERT INTO child VALUES (?)', [(1 + i % 10,) for i in range(40)])
    con.commit()
    con.close()
    db, copy = copy_of(path, 'child', ['k'])
    conn = DatabaseData(db, None, 'lookups')
    parent = DatabaseParent(conn, None, 'parent', {'K': 'number'}, [])
    calls = []

    def timed_out(columns, check):
        calls.append(columns)
        raise QueryFailed('timed out after 30 s', timeout=True)
    monkeypatch.setattr(parent, 'has_duplicates', timed_out)
    result = measure(copy, ['k'], parent, ['k'], lambda: None)
    assert calls == [['k']]                                            # asked once; never again
    assert result['parent_unique'] is None and result['parent_unique_how'] == 'unique not proven: the check for duplicates ran out of time'
    assert (result['cardinality'], result['cardinality_proven']) == ('unknown', False)
    assert result['cardinality_note'] == 'unique not proven; up to 4 child rows for a key'
    assert result['coverage'] == 1.0 and result['status'] == 'checked'  # the rest of the check stands
    conn.close()
    copy.close()
    db.dispose()


def test_profilings_distinct_counts_are_never_proof(client, extras, app, monkeypatch):
    """x_parents.label is different in every row, and the saved profile says so. No constraint says so."""
    ws = extras
    assert wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())['errors'] == []
    label = app.state.profiling.store.columns(ws, 'X_PARENTS')['X_PARENTS']['LABEL']
    assert label['unique'] is True and label['distinct'] == label['rows'] == 200
    sql = b'SELECT * FROM x_children c JOIN x_parents p ON p.label = c.msisdn;'
    client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('more.sql', sql, 'text/plain'))])
    monkeypatch.setattr(DatabaseParent, 'has_duplicates',
                        lambda self, columns, check: (_ for _ in ()).throw(QueryFailed('timed out', timeout=True)))
    check(client, ws)
    rel = next(r for r in overview(client, ws)['relationships'] if 'msisdn' in r['from_columns'] + r['to_columns'])
    assert rel['check']['parent_unique'] is None and rel['check']['cardinality'] == 'unknown'
    assert 'unique not proven' in rel['check']['parent_unique_how']
    source = (relations_module.__file__, open(relations_module.__file__, encoding='utf-8').read())[1]
    assert "'distinct'" not in source.split('def candidates')[0]         # the checks never read saved distinct counts


def test_a_child_that_was_read_whole_can_prove_one_to_one(tmp_path):
    path = tmp_path / 'u.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE parent (k INTEGER PRIMARY KEY); CREATE TABLE child (k INTEGER);')
    con.executemany('INSERT INTO parent VALUES (?)', [(i,) for i in range(1, 101)])
    con.executemany('INSERT INTO child VALUES (?)', [(i,) for i in range(1, 61)])
    con.commit()
    con.close()
    db, copy = copy_of(path, 'child', ['k'])
    parent = DatabaseParent(DatabaseData(db, None, 'x'), None, 'parent', {'K': 'number'}, [(frozenset({'K'}), 'primary key')])
    whole = measure(copy, ['k'], parent, ['k'], lambda: None)
    assert (whole['cardinality'], whole['cardinality_proven']) == ('1-1', True)
    assert whole['cardinality_note'] == ('one parent row and one child row for a key (no key repeats in the child, and all '
                                         'its rows were read)')
    copy.sampled = True                                                 # the same numbers from a sample prove less
    sampled = measure(copy, ['k'], parent, ['k'], lambda: None)
    assert (sampled['cardinality'], sampled['cardinality_proven']) == ('1-1', False)
    assert '1-N is not ruled out' in sampled['cardinality_note'] and sampled['label'] == 'measured on a sample of 60 rows'
    parent.conn.close()
    copy.close()
    db.dispose()


# ----------------------------------------- counted by the database, over the whole table

def not_exists(reads):
    return [r for r in reads if 'NOT (EXISTS' in r]


def test_the_database_counts_a_relationship_over_the_whole_child_table(client, extras, recorder):
    """Child and parent in one database, key columns of the same kind: ONE anti-join statement gives the rows, the
    empty keys and the orphan rows of the whole table; one more gives the example keys, only when there are orphans."""
    ws = extras
    recorder.opened = []
    job = check(client, ws)
    assert (job['state'], job['done'], job['total'], job['errors']) == ('done', N, N, [])
    view = overview(client, ws)
    assert view['summary'] == {'relationships': N, 'checked': N, 'not_checked': 0, 'never': 0, 'with_orphans': 4,
                               'sampled': 0, 'not_exact': 0}
    rels = by_pair(view)
    c = rels[('x_children', ('parent_id',), 'x_parents')]['check']
    assert (c['method'], c['exact'], c['sampled'], c['partial_keys'], c['not_whole']) == ('database', True, False, False, None)
    assert (c['rows'], c['null_rows'], c['rows_checked'], c['orphan_rows'], c['orphan_keys']) == (
        1000, EMPTY, 1000 - EMPTY, ORPHAN_ROWS, ORPHAN_KEYS)
    assert c['coverage'] == pytest.approx(1 - ORPHAN_ROWS / (1000 - EMPTY)) and c['label'] == 'measured on all 1,000 rows'
    assert c['keys_checked'] is None and c['keys_total'] == KEYS         # nothing was looked up
    assert c['shown'] == {
        'coverage': '92%', 'coverage_title': '874 of 950 rows with a key have a parent', 'orphans': '76',
        'empty_keys': '5%', 'empty_title': '50 of 1,000 rows have an empty key', 'exact': True, 'tag': '', 'notes': [],
        'how': 'Counted inside the database over the whole child table (one statement)', 'measured': 'measured on all 1,000 rows'}
    examples = c['orphan_examples']                                    # the keys with the most rows, from the database itself
    assert len(examples) == 19 and {e['key'][0] for e in examples} == {r[1] for r in CHILDREN if r[1] and r[1] > 200}
    assert [e['rows'] for e in examples] == sorted((e['rows'] for e in examples), reverse=True)
    assert sum(e['rows'] for e in examples) == ORPHAN_ROWS and c['examples_from_copy'] is False
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique_how']) == ('1-N', True, 'unique: primary key in the catalog')
    assert c['statements'] == 3                                        # the child's read, the count, the example keys

    # a key of two columns: an empty a or b is an empty key, never an orphan
    pair = rels[('x_pair_items', ('a', 'b'), 'x_pairs')]['check']
    assert (pair['method'], pair['rows'], pair['null_rows'], pair['orphan_rows'], pair['orphan_keys']) == (
        'database', 500, ITEM_EMPTY, ITEM_ORPHANS, 10)
    assert all(e['key'][0] == 11 and 1 <= e['key'][1] <= 10 for e in pair['orphan_examples']) and len(pair['orphan_examples']) == 10
    # no orphan: one statement less
    one = rels[('x_profiles', ('parent_id',), 'x_parents')]['check']
    assert (one['method'], one['orphan_rows'], one['coverage'], one['statements'], one['cardinality']) == ('database', 0, 1.0, 2, '1-1')
    assert one['shown']['coverage'] == '100%' and one['orphan_examples'] == []
    # no constraint makes x_parents.grp unique: the parent is asked once whether a key repeats
    many = next(r for r in rels.values() if r['from_columns'] == ['grp'])['check']
    assert (many['method'], many['cardinality'], many['parent_unique'], many['coverage']) == ('database', 'N-N', False, 1.0)
    assert many['parent_unique_how'] == 'not unique: a key occurs more than once in the parent'
    # text and a number are not compared inside the database: looked up as before, and the result says why
    odd = rels[('x_children', ('msisdn',), 'x_pairs')]['check']
    assert (odd['method'], odd['exact'], odd['coverage'], odd['orphan_keys']) == ('lookup', True, 0.0, 1000)
    assert odd['not_whole'] == ('x_children.msisdn is VARCHAR(12) and x_pairs.a is INTEGER, and columns of different types are '
                                'not compared inside the database')

    # what the database was asked: one anti-join per relationship it counted, everything bound, nothing but reads
    reads = recorder.reads()
    counts = [r for r in not_exists(reads) if ' AS a JOIN ' in r]
    assert len(counts) == 5 and len(not_exists(reads)) == 5 + 3       # ... and the example keys of the three with orphans
    (counted,) = [r for r in counts if 'c."parent_id"' in r and '"x_children"' in r]
    assert ' '.join(counted.split()) == (
        'SELECT a.n, a.e, o.k, o.r FROM (SELECT count(*) AS n, coalesce(sum(CASE WHEN (t."parent_id" IS NULL) THEN 1 ELSE 0 END), 0) '
        'AS e FROM "x_children" AS t) AS a JOIN (SELECT count(*) AS k, coalesce(sum(g.n), 0) AS r FROM (SELECT count(*) AS n '
        'FROM "x_children" AS c WHERE c."parent_id" IS NOT NULL AND NOT (EXISTS (SELECT 1 FROM "x_parents" AS p '
        'WHERE p."parent_id" = c."parent_id")) GROUP BY c."parent_id") AS g) AS o ON 1 = 1')
    assert not any(' IN (' in r and '"x_parents"' in r and '"parent_id" IN' in r for r in reads)       # no look-up for it
    assert all("'" not in r for r in not_exists(reads))
    for database in recorder.opened:
        assert database.blocked == []
        for statement in database.statements:
            check_read_only_sql(statement, dialect='sqlite')


def test_the_whole_table_is_counted_where_a_sample_and_5000_keys_fell_short(client, extras, monkeypatch):
    """The dry run of 13 Oct 2026: 40 orphan tickets listed, 310 in the table. The child is still READ on a sample -
    for how often a key repeats - but the rows, empty keys and orphans are those of the whole table."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 300)
    monkeypatch.setattr(config, 'RELATION_MAX_KEYS', 90)
    check(client, extras)
    view = overview(client, extras)
    c = by_pair(view)[('x_children', ('parent_id',), 'x_parents')]['check']
    assert (c['method'], c['exact'], c['sampled'], c['partial_keys']) == ('database', True, False, False)
    assert (c['rows'], c['null_rows'], c['orphan_rows'], c['orphan_keys']) == (1000, EMPTY, ORPHAN_ROWS, ORPHAN_KEYS)
    assert c['label'] == 'measured on all 1,000 rows' and c['shown']['orphans'] == str(ORPHAN_ROWS) and c['shown']['tag'] == ''
    assert (c['cardinality_on_sample'], c['child_sample_method']) == (True, 'first 300 rows, not random')
    assert len(c['orphan_examples']) == 19                             # all of them, though only 90 keys were read
    # msisdn -> x_pairs.a is text against a number: looked up, on the sample - and it says so
    assert view['summary']['sampled'] == 1 and view['summary']['not_exact'] == 1


def small_db(tmp_path, script, name='w.db'):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript(script)
    con.commit()
    con.close()
    return path


def test_a_count_that_runs_out_of_time_falls_back_and_the_row_says_at_least(client, ws, tmp_path, monkeypatch):
    """The anti-join ran into the statement timeout: the keys are looked up as before, what is found is a lower bound and
    says so in the list row, and no other relationship of that table asks for a count in this run."""
    path = small_db(tmp_path, '''
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY);
        CREATE TABLE products (product_id INTEGER PRIMARY KEY);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers (customer_id),
                             product_id INTEGER REFERENCES products (product_id));
        CREATE TABLE notes (note_id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders (order_id));
        WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 400)
        INSERT INTO orders SELECT i, 1 + i % 60, 1 + i % 25 FROM n;
        INSERT INTO customers SELECT DISTINCT customer_id FROM orders WHERE customer_id <= 50;
        INSERT INTO products SELECT DISTINCT product_id FROM orders WHERE product_id <= 20;
        INSERT INTO notes SELECT order_id, order_id + 350 FROM orders WHERE order_id <= 100;''')
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    real, asked = WholeTable.counts, []

    def slow(self, check):
        asked.append(self.child[1])
        if self.child[1] != 'orders':
            return real(self, check)
        check()
        self.on_slow()
        raise QueryFailed('timed out after 30 s', timeout=True)
    monkeypatch.setattr(WholeTable, 'counts', slow)
    job = check(client, ws)
    assert (job['state'], job['errors']) == ('done', [])               # not an error: the relationships were measured
    assert asked.count('orders') == 1 and asked.count('notes') == 1    # orders was asked once, for its first relationship
    rels = by_pair(overview(client, ws))
    first, second = [rels[('orders', (column,), parent)]['check'] for column, parent in (('customer_id', 'customers'), ('product_id', 'products'))]
    if first['not_whole'].startswith('an earlier'):
        first, second = second, first
    assert first['not_whole'] == 'the count over the whole table ran out of time'
    assert second['not_whole'] == 'an earlier count over this table ran out of time in this run'
    c = rels[('orders', ('customer_id',), 'customers')]['check']
    assert (c['method'], c['exact'], c['sampled'], c['rows']) == ('lookup', False, True, 100)
    assert c['orphan_rows'] == sum(1 for i in range(1, 101) if 1 + i % 60 > 50) == 10       # of the 60 in the table
    assert (c['shown']['orphans'], c['shown']['tag'], c['shown']['exact']) == ('≥ 10', 'first 100 rows', False)
    assert c['shown']['notes'][-1].startswith('The database was not asked to count the whole table: ')
    assert c['shown']['coverage'] == '90%' and c['label'] == 'measured on a sample of 100 rows'
    counted = rels[('notes', ('order_id',), 'orders')]['check']        # another child table: counted
    assert (counted['method'], counted['rows'], counted['orphan_rows'], counted['exact']) == ('database', 100, 50, True)

    # a count that fails for another reason: the same fall-back, with the reason - and the table is asked again
    asked.clear()
    monkeypatch.setattr(WholeTable, 'counts', lambda self, check: (asked.append(self.child[1]), (_ for _ in ()).throw(
        QueryFailed('no such collation sequence')))[1])
    check(client, ws)
    c = by_pair(overview(client, ws))[('orders', ('customer_id',), 'customers')]['check']
    assert (c['method'], c['not_whole']) == ('lookup', 'the count over the whole table failed (no such collation sequence)')
    assert asked.count('orders') == 2

    # and when it works, the same table says what it holds: 60 orphan rows, not "at least 10"
    monkeypatch.setattr(WholeTable, 'counts', real)
    check(client, ws)
    c = by_pair(overview(client, ws))[('orders', ('customer_id',), 'customers')]['check']
    assert (c['method'], c['rows'], c['orphan_rows'], c['shown']['orphans'], c['shown']['tag']) == ('database', 400, 60, '60', '')


def test_empty_keys_are_never_orphans_and_a_table_can_point_at_itself(tmp_path):
    path = small_db(tmp_path, '''
        CREATE TABLE pairs (a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY (a, b));
        CREATE TABLE items (item_id INTEGER PRIMARY KEY, a INTEGER, b INTEGER);
        INSERT INTO pairs VALUES (1, 1), (1, 2), (2, 1);
        INSERT INTO items (a, b) VALUES (1, 1), (1, 1), (1, 2), (2, 2), (2, 2), (9, 9), (NULL, 1), (1, NULL), (NULL, NULL), (NULL, 9);
        CREATE TABLE staff (staff_id INTEGER PRIMARY KEY, boss_id INTEGER);
        INSERT INTO staff VALUES (1, NULL), (2, 1), (3, 1), (4, 2), (5, 77), (6, 77), (7, 88);''')
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(path)))
    conn = DatabaseData(db, None, 'counts')
    pairs = WholeTable(conn, (None, 'items'), ['a', 'b'], (None, 'pairs'), ['a', 'b'])
    assert pairs.counts(lambda: None) == (10, 4, 2, 3)                 # rows, empty keys, orphan keys, orphan rows
    assert pairs.examples(lambda: None, 20) == [([2, 2], 2), ([9, 9], 1)]
    assert pairs.examples(lambda: None, 1) == [([2, 2], 2)] and pairs.statements == 3
    own = WholeTable(conn, (None, 'staff'), ['boss_id'], (None, 'staff'), ['staff_id'])      # child and parent: one table
    assert own.counts(lambda: None) == (7, 1, 2, 3) and own.examples(lambda: None, 20) == [([77], 2), ([88], 1)]
    assert db.blocked == [] and all(s.startswith(('SELECT', 'PRAGMA')) for s in db.statements)
    conn.close()
    db.dispose()


# -------------------------------------------------------------------- date keys

DATES = '''
CREATE TABLE days (day DATE PRIMARY KEY, label VARCHAR(10));
CREATE TABLE shifts (shift_id INTEGER PRIMARY KEY, day DATE REFERENCES days (day));
CREATE TABLE bookings (booking_id INTEGER PRIMARY KEY, day DATE REFERENCES days (day));
CREATE TABLE slots (starts_at DATETIME PRIMARY KEY, room VARCHAR(10));
CREATE TABLE visits (visit_id INTEGER PRIMARY KEY, starts_at DATETIME REFERENCES slots (starts_at));
'''


def dates_db(tmp_path):
    """Days and moments as SQLite keeps them: the text they were given.
    shifts    60 rows, every day exists                       -> 0 orphans
    bookings  50 rows: 5 empty, 6 on days that do not exist, and 3 written with a time of day - '2025-01-05 00:00:00' is
              not the text '2025-01-05', and SQLite itself does not call them equal -> 9 orphan rows with 5 different keys
    visits    40 rows on existing slots, 4 on a slot that does not exist"""
    path = tmp_path / 'dates.db'
    con = sqlite3.connect(path)
    con.executescript(DATES)
    con.executemany('INSERT INTO days VALUES (?, ?)', [(f'2025-01-{d:02d}', f'day {d}') for d in range(1, 31)])
    con.executemany('INSERT INTO shifts (day) VALUES (?)', [(f'2025-01-{1 + i % 30:02d}',) for i in range(60)])
    bookings = [f'2025-01-{1 + i % 30:02d}' for i in range(36)] + [None] * 5 + ['2025-02-01'] * 3 + ['2025-02-02'] * 2 + ['2024-12-31']
    bookings += ['2025-01-05 00:00:00', '2025-01-05 00:00:00', '2025-01-06 00:00:00']
    con.executemany('INSERT INTO bookings (day) VALUES (?)', [(v,) for v in bookings])
    con.executemany('INSERT INTO slots VALUES (?, ?)', [(f'2025-03-01 {h:02d}:30:00', f'room {h}') for h in range(8, 18)])
    con.executemany('INSERT INTO visits (starts_at) VALUES (?)',
                    [(f'2025-03-01 {8 + i % 10:02d}:30:00',) for i in range(40)] + [('2025-03-01 19:30:00',)] * 4)
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize('whole_table', [True, False], ids=['counted in the database', 'looked up'])
def test_a_date_key_on_sqlite_matches(client, ws, tmp_path, monkeypatch, recorder, whole_table):
    """The dry run's problem 1: PRD_RATE_CARD.EFFECTIVE_FROM found no parent at all, because '2025-10-01' was looked up
    as '2025-10-01 00:00:00.000000'. Both ways of measuring give the same numbers - the database's own."""
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', whole_table)
    path = dates_db(tmp_path)
    assert client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)}).status_code == 200
    recorder.opened = []
    job = check(client, ws)
    assert (job['state'], job['errors'], job['total']) == ('done', [], 3)
    rels = by_pair(overview(client, ws))
    method = 'database' if whole_table else 'lookup'
    full = rels[('shifts', ('day',), 'days')]['check']                 # every child row matches
    assert (full['method'], full['rows'], full['null_rows'], full['orphan_rows'], full['coverage']) == (method, 60, 0, 0, 1.0)
    assert (full['cardinality'], full['shown']['coverage']) == ('1-N', '100%')
    some = rels[('bookings', ('day',), 'days')]['check']               # real orphans
    assert (some['method'], some['rows'], some['null_rows'], some['orphan_rows'], some['orphan_keys']) == (method, 50, 5, 9, 5)
    assert some['coverage'] == pytest.approx(36 / 45) and some['shown']['coverage'] == '80%'
    assert [(e['key'][0], e['rows']) for e in some['orphan_examples']] == [
        ('2025-02-01', 3), ('2025-01-05 00:00:00', 2), ('2025-02-02', 2), ('2024-12-31', 1), ('2025-01-06 00:00:00', 1)]
    moments = rels[('visits', ('starts_at',), 'slots')]['check']
    assert (moments['method'], moments['rows'], moments['orphan_rows'], moments['orphan_keys']) == (method, 44, 4, 1)
    assert moments['orphan_examples'] == [{'key': ['2025-03-01 19:30:00'], 'rows': 4}]
    # what SQLite itself says
    con = sqlite3.connect(path)
    assert con.execute('SELECT COUNT(*) FROM bookings b WHERE b.day IS NOT NULL AND NOT EXISTS '
                       '(SELECT 1 FROM days d WHERE d.day = b.day)').fetchone()[0] == 9
    con.close()
    lookups = [r for r in recorder.reads() if ' IN (' in r]
    assert bool(lookups) is not whole_table and all("'" not in r for r in recorder.reads() if 'sqlite_master' not in r)


def test_date_keys_in_files_match_by_the_day(tmp_path):
    """Files: the keys are compared inside the in-memory copy. A day matches the same day, written as a day or as its
    midnight; 13:00 on that day is not the day."""
    from app.analysis.relations import FileParent
    from app.profiling.data import FileData
    folder = tmp_path / 'files'
    folder.mkdir()
    (folder / 'days.csv').write_text('day,label\n' + ''.join(f'2025-01-{d:02d},day {d}\n' for d in range(1, 21)), encoding='utf-8')
    (folder / 'shifts.csv').write_text('shift_id,day\n' + ''.join(f'{i},2025-01-{1 + i % 20:02d}\n' for i in range(40)), encoding='utf-8')
    (folder / 'events.csv').write_text('event_id,at\n' + ''.join(f'{i},2025-01-{1 + i % 20:02d} 00:00:00\n' for i in range(30))
                                       + '30,2025-01-03 13:00:00\n31,2025-01-03 13:00:00\n32,2025-02-01 00:00:00\n33,\n', encoding='utf-8')
    parent = FileParent(FileData(folder / 'days.csv', threshold=float('inf')).open())
    assert dict(parent.copy.columns)['day'] == 'DATE' and parent.family('day') == 'date'
    shifts = FileData(folder / 'shifts.csv').open()
    all_there = measure(shifts, ['day'], parent, ['day'], lambda: None)
    assert (all_there['rows'], all_there['orphan_rows'], all_there['coverage'], all_there['exact']) == (40, 0, 1.0, True)
    events = FileData(folder / 'events.csv').open()
    assert dict(events.columns)['at'].startswith('TIMESTAMP')
    some = measure(events, ['at'], parent, ['day'], lambda: None)
    assert (some['rows'], some['null_rows'], some['orphan_rows'], some['orphan_keys'], some['method']) == (34, 1, 3, 2, 'lookup')
    assert [(str(key[0]), n) for key, n in some['orphan_keys_raw']] == [('2025-01-03 13:00:00', 2), ('2025-02-01 00:00:00', 1)]
    for copy in (shifts, events):
        copy.close()
    parent.close()


def test_results_saved_before_the_counts_said_whether_they_are_exact():
    """A result saved by the version before 13 Oct 2026 has no 'method' and no 'exact': it is printed by the same
    rules, from what it does say - these are the two rows of the dry run."""
    old = {'status': 'checked', 'sampled': False, 'rows': 55406, 'label': 'measured on all 55,406 rows', 'null_rows': 0,
           'keys_total': 37026, 'keys_checked': 5000, 'rows_checked': 7497, 'coverage': 0.99466, 'orphan_rows': 40,
           'orphan_keys': 40, 'partial_keys': True}
    view = shown(old)
    assert (view['orphans'], view['tag'], view['exact'], view['coverage']) == ('≥ 40', '5,000 of 37,026 keys looked up', False, '99.5%')
    cdr = {**old, 'sampled': True, 'sample_method': 'first 100,000 rows, not random', 'rows': 100000, 'keys_total': 26513,
           'rows_checked': 18870, 'orphan_rows': 4, 'label': 'measured on a sample of 100,000 rows'}
    view = shown(cdr)
    assert (view['orphans'], view['coverage']) == ('≥ 4', '>99.9%')    # the report printed "100%" next to 4 orphan rows
    assert view['tag'] == '5,000 of 26,513 keys looked up · first 100,000 rows' and len(view['notes']) == 2
    assert limits({'status': 'checked', 'rows': 10, 'partial_keys': False, 'sampled': False}) == ('', [])
    assert shown({'status': 'not_checked'}) is None and shown(None) is None
    clean = shown({'status': 'checked', 'rows': 0, 'rows_checked': 0, 'orphan_rows': 0, 'null_rows': 0})
    assert (clean['coverage'], clean['orphans'], clean['empty_keys'], clean['exact']) == ('', '0', '', True)


# ------------------------------------------------------ time budgets and failures

def test_a_check_that_runs_out_of_time_is_not_checked_and_not_retried(client, extras, recorder, monkeypatch, lookups):
    real = DatabaseParent.lookup
    asked = []

    def slow(self, columns, keys, check):
        asked.append(self.table)
        if self.table == 'x_pairs':
            raise QueryFailed('timed out after 30 s', timeout=True)
        return real(self, columns, keys, check)
    monkeypatch.setattr(DatabaseParent, 'lookup', slow)
    job = check(client, extras)
    assert job['state'] == 'done' and job['done'] == job['total'] == N       # the job moved on
    assert {'table': 'x_pair_items → x_pairs', 'message': 'not checked (timeout)', 'timeout': True} in job['errors']
    assert len(job['errors']) == 2 and all(e['timeout'] for e in job['errors'])
    rels = by_pair(overview(client, extras))
    timed_out = rels[('x_pair_items', ('a', 'b'), 'x_pairs')]['check']
    assert {k: timed_out[k] for k in ('status', 'reason', 'reason_code', 'label')} == {
        'status': 'not_checked', 'reason': 'timeout', 'reason_code': 'timeout', 'label': 'not checked (timeout)'}
    assert asked.count('x_pairs') == 2                                  # once for each of its two relationships: no retry
    assert not any('JOIN' in r.upper() or 'NOT EXISTS' in r.upper() for r in recorder.reads())
    assert rels[('x_children', ('parent_id',), 'x_parents')]['check']['status'] == 'checked'


def test_every_relationship_has_its_own_time_budget(client, extras, monkeypatch):
    budgets = []
    real = relations_module.budget_check

    def budget(seconds, also=None, clock=time.monotonic):
        budgets.append(seconds)
        return real(seconds, also, clock)
    monkeypatch.setattr(relations_module, 'budget_check', budget)
    check(client, extras)
    assert budgets and set(budgets) == {config.RELATION_CHECK_BUDGET_S} == {60}
    assert len(budgets) == 5 + N                                        # one per child table read, one per relationship

    monkeypatch.setattr(relations_module, 'budget_check', lambda seconds, also=None, clock=time.monotonic: real(-1, also, clock))
    job = check(client, extras)
    assert job['state'] == 'done' and len(job['errors']) == N and all(e['timeout'] for e in job['errors'])
    assert all(r['check']['label'] == 'not checked (timeout)' for r in overview(client, extras)['relationships'])


def test_tables_in_different_sources_are_not_checked(client, ws, tmp_path, demo_csv):
    """accounts comes from a database, customers only from files: no check is run across the two."""
    path = tmp_path / 'part.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, customer_id INTEGER);'
                      'INSERT INTO accounts VALUES (1, 1), (2, 999);')
    con.commit()
    con.close()
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'customers.csv').write_bytes((demo_csv / 'customers.csv').read_bytes())
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    view = overview(client, ws)
    (rel,) = view['relationships']
    assert (rel['from'], rel['to'], rel['checkable']) == ('accounts', 'customers', False)
    assert rel['check']['label'] == 'not checked: tables are in different sources'       # known before anything runs
    job = check(client, ws)
    assert (job['state'], job['done'], job['errors']) == ('done', 1, [])
    (rel,) = overview(client, ws)['relationships']
    assert rel['check'] == {'status': 'not_checked', 'reason': 'tables are in different sources', 'shown': None,
                            'reason_code': 'different_sources', 'label': 'not checked: tables are in different sources'}


def test_tables_known_from_sql_files_only_have_nothing_to_check(client, ws, demo_sql):
    client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(demo_sql)})
    view = overview(client, ws)
    assert len(view['relationships']) == 16 and not any(r['checkable'] for r in view['relationships'])
    assert view['relationships'][0]['check']['reason_code'] == 'no_data'
    assert 'has no data (structure only, no data to profile)' in view['relationships'][0]['check']['label']
    job = check(client, ws)
    assert job['state'] == 'done' and overview(client, ws)['summary']['not_checked'] == 16


# ------------------------------------------------------------------------ files

def test_checks_between_files_of_one_source(client, ws, tmp_path):
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'customers.csv').write_text('customer_id,city\n' + ''.join(f'{i},City{i % 4}\n' for i in range(1, 41)), encoding='utf-8')
    (folder / 'orders.csv').write_text('order_id,customer_id,total\n' + ''.join(
        f'{i},{"" if i % 10 == 0 else 1 + i % 45},{i * 2}\n' for i in range(1, 201)), encoding='utf-8')
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    job = check(client, ws)
    assert (job['state'], job['errors']) == ('done', [])
    (rel,) = overview(client, ws)['relationships']
    c = rel['check']
    assert (rel['kind'], rel['from'], rel['to']) == ('names', 'orders', 'customers')
    assert (c['rows'], c['null_rows'], c['keys_total'], c['orphan_keys']) == (200, 20, 45, 5)
    assert {e['key'][0] for e in c['orphan_examples']} == {41, 42, 43, 44, 45} and c['label'] == 'measured on all 200 rows'
    # a file declares no key: uniqueness is what the data says, and the whole parent file was read
    assert c['parent_unique_how'] == 'unique: no key occurs twice in the parent (checked now; no constraint says so)'
    assert (c['cardinality'], c['cardinality_proven']) == ('1-N', True)
    assert sorted(p.name for p in (tmp_path / 'data').iterdir()) == ['explorer.db']
    # files are joined in memory, which costs no database anything: every key is looked up, so the counts are exact
    assert (c['method'], c['exact'], c['keys_checked'], c['partial_keys'], c['not_whole']) == ('lookup', True, 45, False, None)


def test_every_key_of_a_file_is_looked_up(client, ws, tmp_path, monkeypatch):
    """The limit of 5,000 keys protects a database. Between two files it would only make the counts a lower bound."""
    monkeypatch.setattr(config, 'RELATION_MAX_KEYS', 30)
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'customers.csv').write_text('customer_id,city\n' + ''.join(f'{i},City{i % 4}\n' for i in range(1, 401)), encoding='utf-8')
    (folder / 'orders.csv').write_text('order_id,customer_id\n' + ''.join(f'{i},{1 + i % 450}\n' for i in range(1, 2001)), encoding='utf-8')
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    assert check(client, ws)['errors'] == []
    (rel,) = overview(client, ws)['relationships']
    c = rel['check']
    assert (c['keys_total'], c['keys_checked'], c['partial_keys'], c['exact']) == (450, 450, False, True)
    assert (c['orphan_keys'], c['orphan_rows'], c['shown']['orphans'], c['shown']['tag']) == (50, 200, '200', '')


# ----------------------------------------------------------------- suggestions

class ProbeCopy:
    """A child's copy for the first look: canned values per column, and whether whole blocks were sampled."""

    def __init__(self, values, block):
        self.values, self.plan, self.closed = values, {'kind': 'percent', 'block': block} if block is not None else None, False

    def run(self, statement):
        column = next(name for name in self.values if f'"{name}"' in str(statement) or f' {name},' in str(statement))
        return [(value, 1) for value in self.values[column]]

    def close(self):
        self.closed = True


def test_the_first_look_reads_a_table_again_when_its_blocks_give_too_few_values():
    """Round 4. The first look asks for whole blocks (SAMPLE BLOCK on Oracle). When they give a column fewer than
    FIRST_LOOK_VALUES values - a small table can give few blocks or none - the table is read again the usual way
    (no blocks asked), once."""
    assert relations_module.FIRST_LOOK_VALUES == 3

    def look(*answers):
        asked, copies = [], list(answers)

        class Run:
            def child_copy(self, child, columns, check, sample_rows=None, blocks=False):
                asked.append((sample_rows, blocks))
                return copies[len(asked) - 1], list(columns)

        values, twice = relations_module._Run.look_values(Run(), None, ['code', 'ref'], None)
        assert all(c.closed for c in answers[:len(asked)])
        return values, twice, asked

    many = {'code': ['A', 'B', 'C', 'D'], 'ref': [1, 2, 3]}
    few = {'code': ['A', 'B', 'C', 'D'], 'ref': [7]}
    probe = config.RELATION_PROBE_ROWS
    # enough values from the blocks: one read
    assert look(ProbeCopy(many, True)) == ({'CODE': ['A', 'B', 'C', 'D'], 'REF': [1, 2, 3]}, False, [(probe, True)])
    # one column with too few: the table is read again, without blocks, and that read's values count
    assert look(ProbeCopy(few, True), ProbeCopy(many, False)) == (
        {'CODE': ['A', 'B', 'C', 'D'], 'REF': [1, 2, 3]}, True, [(probe, True), (probe, False)])
    # ... once, whatever the second read gives
    assert look(ProbeCopy(few, True), ProbeCopy({'code': [], 'ref': []}, False))[1:] == (True, [(probe, True), (probe, False)])
    # not sampled by blocks (another engine, a small table read whole, a file): its few values are all there are
    for plan in (False, None):
        assert look(ProbeCopy(few, plan)) == ({'CODE': ['A', 'B', 'C', 'D'], 'REF': [7]}, False, [(probe, True)])


def test_links_are_suggested_by_values(client, extras, recorder):
    ws = extras
    recorder.opened = []
    job = check(client, ws, relationships=[], suggest=True)
    assert (job['state'], job['errors'], job['suggest'], job['phase']) == ('done', [], True, 'suggesting')
    assert job['total'] == job['done'] > 0
    view = overview(client, ws)
    (s,) = view['suggestions']
    assert (s['from'], s['from_columns'], s['to'], s['to_columns'], s['status']) == (
        'x_children', ['parent_code'], 'x_parents', ['code'], 'suggested')
    assert s['confidence'] == pytest.approx(190 / 209, abs=1e-4)        # the overlap IS the confidence
    e = s['evidence']
    assert (e['values_checked'], e['values_found'], e['overlap']) == (KEYS, KEYS - ORPHAN_KEYS, 0.9091)
    assert e['text'] == '190 of 209 distinct values of x_children.parent_code exist in x_parents.code'
    assert e['label'] == 'measured on all 1,000 rows' and e['parent_unique_proven'] is True
    assert 'in the catalog' in e['parent_key'] and e['name_hint'] == '"parent" in the column name points at x_parents'
    assert s['cardinality'] == '1-N' and e['child_max_per_key'] >= 4
    # the candidates: same type family, the parent column a key or proven unique, not related already
    lookups = [r for r in recorder.reads() if ' IN (' in r]
    assert lookups and all(r.count('?') <= config.RELATION_IN_CHUNK for r in lookups)
    assert not any('"label" IN' in r or '"grp" IN' in r for r in lookups)                # not unique: never a parent
    assert not any('FROM "x_parents" \nWHERE "parent_id" IN' in r and 'x_children' in r for r in lookups)
    assert view['settings']['max_pairs'] == 200 and view['settings']['suggest_values'] == 1000


def test_personal_values_of_a_suggestion_stay_in_memory(client, extras, tmp_path):
    """The values looked up are codes and phone numbers; what is saved is counts."""
    check(client, extras, suggest=True)
    blob = (tmp_path / 'data' / 'explorer.db').read_bytes()
    for raw in (b'P0001', b'P0150', b'P0220', b'96650000', b'9665'):
        assert raw not in blob, raw
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    (evidence,) = [json.loads(r[0]) for r in con.execute('SELECT evidence FROM value_links')]
    con.close()
    assert not any(isinstance(v, list) for v in evidence.values())
    assert set(evidence) == {'overlap', 'values_checked', 'values_found', 'sampled', 'rows', 'sample_method', 'label',
                             'parent_key', 'parent_unique_proven', 'child_max_per_key', 'name_hint', 'score', 'text'}


def test_accepting_a_suggestion_makes_it_a_relationship(client, extras):
    ws = extras
    check(client, ws, relationships=[], suggest=True)
    (s,) = overview(client, ws)['suggestions']
    before = client.get(f'/api/workspaces/{ws}/model').json()['stats']
    assert before['values'] == 0
    accepted = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'accept'})
    assert accepted.status_code == 200 and accepted.json()['status'] == 'accepted' and accepted.json()['decided_at']
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['stats']['values'] == 1 and model['stats']['relationships'] == before['relationships'] + 1
    (link,) = [r for r in model['relationships'] if r['source'] == 'values']
    assert (link['from'], link['from_columns'], link['to'], link['to_columns']) == ('x_children', ['parent_code'], 'x_parents', ['code'])
    # The share as measured, 190 of 209 values: 90.9%, as the suggestion said, not a rounded 91%.
    assert link['confidence'] == pytest.approx(190 / 209) and link['cardinality'] == '1-N'
    assert link['evidence'] == ['90.9% of the values checked exist in x_parents; accepted by you']
    # It is measured at once: the list and the report never show an accepted link as "never checked".
    started = accepted.json()['job']
    assert (started['kind'], started['total']) == ('relations', 1) and wait(client, started)['state'] == 'done'
    view = overview(client, ws)
    assert view['suggestions'] == [] and [x['key'] for x in view['accepted']] == [s['key']]
    (measured,) = [r for r in view['relationships'] if r['kind'] == 'values']
    assert (measured['check']['status'], measured['check']['orphan_keys'], measured['check']['rows']) == ('checked', 19, 1000)
    assert view['summary']['never'] == N and view['summary']['checked'] == 1       # that one, and no other
    assert view['accepted'][0]['overlap_text'] == '90.9%'
    # The report says the same share as the suggestion, in its HTML and in its Excel file.
    page = client.get(f'/api/workspaces/{ws}/report?format=html').text
    assert '<span class="nowrap">values</span> 90.9%' in page and '<span class="nowrap">values</span> 91%' not in page
    book = openpyxl.load_workbook(io.BytesIO(client.get(f'/api/workspaces/{ws}/report?format=xlsx').content))
    sheet = book['Relationships']
    header = [c.value for c in sheet[1]]
    (row,) = [[c.value for c in r] for r in sheet.iter_rows(min_row=2) if r[header.index('Kind')].value == 'values']
    assert row[header.index('Confidence %')] == '90.9%'
    # it is checked like any other relationship from now on
    check(client, ws)
    c = next(r for r in overview(client, ws)['relationships'] if r['kind'] == 'values')['check']
    assert (c['orphan_keys'], c['cardinality'], c['coverage'] < 1) == (19, '1-N', True)
    assert 'in the catalog' in c['parent_unique_how'] and c['orphan_examples'][0]['key'][0].startswith('P02')
    # the ER graph's data and both exports carry it, with its own line style
    details = client.get(f'/api/workspaces/{ws}/tables/x_children').json()
    assert any(r['source'] == 'values' and r['other'] == 'x_parents' for r in details['relationships'])
    drawio = client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=values')
    edges = [cell.get('style') for cell in ET.fromstring(drawio.text).iter('mxCell') if cell.get('edge') == '1']
    assert len(edges) == 1 and 'dashed=1;dashPattern=1 3;' in edges[0]                 # dotted
    assert 'dotted line = accepted from value overlap' in drawio.text
    others = client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=names,joins').text
    assert 'dashPattern' not in others and 'dashed=1' in others                        # the other inferred kinds: dashed
    assert client.get(f'/api/workspaces/{ws}/export?format=png&kinds=values').status_code == 200
    kinds = {k['id']: k for k in client.get('/api/meta').json()['relationship_kinds']}
    assert kinds['values']['label'] == 'from values' and 'accepted by you' in kinds['values']['hint']


def test_a_rejected_suggestion_is_hidden_and_remembered(client, extras, tmp_path, app):
    ws = extras
    check(client, ws, relationships=[], suggest=True)
    (s,) = overview(client, ws)['suggestions']
    rejected = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'reject'})
    assert rejected.json()['status'] == 'rejected'
    view = overview(client, ws)
    assert view['suggestions'] == [] and [x['key'] for x in view['rejected']] == [s['key']]
    assert client.get(f'/api/workspaces/{ws}/model').json()['stats']['values'] == 0
    check(client, ws, relationships=[], suggest=True)                   # the next run does not bring it back
    assert overview(client, ws)['suggestions'] == []
    other = client.post('/api/workspaces', json={'name': 'other'}).json()['id']     # remembered per workspace
    client.post(f'/api/workspaces/{other}/sources/db', json={'engine': 'sqlite', 'database': str(tmp_path / 'extras.db')})
    check(client, other, relationships=[], suggest=True)
    assert len(overview(client, other)['suggestions']) == 1
    # "reset" puts it back among the suggestions; a wrong key or action is refused
    assert client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'reset'}).json()['status'] == 'suggested'
    assert len(overview(client, ws)['suggestions']) == 1
    assert client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': 'nope', 'action': 'accept'}).status_code == 404
    assert client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'maybe'}).status_code == 400
    # an accepted link can be taken back - and what was measured for it goes with it
    accepted = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'accept'}).json()
    wait(client, accepted['job'])
    assert client.get(f'/api/workspaces/{ws}/model').json()['stats']['values'] == 1
    assert s['key'] in app.state.relations.store.checks(ws)
    client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'reject'})
    assert client.get(f'/api/workspaces/{ws}/model').json()['stats']['values'] == 0
    assert s['key'] not in app.state.relations.store.checks(ws)


def test_an_accepted_link_is_not_checked_while_another_job_runs(client, extras, monkeypatch):
    """One job per workspace: when one is running, the link is accepted all the same and says why it was not measured."""
    ws = extras
    check(client, ws, relationships=[], suggest=True)
    (s,) = overview(client, ws)['suggestions']
    hold, reached = threading.Event(), threading.Event()
    real = relations_module._Run.check_all

    def held(self, wanted, pairs=()):
        reached.set()
        while not hold.is_set():
            self.job.check()
            time.sleep(0.005)
        return real(self, wanted, pairs)
    monkeypatch.setattr(relations_module._Run, 'check_all', held)
    running = client.post(f'/api/workspaces/{ws}/relations/check', json={}).json()
    assert reached.wait(10)
    accepted = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'accept'})
    assert accepted.status_code == 200 and accepted.json()['status'] == 'accepted'
    assert (accepted.json()['job'], accepted.json()['not_checked_now']) == (None, 'another job is running in this workspace')
    hold.set()
    assert wait(client, running)['state'] == 'done'
    assert client.get(f'/api/workspaces/{ws}/model').json()['stats']['values'] == 1


def test_the_pairs_to_measure_are_chosen_by_their_values(client, extras, recorder, monkeypatch):
    """The dry run's problem 4: 6,655 pairs could hold the same values, the 200 likeliest BY NAME were tried, and the
    links that only their values show were never reached. Every pair now gets a first look at a few values, and only
    the pairs that share values are measured - however few may be measured in a run."""
    recorder.opened = []
    job = check(client, extras, relationships=[], suggest=True)
    (note,) = job['notes']
    pairs = int(note.split(' column pairs')[0])
    assert pairs >= 20 and job['total'] == 1 == job['done']             # ... of which ONE shares values
    assert note == (f'{pairs} column pairs could hold the same values. A first look at 30 values of each column found 1 that '
                    f'share values; all of them were measured.')
    (s,) = overview(client, extras)['suggestions']
    assert (s['from_columns'], s['to_columns'], s['overlap_text']) == (['parent_code'], ['code'], '90.9%')
    # the first look: one small read a child table, and for a parent key ONE look-up of the values of every column
    # that might point at it - bound lists, never a value in the text
    reads = recorder.reads()
    small = [r for r in reads if ' AS c0' in r]
    assert small and len([r for r in small if 'FROM "x_children"' in r]) == 2      # the first look, then the full check
    assert all("'" not in r for r in reads if 'sqlite_master' not in r)
    for database in recorder.opened:
        assert database.blocked == []
    # with room for one pair only, it is the one that shares values - not the likeliest by its name
    monkeypatch.setattr(config, 'RELATION_MAX_PAIRS', 1)
    job = check(client, extras, relationships=[], suggest=True)
    assert job['total'] == 1 and len(overview(client, extras)['suggestions']) == 1
    # a schema with more pairs than get a first look: the note says how many did
    monkeypatch.setattr(config, 'RELATION_LOOK_PAIRS', 5)
    job = check(client, extras, relationships=[], suggest=True)
    assert f'{pairs} column pairs could hold the same values; the 5 likeliest by their names got a first look' in job['notes'][0]
    assert '(DATA_EXPLORER_RELATION_LOOK_PAIRS)' in job['notes'][0]


def numbers_db(tmp_path):
    """branches   60 rows, branch_id 1 .. 60: a key that counts up from 1
    accounts   200 rows, account_no 500001 .. 500200: a key that does not start near 1
    visits     400 rows: weekday 1-7, minutes 1-50 and office 1-40 are small whole numbers that EVERY key counting
               from 1 holds; branch_ref points at a branch by its name; holder holds account numbers under a
               name that says nothing; paid is money (3.5, 7.0 ...)"""
    path = tmp_path / 's.db'
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE branches (branch_id INTEGER PRIMARY KEY, city TEXT);
        CREATE TABLE accounts (account_no INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE visits (visit_id INTEGER PRIMARY KEY, weekday INTEGER, minutes INTEGER, office INTEGER,
                             branch_ref INTEGER, holder INTEGER, paid NUMERIC(8, 2), sort_branch INTEGER);''')
    con.executemany('INSERT INTO branches VALUES (?, ?)', [(i, f'City {i}') for i in range(1, 61)])
    con.executemany('INSERT INTO accounts VALUES (?, ?)', [(500000 + i, f'Account {i}') for i in range(1, 201)])
    con.executemany('INSERT INTO visits VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [
        (i, 1 + i % 7, 1 + i % 50, 1 + i % 40, 1 + i % 55, 500001 + i % 180, (1 + i % 40) * 3.5, 1 + i % 30) for i in range(1, 401)])
    con.commit()
    con.close()
    return path


def test_numbers_are_not_suggested_to_a_key_that_counts_from_one(client, ws, tmp_path, recorder):
    """A count, a sort order, a number of minutes: whole numbers from 1 up, and so is every key that counts up from 1.
    USG_DAILY_TRAFFIC.ACTIVE_SUBS "matched" the keys of four tables completely, and LKP_STATUS_CODE.SORT_ORDER the
    key of the orders. Such a column is only suggested to such a key when its name points there."""
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(numbers_db(tmp_path))})
    recorder.opened = []
    job = check(client, ws, relationships=[], suggest=True)
    assert job['errors'] == []
    found = {(s['from_columns'][0], s['to'], s['to_columns'][0]): s for s in overview(client, ws)['suggestions']}
    # office, minutes, weekday, sort_branch and paid all "exist" in branch_id 1..60 - and are not suggested
    assert set(found) == {('branch_ref', 'branches', 'branch_id'), ('holder', 'accounts', 'account_no')}
    named = found[('branch_ref', 'branches', 'branch_id')]
    assert named['evidence']['name_hint'] == '"branch" in the column name points at branches' and named['overlap_text'] == '100%'
    plain = found[('holder', 'accounts', 'account_no')]                # no hint - and no need: that key does not count from 1
    assert plain['evidence']['name_hint'] is None and plain['evidence']['values_found'] == 180
    assert job['total'] == 2                                            # the others did not even go on to the full check
    # what "counts up from 1" cost: the key's two ends, and one look-up of 40 numbers between them
    reads = recorder.reads()
    assert [r for r in reads if r.startswith('SELECT min("branch_id") AS low, max("branch_id") AS high')] == [
        'SELECT min("branch_id") AS low, max("branch_id") AS high \nFROM "branches"']
    assert len([r for r in reads if 'min("account_no")' in r]) == 1


@pytest.mark.parametrize('low, high, held, serial', [
    (1, 60, range(1, 61), True), (1, 48979, range(1, 48980), True), (0, 9, range(0, 10), True),
    (1, 1000, range(1, 1001, 2), False),            # every second number: half of what is asked for is there
    (1, 1000, [n for n in range(1, 1001) if n % 10], True),      # one in ten is missing: still a key in which a number is found
    (5000001, 5200224, range(5000001, 5200225), False),          # dense, but it does not start near 1
    (1001, 2000, range(1001, 2001), False), (1000, 2000, range(1000, 2001), True),
    ('A001', 'Z999', [], False), (None, None, [], False), (7, 7, [7], False), (1.5, 99.5, [], False),
])
def test_a_key_that_counts_up_from_about_one(low, high, held, serial):
    held = set(held)

    class Parent:
        asked = []

        def bounds(self, column, check):
            return low, high

        def lookup(self, columns, keys, check):
            Parent.asked.append(keys)
            return {i: 1 for i, (value,) in enumerate(keys) if value in held}

    class Target:
        class table:
            key = 'T'

    class Column:
        name = 'k'

    run = relations_module._Run.__new__(relations_module._Run)
    run.serials = {}
    assert run.serial(Target, Column, Parent(), 'k', lambda: None) is serial
    assert run.serial(Target, Column, Parent(), 'k', lambda: None) is serial and len(Parent.asked) <= 1      # asked once a key
    assert all(len(keys) <= 40 for keys in Parent.asked)


def test_a_table_prefix_and_a_word_inside_a_name_are_no_hint():
    """The dry run's accidents had a hint in their names: "subs" for every table whose name starts with SUB_, and the
    "order" of SORT_ORDER for the table of orders."""
    from app.analysis.relations import pointer_words, table_prefixes
    tables = ['SUB_DEVICE', 'SUB_SIM_CARD', 'SUB_SUBSCRIPTION', 'SAL_ORDER', 'SAL_CHANNEL', 'SAL_DEALER', 'LKP_STATUS_CODE',
              'USG_DAILY_TRAFFIC', 'CRM_CUSTOMER', 'HR_EMPLOYEE', 'main.PRD_PLAN']
    prefixes = table_prefixes(tables)
    assert prefixes == {'sub', 'sal'}                                   # the first word of several tables
    assert table_prefixes(['customers', 'orders', 'order_items']) == set()
    score = lambda child, table, key: affinity(child, table, key, prefixes)[0]
    # the accidents
    assert score('ACTIVE_SUBS', 'SUB_DEVICE', 'DEVICE_ID') == 0 and score('ACTIVE_SUBS', 'SUB_SIM_CARD', 'SIM_ID') == 0
    assert score('ACTIVE_SUBS', 'SUB_SUBSCRIPTION', 'SUBSCRIPTION_ID') == 0
    assert score('SORT_ORDER', 'SAL_ORDER', 'ORDER_ID') == 0
    # what is a hint: the name is that word, a key word follows it, or the whole name is the table's name
    assert pointer_words('CUSTOMER_ID') == ['customer'] and pointer_words('cust') == ['cust']
    assert pointer_words('BILLING_CUSTOMER_ID') == ['customer'] and pointer_words('ASSIGNED_EMP_ID') == ['emp']
    assert pointer_words('SORT_ORDER') == [] and pointer_words('ACTIVE_SUBS') == [] and pointer_words('ID') == []
    assert affinity('ORDER_NO', 'SAL_ORDER', 'ORDER_ID', prefixes) == (2, '"order" in the column name points at SAL_ORDER')
    assert score('SALES_ORDER_ID', 'SAL_ORDER', 'ORDER_ID') == 2 and score('DEVICE_REF', 'SUB_DEVICE', 'DEVICE_ID') == 2
    assert affinity('SALES_CHANNEL', 'SAL_CHANNEL', 'CHANNEL_CODE', prefixes) == (2, 'the column is named like SAL_CHANNEL')
    assert score('SUB_ID', 'SUB_SUBSCRIPTION', 'SUBSCRIPTION_ID') == 2          # short for "subscription", its own word
    assert score('SUB_ID', 'SUB_DEVICE', 'DEVICE_ID') == 0                       # ... and no hint for a table that only starts with SUB_
    # a word both column names hold - unless it is just the parent table's own word
    assert affinity('MSISDN_ACTIVATED', 'SUB_SUBSCRIPTION', 'MSISDN', prefixes) == (1.5, 'both column names hold "msisdn"')
    assert score('CONTACT_MSISDN', 'SUB_SUBSCRIPTION', 'MSISDN') == 1.5 and score('SORT_ORDER', 'SAL_ORDER', 'ORDER_NO') == 0
    assert score('SUBSCRIBER_REF', 'SUB_SUBSCRIPTION', 'SUBSCRIPTION_ID') == 0 and score('REF_NO', 'BIL_INVOICE', 'INVOICE_NO') == 0


# ------------------------------------------------------------ jobs and storage

def test_one_job_per_workspace_across_profiling_and_relations(client, extras, monkeypatch):
    ws = extras
    hold, reached = threading.Event(), threading.Event()
    real = relations_module._Run.check_all

    def held(self, wanted, pairs=()):
        reached.set()
        while not hold.is_set():
            self.job.check()
            time.sleep(0.005)
        return real(self, wanted, pairs)
    monkeypatch.setattr(relations_module._Run, 'check_all', held)
    job = client.post(f'/api/workspaces/{ws}/relations/check', json={}).json()
    assert reached.wait(10) and job['kind'] == 'relations'
    for route, body in ((f'/api/workspaces/{ws}/profile', {'tables': 'all'}), (f'/api/workspaces/{ws}/relations/check', {})):
        busy = client.post(route, json=body)
        assert busy.status_code == 409 and busy.json()['job']['id'] == job['id']
        assert busy.json()['detail'] == 'A relationship check is already running in this workspace.'
    assert client.delete(f'/api/workspaces/{ws}/profile').status_code == 409
    assert client.get(f'/api/workspaces/{ws}/profile').json()['job']['id'] == job['id']       # both tabs see the one job
    cancelled = client.delete(f'/api/jobs/{job["id"]}')
    assert cancelled.status_code == 200 and wait(client, job)['state'] == 'cancelled'
    monkeypatch.setattr(relations_module._Run, 'check_all', real)

    profiling = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'})          # and the other way round
    assert profiling.status_code == 200
    busy = client.post(f'/api/workspaces/{ws}/relations/check', json={})
    if busy.status_code == 409:
        assert busy.json()['detail'] == 'A profiling job is already running in this workspace.'
    wait(client, profiling.json())
    assert check(client, ws)['state'] == 'done'


def test_progress_and_cancel(client, extras, monkeypatch):
    ws = extras
    seen = []
    real = relations_module._Run.check_one

    def slow(self, r, child, copy, columns, read_s):
        seen.append((self.job.done, self.job.total, self.job.current, self.job.phase))
        if len(seen) == 3:
            self.job._cancel.set()
        return real(self, r, child, copy, columns, read_s)
    monkeypatch.setattr(relations_module._Run, 'check_one', slow)
    job = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    assert (job['state'], job['done'], job['total']) == ('cancelled', 2, N)   # the third was under way: not saved
    assert [s[0] for s in seen] == [0, 1, 2] and seen[0][1] == N and seen[0][3] == 'checking' and '→' in seen[0][2]
    view = overview(client, ws)
    assert view['summary']['checked'] == 2 and view['summary']['never'] == N - 2   # what was finished is kept
    assert client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': ['nope']}).status_code == 404
    assert client.get('/api/workspaces/nope/relations/check').status_code == 404


def test_example_keys_of_personal_columns_are_saved_masked(client, extras, tmp_path, app):
    """x_children.msisdn joined to a key it never matches: every key is an orphan, and a phone number."""
    ws = extras
    check(client, ws)
    rel = by_pair(overview(client, ws))[('x_children', ('msisdn',), 'x_pairs')]
    c = rel['check']
    assert (c['coverage'], c['orphan_keys'], c['orphan_columns']) == (0.0, 1000, ['msisdn'])
    assert c['examples_masked'] is True and len(c['orphan_examples']) == 20
    assert all(e['key'][0].startswith('96') and e['key'][0].count('*') == 8 for e in c['orphan_examples'])
    blob = (tmp_path / 'data' / 'explorer.db').read_bytes()
    assert not any(str(row[4]).encode() in blob for row in CHILDREN[:200])            # no phone number of any row
    # a key that is not personal is shown as it is - until the user marks it
    plain = by_pair(overview(client, ws))[('x_children', ('parent_id',), 'x_parents')]['check']
    assert plain['orphan_examples'][0]['key'][0] > 200 and plain['examples_masked'] is False
    client.post(f'/api/workspaces/{ws}/personal', json={'table': 'x_children', 'column': 'parent_id', 'personal': True})
    marked = by_pair(overview(client, ws))[('x_children', ('parent_id',), 'x_parents')]['check']
    assert marked['examples_masked'] is True and all(e['key'][0] == '***' for e in marked['orphan_examples'])
    check(client, ws)                                                   # and a new check masks at the moment it saves
    again = by_pair(overview(client, ws))[('x_children', ('parent_id',), 'x_parents')]['check']
    assert again['examples_masked'] is True and all(e['key'][0] == '***' for e in again['orphan_examples'])


def test_results_follow_the_model(client, extras, app):
    ws = extras
    check(client, ws, suggest=True)
    store = app.state.relations.store
    assert len(store.checks(ws)) == N and len(store.links(ws)) == 1
    cleared = client.delete(f'/api/workspaces/{ws}/profile')            # "Clear saved statistics": example keys are values
    assert cleared.status_code == 200 and store.checks(ws) == {} and store.links(ws) == []
    check(client, ws, suggest=True)
    (s,) = overview(client, ws)['suggestions']
    client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'reject'})
    client.delete(f'/api/workspaces/{ws}/profile')
    assert [x['status'] for x in store.links(ws)] == ['rejected']       # a decision is a setting: it stays
    check(client, ws)
    sources = client.get(f'/api/workspaces/{ws}').json()['sources']
    joins = next(x for x in sources if x['kind'] == 'sql_files')['id']
    client.delete(f'/api/workspaces/{ws}/sources/{joins}')              # the three join relationships go with their source
    assert len(store.checks(ws)) == 3 and overview(client, ws)['summary']['relationships'] == 3
    database = next(x for x in sources if x['kind'] == 'database')['id']
    client.delete(f'/api/workspaces/{ws}/sources/{database}')
    assert store.checks(ws) == {} and overview(client, ws)['relationships'] == []
    client.delete(f'/api/workspaces/{ws}')
    con = sqlite3.connect(app.state.workspaces.db_path)
    assert [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('relation_checks', 'value_links')] == [0, 0]
    con.close()


def test_removing_the_source_cancels_a_running_check(client, extras, app, monkeypatch):
    ws = extras
    reached = threading.Event()

    def held(self, wanted, pairs=()):
        reached.set()
        while True:
            self.job.check()
            time.sleep(0.005)
    monkeypatch.setattr(relations_module._Run, 'check_all', held)
    job = client.post(f'/api/workspaces/{ws}/relations/check', json={}).json()
    assert reached.wait(10)
    database = next(x for x in client.get(f'/api/workspaces/{ws}').json()['sources'] if x['kind'] == 'database')['id']
    assert client.delete(f'/api/workspaces/{ws}/sources/{database}').status_code == 200
    assert client.get(f'/api/jobs/{job["id"]}').json()['state'] == 'cancelled'
    assert app.state.relations.store.checks(ws) == {}


def test_a_password_is_asked_for_before_a_check_starts(client, app, ws):
    store = app.state.workspaces
    settings = {'engine': 'postgresql', 'host': '127.0.0.1', 'port': 9, 'database': 'crm', 'username': 'reader',
                'schema_filter': '', 'include_views': False}
    origin = lambda t: [{'kind': 'db', 'source_id': 'src1', 'label': 'crm', 'detail': 'public', 'schema': 'public', 'table': t}]
    column = lambda n, pk=False: {'name': n, 'type': 'INTEGER', 'origins': ['db'], 'types': {'db': 'INTEGER'}, 'pk': pk}
    snapshot = {'tables': [
        {'name': 'customers', 'schema': 'public', 'origins': origin('customers'), 'columns': [column('customer_id', True)]},
        {'name': 'orders', 'schema': 'public', 'origins': origin('orders'), 'columns': [column('order_id', True), column('customer_id')]}],
        'relationships': []}
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('src1', ws, 'database', 'crm', json.dumps(settings), json.dumps(snapshot), '[]', '2026', '2026'))
    asked = client.post(f'/api/workspaces/{ws}/relations/check', json={})
    assert asked.status_code == 409 and asked.json()['needs_password'] is True and asked.json()['source']['id'] == 'src1'
    job = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'passwords': {'src1': 'S3cret-Pa55'}}).json())
    assert job['state'] == 'done' and 'S3cret-Pa55' not in json.dumps(job)       # nothing listens there: a clean failure
    (rel,) = overview(client, ws)['relationships']
    assert rel['check']['status'] == 'not_checked' and rel['check']['reason_code'] == 'error'
    assert 'S3cret-Pa55' not in json.dumps(rel)
