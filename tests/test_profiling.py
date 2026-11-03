"""Profiling: the queries, sampling, column statistics, background jobs, masking and storage."""

import hashlib
import json
import re
import sqlite3
import time
from datetime import date, timedelta

import pytest
from openpyxl import Workbook
from sqlalchemy.dialects import mssql, mysql, oracle, postgresql, sqlite

from app import config
from app.main import create_app
from app.profiling import queries as q
from app.profiling import service as service_module
from app.profiling.data import DatabaseData, FileData, QueryFailed
from app.profiling.personal import mask
from app.profiling.profiler import Cancelled, TimedOut, budget_check, profile_copy, profile_table, read_table
from app.security import check_read_only_sql
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from app.workspace import SCHEMA_V1, SCHEMA_VERSION, Workspaces
from conftest import local_client

DIALECTS = {'oracle': oracle.dialect(), 'mssql': mssql.dialect(), 'postgresql': postgresql.dialect(),
            'mysql': mysql.dialect(), 'sqlite': sqlite.dialect()}


def sql(statement, dialect):
    return ' '.join(str(statement.compile(dialect=DIALECTS[dialect])).split())


def wait(client, job):
    assert 'id' in job, job
    for _ in range(3000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def profile_all(client, ws, **body):
    started = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all', **body})
    assert started.status_code == 200, started.text
    return wait(client, started.json())


def table_profile(client, ws, table, **params):
    response = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def stats_of(profile):
    return {c['name']: c['stats'] for c in profile['columns']}


# ------------------------------------------------------------------ the data

N = 200
FIRST = ['Ahmed', 'Sara', 'Omar', 'Noura', 'Khalid', 'Maha', 'Faisal', 'Reem']


def people_rows():
    rows = []
    for i in range(1, N + 1):
        rows.append((
            i,                                              # person_id
            f'{FIRST[i % 8]} Al-Test{i:03d}',               # full_name (personal by name)
            f'05{10000000 + i * 37:08d}',                   # contact  (personal by its values: Saudi mobiles)
            None if i % 10 == 0 else f'user{i:03d}@mail.example.com',     # email
            f'{1 + i % 2}{100000000 + i:09d}',              # ref_no   (personal by its values: 10-digit IDs)
            ['Riyadh', 'Jeddah', 'Dammam', 'Abha'][i % 4],  # city
            f'ROWMARKER-{i:04d}',                           # note: a different value in every row
            round(i * 1.5, 2),                              # amount
            i % 7,                                          # score
            (date(2025, 1, 1) + timedelta(days=i)).isoformat(),            # joined: 200 days (by month)
            (date(2015, 1, 1) + timedelta(days=i * 20)).isoformat(),       # born: 11 years (by year)
            b'\x00\x01binary' if i % 2 else None,           # photo (BLOB)
            1,                                              # active: the same in every row
            None,                                           # unused: never filled
            'x' if i % 5 == 0 else None,                    # rare: 80% empty
        ))
    return rows


PEOPLE_DDL = """
CREATE TABLE people (
    person_id INTEGER PRIMARY KEY, full_name VARCHAR(60) NOT NULL, contact VARCHAR(12), email VARCHAR(80),
    ref_no VARCHAR(10), city VARCHAR(20), note VARCHAR(20), amount DECIMAL(10, 2), score INTEGER,
    joined DATE, born DATE, photo BLOB, active INTEGER, unused VARCHAR(10), rare VARCHAR(5));
CREATE TABLE empty_table (id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE log_lines (message TEXT, level TEXT);
INSERT INTO log_lines VALUES ('started', 'info'), ('stopped', 'info');
"""


@pytest.fixture
def people_db(tmp_path):
    path = tmp_path / 'people.db'
    con = sqlite3.connect(path)
    con.executescript(PEOPLE_DDL)
    con.executemany('INSERT INTO people VALUES (' + ', '.join('?' * 15) + ')', people_rows())
    con.commit()
    con.close()
    return path


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Profiling'}).json()['id']


@pytest.fixture
def people(client, ws, people_db):
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(people_db)})
    assert added.status_code == 200, added.text
    return ws


def data_files(tmp_path):
    return [p for p in (tmp_path / 'data').rglob('*') if p.is_file()]


def on_disk(tmp_path, text):
    needle = text.encode()
    return [p.name for p in data_files(tmp_path) if needle in p.read_bytes()]


# ------------------------------------------------- the queries (no database)

def test_sampling_uses_each_engines_own_clause():
    cols = [q.ColumnPlan.of('event_id', 'INTEGER', 'x'), q.ColumnPlan.of('payload', 'BLOB', 'sqlite')]
    for dialect, clause in [
            ('oracle', 'FROM "TELECOM"."USAGE_EVENTS" SAMPLE (5.0) SEED (42)'),            # rows: an even sample
            ('postgresql', 'FROM "TELECOM"."USAGE_EVENTS" TABLESAMPLE SYSTEM (5.0) REPEATABLE (42)'),
            ('mssql', 'FROM [TELECOM].[USAGE_EVENTS] TABLESAMPLE (5.0 PERCENT) REPEATABLE (42)')]:
        text = sql(q.read_query(dialect, 'TELECOM', 'USAGE_EVENTS', cols, percent=5, seed=42, limit=200_000), dialect)
        assert clause in text, text
        check_read_only_sql(text, dialect=dialect)
        assert '5%' in q.sample_text(dialect, 5) and 'random sample of' in q.sample_text(dialect, 5)
    assert q.sample_text('oracle', 5) == 'SAMPLE (5%): a random sample of rows'
    # Oracle above the size limit: whole blocks - only 5% of the table is read - and it is said what that is
    blocks = sql(q.read_query('oracle', 'TELECOM', 'USAGE_EVENTS', cols, percent=5, seed=42, limit=200_000, block=True), 'oracle')
    assert 'FROM "TELECOM"."USAGE_EVENTS" SAMPLE BLOCK (5.0) SEED (42)' in blocks
    check_read_only_sql(blocks, dialect='oracle')
    assert q.sample_text('oracle', 5, block=True) == ('SAMPLE BLOCK (5%): block sample: a few stretches of the table, may not '
                                                      'represent all of it')
    for dialect in ('postgresql', 'mssql'):                             # "block" is Oracle's alone
        assert 'BLOCK' not in sql(q.read_query(dialect, 'S', 'T', cols, percent=5, seed=1, block=True), dialect)
        assert q.sample_text(dialect, 5, block=True) == q.sample_text(dialect, 5)
    for dialect, clause in [('mysql', 'FROM `telecom`.`usage_events` LIMIT %s'),
                            ('sqlite', 'FROM "telecom"."usage_events" LIMIT ? OFFSET ?')]:
        # no sampling clause on these engines: asking for a percent still gives the first rows
        text = sql(q.read_query(dialect, 'telecom', 'usage_events', cols, percent=5, seed=42, limit=100_000), dialect)
        assert clause in text and 'SAMPLE' not in text
    assert q.sample_text('mysql', limit=100_000) == 'first 100,000 rows, not random'
    whole = sql(q.read_query('oracle', 'TELECOM', 'USAGE_EVENTS', cols), 'oracle')
    assert 'SAMPLE' not in whole and 'ROWNUM' not in whole and 'FETCH' not in whole
    # a large or binary column is never fetched: only whether it holds a value
    assert '"event_id" AS c0' in whole and 'CASE WHEN ("payload" IS NOT NULL) THEN 1 ELSE NULL END AS c1' in whole
    tiny = sql(q.read_query('oracle', 'S', 'T', cols, percent=0.000000001, seed=1), 'oracle')
    assert f'SAMPLE ({q.MIN_PERCENT})' in tiny                          # never 0, never 1e-9
    counted = sql(q.bounded_count_query('postgresql', 'S', 'T', 100_001), 'postgresql')
    assert counted.startswith('SELECT count(*) AS n FROM (SELECT 1 AS x FROM "S"."T" LIMIT')


NASTY = ['plain', 'Mixed Case', 'select', 'with space', 'semi;colon', 'quote"inside', "apostrophe's",
         'x"; DROP TABLE customers; --', 'bracket]name', 'back`tick', 'percent%sign', 'DELETE']


@pytest.mark.parametrize('dialect', list(DIALECTS))
def test_identifiers_are_always_quoted_and_values_always_bound(dialect):
    columns = [q.ColumnPlan.of(name, 'VARCHAR(10)', dialect) for name in NASTY]
    for table in NASTY:
        source = q.table_source(dialect, 'Odd Schema', table)
        statements = [q.count_query(source), q.stats_query(source, columns)[0],
                      q.rows_query(dialect, 'Odd Schema', table, NASTY, 20), q.minmax_query(source, NASTY[:3]),
                      q.read_query(dialect, 'Odd Schema', table, columns, limit=500),
                      q.read_query(dialect, 'Odd Schema', table, columns, percent=2.5, seed=7, limit=500),
                      q.bounded_count_query(dialect, 'Odd Schema', table, 500)]
        for name in NASTY:
            statements += [q.top_values_query(source, name), q.number_histogram_query(source, name, 1.5, 2.5),
                           q.date_histogram_query(source, name, True), q.date_histogram_query(source, name, False)]
        for statement in statements:
            text = sql(statement, dialect)
            check_read_only_sql(text, dialect=dialect)       # one statement, and a read
            assert ';' not in text.replace('semi;colon', '').replace('; DROP TABLE customers; --', '')
            if 'SAMPLE' not in text:                         # (a sampling percent is part of the clause)
                assert '1.5' not in text and '2.5' not in text   # numbers travel as parameters, not as text
    opener, closer = {'mssql': '[]', 'mysql': '``'}.get(dialect, '""')
    text = sql(q.top_values_query(q.table_source(dialect, None, 'T'), 'x"; DROP TABLE customers; --'), dialect)
    doubled = 'x""; DROP TABLE customers; --' if opener == '"' else 'x"; DROP TABLE customers; --'
    assert f'{opener}{doubled}{closer}' in text              # the whole name stays inside its quotes
    assert f'{opener}T{closer}' in text


def test_type_families():
    cases = {
        ('NUMBER(10, 2)', 'oracle'): 'number', ('VARCHAR(120 CHAR)', 'oracle'): 'text', ('CLOB', 'oracle'): 'lob',
        ('BLOB', 'oracle'): 'lob', ('RAW(16)', 'oracle'): 'lob', ('LONG', 'oracle'): 'lob', ('DATE', 'oracle'): 'date',
        ('TIMESTAMP WITH TIME ZONE', 'oracle'): 'date', ('BINARY_DOUBLE', 'oracle'): 'number',
        ('INTERVAL DAY TO SECOND', 'oracle'): 'other', ('XMLTYPE', 'oracle'): 'lob',
        ('DATETIME2', 'mssql'): 'date', ('BIT', 'mssql'): 'boolean', ('NVARCHAR(50)', 'mssql'): 'text',
        ('TEXT', 'mssql'): 'lob', ('XML', 'mssql'): 'lob', ('VARBINARY(100)', 'mssql'): 'lob',
        ('VARCHAR(max)', 'mssql'): 'lob', ('NVARCHAR(MAX)', 'mssql'): 'lob', ('VARCHAR(8000)', 'mssql'): 'text',
        ('TIMESTAMP', 'mssql'): 'lob', ('UNIQUEIDENTIFIER', 'mssql'): 'other', ('MONEY', 'mssql'): 'number',
        ('TEXT', 'postgresql'): 'text', ('BYTEA', 'postgresql'): 'lob', ('JSONB', 'postgresql'): 'lob',
        ('BOOLEAN', 'postgresql'): 'boolean', ('UUID', 'postgresql'): 'other', ('INTEGER[]', 'postgresql'): 'lob',
        ('TIMESTAMP WITHOUT TIME ZONE', 'postgresql'): 'date', ('TIME WITHOUT TIME ZONE', 'postgresql'): 'other',
        ('DOUBLE PRECISION', 'postgresql'): 'number', ('CHARACTER VARYING(5)', 'postgresql'): 'text',
        ('LONGTEXT', 'mysql'): 'lob', ('TEXT', 'mysql'): 'lob', ('MEDIUMBLOB', 'mysql'): 'lob',
        ('DATETIME', 'mysql'): 'date', ('TINYINT', 'mysql'): 'number', ('ENUM', 'mysql'): 'other',
        ('TEXT', 'sqlite'): 'text', ('BLOB', 'sqlite'): 'lob', ('INTEGER', 'sqlite'): 'number', ('', 'sqlite'): 'other',
        ('VARCHAR', 'duckdb'): 'text', ('BIGINT', 'duckdb'): 'number', ('BOOLEAN', 'duckdb'): 'boolean',
        ('TIMESTAMP', 'duckdb'): 'date', ('TIME', 'duckdb'): 'other', (None, 'duckdb'): 'other',
    }
    assert {k: q.family(*k) for k in cases} == cases
    assert all(q.is_whole(t) for t in ('INTEGER', 'BIGINT', 'NUMBER(10, 0)', 'NUMBER(10)', 'SMALLINT', 'NUMERIC(8,0)'))
    assert not any(q.is_whole(t) for t in ('NUMBER(10, 2)', 'DOUBLE', 'DECIMAL(8, 2)', 'VARCHAR(10)', None))


def test_large_and_binary_columns_only_get_a_null_count():
    columns = [q.ColumnPlan.of('doc', 'CLOB', 'oracle'), q.ColumnPlan.of('n', 'NUMBER(5)', 'oracle')]
    statement, fields = q.stats_query(q.table_source('oracle', 'S', 'T'), columns)
    assert fields == [('doc', 'nulls'), ('n', 'nulls'), ('n', 'distinct'), ('n', 'min'), ('n', 'max'), ('n', 'avg')]
    text = sql(statement, 'oracle')
    assert 'sum(CASE WHEN ("doc" IS NULL)' in text and 'count(DISTINCT "doc")' not in text and 'min("doc")' not in text


def test_row_estimates_come_from_each_engines_catalog():
    expected = {'oracle': 'FROM all_tables WHERE all_tables.owner =', 'postgresql': 'pg_catalog.pg_class.reltuples',
                'mssql': 'sys.partitions', 'mysql': 'information_schema.`TABLES`'}
    partitioned = sql(q.estimate_query('postgresql', 'TELECOM'), 'postgresql')
    assert partitioned.startswith('WITH RECURSIVE parts') and 'pg_catalog.pg_inherits' in partitioned    # sum of the partitions
    for dialect, fragment in expected.items():
        text = sql(q.estimate_query(dialect, 'TELECOM'), dialect)
        assert fragment in text and 'TELECOM' not in text, text       # the schema is a bound value
        check_read_only_sql(text, dialect=dialect)
    assert 'num_rows' in sql(q.estimate_query('oracle', 'X'), 'oracle')
    assert '`TABLE_ROWS`' in sql(q.estimate_query('mysql', 'x'), 'mysql')
    assert 'FROM otherdb.sys.tables' in sql(q.estimate_query('mssql', 'otherdb.dbo'), 'mssql')
    assert 'FROM [Sales DB].sys.tables' in sql(q.estimate_query('mssql', 'Sales DB.dbo'), 'mssql')
    assert q.estimate_query('sqlite', None) is None                   # no statistics there: counted exactly


# ------------------------------------------------------ statistics (SQLite)

@pytest.fixture
def people_profile(client, people):
    job = profile_all(client, people)
    assert (job['state'], job['tables_done'], job['tables_total'], job['errors']) == ('done', 3, 3, [])
    return table_profile(client, people, 'people')


def test_row_counts_and_what_was_profiled(client, people, people_profile):
    p = people_profile
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled']) == (N, 'exact', N)
    assert (p['sampled'], p['sample_method'], p['profiled']) == (False, None, True)
    assert p['profiled_at'] and p['source']['kind'] == 'database' and p['can_profile'] and p['notes'] == []
    overview = client.get(f'/api/workspaces/{people}/profile').json()
    assert {t['id']: (t['row_count'], t['row_count_kind']) for t in overview['tables']} == {
        'people': (N, 'exact'), 'empty_table': (0, 'exact'), 'log_lines': (2, 'exact')}
    model = client.get(f'/api/workspaces/{people}/model').json()
    assert {t['id']: (t['row_count'], t['row_count_kind'], t['can_profile']) for t in model['tables']} == {
        'people': (N, 'exact', True), 'empty_table': (0, 'exact', True), 'log_lines': (2, 'exact', True)}
    details = client.get(f'/api/workspaces/{people}/tables/people').json()
    assert details['row_count'] == N and details['profile']['rows_profiled'] == N


def test_column_statistics(people_profile):
    st = stats_of(people_profile)
    key = st['person_id']
    assert (key['nulls'], key['distinct'], key['min'], key['max'], key['avg'], key['rows']) == (0, N, 1, N, 100.5, N)
    assert key['unique'] is True and 'top' not in key                       # all different: no top values
    city = st['city']
    assert (city['nulls'], city['distinct'], city['min'], city['max']) == (0, 4, 'Abha', 'Riyadh')
    assert (city['min_length'], city['max_length']) == (4, 6) and 'avg' not in city
    assert city['top'] == [['Abha', 50], ['Dammam', 50], ['Jeddah', 50], ['Riyadh', 50]]
    amount = st['amount']
    assert (amount['min'], amount['max'], amount['avg'], amount['distinct']) == (1.5, 300, 150.75, N)
    score = st['score']
    assert score['distinct'] == 7 and [n for _, n in score['top']] == sorted((n for _, n in score['top']), reverse=True)
    assert len(score['top']) == 7 and sum(n for _, n in score['top']) == N
    email = st['email']
    assert (email['nulls'], email['distinct']) == (20, 180)                 # null count; distinct ignores them
    rare = st['rare']
    assert (rare['nulls'], rare['distinct'], rare['top']) == (160, 1, [['x', 40]])
    assert (st['unused']['nulls'], st['unused']['distinct']) == (N, 0) and 'top' not in st['unused']
    assert st['joined']['min'] == '2025-01-02' and st['joined']['max'] == '2025-07-20'


def test_top_values_are_at_most_ten(people_profile):
    note, born = stats_of(people_profile)['note'], stats_of(people_profile)['born']
    assert note['unique'] is True
    assert born['distinct'] == N


def test_histograms(people_profile):
    st = stats_of(people_profile)
    amount = st['amount']['histogram']
    assert amount['kind'] == 'number' and len(amount['bins']) == 12 and not amount['whole']
    assert sum(b['count'] for b in amount['bins']) == N
    assert amount['bins'][0]['from'] == 1.5 and amount['bins'][-1]['to'] == pytest.approx(300)
    score = st['score']['histogram']                     # whole numbers 0..6: one bar each
    assert score['whole'] and [(b['from'], b['to']) for b in score['bins']] == [(i, i) for i in range(7)]
    assert sum(b['count'] for b in score['bins']) == N
    key = st['person_id']['histogram']
    assert len(key['bins']) == 12 and key['bins'][0] == {'from': 1, 'to': 17, 'count': 17}
    joined = st['joined']['histogram']                   # 200 days: by month
    assert joined['kind'] == 'month' and [b['label'] for b in joined['bins']][:3] == ['2025-01', '2025-02', '2025-03']
    assert len(joined['bins']) == 7 and sum(b['count'] for b in joined['bins']) == N
    born = st['born']['histogram']                       # over three years: by year
    assert born['kind'] == 'year' and born['bins'][0]['label'] == '2015' and born['bins'][-1]['label'] == '2025'
    assert sum(b['count'] for b in born['bins']) == N
    assert 'histogram' not in st['city'] and 'histogram' not in st['active'] or st['active'].get('histogram') is None


def test_binary_columns_are_skipped_for_values_but_their_nulls_are_counted(people_profile):
    photo = stats_of(people_profile)['photo']
    assert (photo['family'], photo['nulls'], photo['rows']) == ('lob', 100, N)
    assert not {'distinct', 'min', 'max', 'top', 'histogram'} & set(photo)
    assert 'photo' not in people_profile['sample']['columns'] and people_profile['sample']['left_out'] == ['photo']


def test_sample_rows_are_twenty_and_masked(people_profile):
    sample = people_profile['sample']
    assert len(sample['rows']) == config.PROFILE_SAMPLE_ROWS == 20 and sample['masked'] is True
    assert sample['personal'] == ['full_name', 'contact', 'email', 'ref_no']
    first = dict(zip(sample['columns'], sample['rows'][0]))
    assert first['person_id'] == 1 and first['city'] == 'Jeddah' and first['note'] == 'ROWMARKER-0001'
    assert first['full_name'] == mask('Sara Al-Test001') == 'Sa***********01' and first['contact'] == '05******37'
    assert first['email'] == 'u***@m***.com' and first['ref_no'] == '21******01'


def test_one_table_costs_the_database_one_read(people_db):
    """A2: one SELECT of the columns that can be profiled; every statistic comes from the copy."""
    before = hashlib.sha256(people_db.read_bytes()).hexdigest()
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db)))
    model, _ = db.read_schema()
    sent_before = len(db.statements)
    data = DatabaseData(db, None, 'people')
    columns = [q.ColumnPlan.of(c.name, c.type, 'sqlite') for c in model.find('people').columns]
    result = profile_table(data, columns)
    data.close()
    sent = db.statements[sent_before:]
    reads = [s for s in sent if s.startswith('SELECT')]
    assert len(reads) == 1 == result['table']['queries'] and result['table']['local_queries'] >= 10
    (read,) = reads
    assert read.startswith('SELECT "person_id" AS c0, "full_name" AS c1') and read.endswith('FROM "people"\n LIMIT ? OFFSET ?')
    assert 'count(' not in read.lower() and 'GROUP BY' not in read and 'DISTINCT' not in read
    assert 'CASE WHEN ("photo" IS NOT NULL) THEN 1 ELSE NULL END' in read            # the BLOB itself is not fetched
    assert db.blocked == [] and all(s.startswith(('SELECT', 'PRAGMA query_only')) for s in sent)
    for statement in sent:
        check_read_only_sql(statement, dialect='sqlite')
    assert hashlib.sha256(people_db.read_bytes()).hexdigest() == before
    assert result['columns']['city']['top'][0] == ['Abha', 50] and result['columns']['photo']['nulls'] == 100
    db.dispose()


# ------------------------------------------------------------------ sampling

def test_big_tables_are_profiled_on_a_sample_and_say_so(client, people, monkeypatch):
    """SQLite has no row estimates and no sampling clause. Since the dry-run round it is counted exactly (one
    COUNT(*) a table), and a table above the sample size is read on a sample spread over its row numbers."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 50)
    job = profile_all(client, people)
    assert job['errors'] == []
    p = table_profile(client, people, 'people')
    assert (p['sampled'], p['sample_random']) == (True, False)
    assert p['sample_method'] == '25 stretches of 2 rows spread over the table, not random'
    assert (p['rows_profiled'], p['row_count'], p['row_count_kind']) == (50, N, 'exact')
    assert p['notes'] == []                                             # nothing about "how many is not known"
    st = stats_of(p)
    # rows 1-2, 9-10, 17-18 ... 193-194 of the 200: the whole table, not its first 50 rows
    assert (st['person_id']['min'], st['person_id']['max'], st['person_id']['rows'], st['email']['nulls']) == (1, 194, 50, 5)
    assert st['joined']['max'] == (date(2025, 1, 1) + timedelta(days=194)).isoformat() and len(st['joined']['histogram']['bins']) == 7
    small = table_profile(client, people, 'log_lines')                # up to the limit: the whole table, counted exactly
    assert (small['sampled'], small['rows_profiled'], small['sample_method']) == (False, 2, None)
    assert (small['row_count'], small['row_count_kind']) == (2, 'exact')
    counted = client.post(f'/api/workspaces/{people}/tables/people/count').json()       # the COUNT(*) on request still works
    assert (counted['row_count'], counted['row_count_kind']) == (N, 'exact')


class FakeTable:
    """A database table that hands out canned rows and records what it was asked."""

    def __init__(self, dialect, rows, is_view=False):
        self.dialect, self.schema, self.table, self.is_view = dialect, 'sales', 'orders', is_view
        self.rows, self.asked, self.queries, self.closed = rows, [], 0, 0

    def read(self, statement, chunk_rows=None):
        text = ' '.join(str(statement.compile(dialect=DIALECTS[self.dialect], compile_kwargs={'literal_binds': True})).split())
        self.asked.append(text)
        self.queries += 1
        rows = self.rows(text, len(self.asked)) if callable(self.rows) else self.rows
        limit = re.search(r'(?:LIMIT|FETCH FIRST|TOP|ROWNUM <=) (\d+)', text)
        rows = rows[:int(limit.group(1))] if limit else rows
        for start in range(0, len(rows), chunk_rows or 1000):
            yield rows[start:start + (chunk_rows or 1000)]

    def close(self):
        self.closed += 1


ORDER_PLANS = [q.ColumnPlan('order_id', 'INTEGER', 'number'), q.ColumnPlan('status', 'VARCHAR(10)', 'text')]


def orders(n):
    return [(i, 'open' if i % 3 else 'done') for i in range(1, n + 1)]


def test_views_are_sampled_on_their_first_rows():
    # SAMPLE / TABLESAMPLE cannot be applied to a view on most engines.
    table = FakeTable('postgresql', orders(400))
    copy = read_table(table, ORDER_PLANS, estimate=5_000_000, settings={'sample_rows': 100})
    assert 'TABLESAMPLE SYSTEM (0.002)' in table.asked[0] and copy.plan['kind'] == 'percent'
    assert (copy.sampled, copy.random, copy.row_count, copy.row_count_kind) == (True, True, 5_000_000, 'estimate')
    copy.close()
    view = FakeTable('postgresql', orders(400), is_view=True)
    copy = read_table(view, ORDER_PLANS, estimate=5_000_000, settings={'sample_rows': 100})
    assert 'TABLESAMPLE' not in view.asked[0] and 'LIMIT 101' in view.asked[0] and len(view.asked) == 1
    assert (copy.sampled, copy.random, copy.method, copy.loaded_rows) == (True, False, 'first 100 rows, not random', 100)
    assert (copy.row_count, copy.row_count_kind) == (5_000_000, 'estimate')
    copy.close()


def test_small_tables_are_read_whole_and_big_ones_never_past_the_cap():
    """A2: up to SAMPLE_TARGET_ROWS rows the table is read whole; a sample stops at the cap."""
    small = FakeTable('oracle', orders(80))
    copy = read_table(small, ORDER_PLANS, estimate=80, settings={'sample_rows': 100})
    assert 'SAMPLE' not in small.asked[0] and len(small.asked) == 1
    assert (copy.sampled, copy.loaded_rows, copy.row_count, copy.row_count_kind) == (False, 80, 80, 'exact')
    assert copy.plan == {'kind': 'table', 'percent': None, 'limit': 100, 'seed': None}
    copy.close()
    # the estimate said 1,000 rows; the sample comes back far bigger: it is cut at twice the target
    stale = FakeTable('mssql', orders(900))
    copy = read_table(stale, ORDER_PLANS, estimate=1000, settings={'sample_rows': 100})
    assert 'TABLESAMPLE (10.0 PERCENT)' in stale.asked[0] and 'TOP 201' in stale.asked[0]
    assert copy.loaded_rows == 200 and 'cut at 200 rows' in copy.notes[0] and len(stale.asked) == 1
    copy.close()


def test_oracle_retries_a_block_sample_once_when_it_comes_back_nearly_empty():
    """A2: SAMPLE BLOCK picks whole blocks; too few rows -> ONE more read with a higher percent.
    (Since the fix round only a table above the size limit is sampled by blocks.)"""
    big = 2 * 1024 ** 3
    answers = {1: orders(3), 2: orders(95)}
    table = FakeTable('oracle', lambda text, n: answers[n])
    copy = read_table(table, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=big)
    assert len(table.asked) == 2 and table.queries == 2 == copy.statements
    assert 'SAMPLE BLOCK (1.0) SEED (' in table.asked[0] and 'SAMPLE BLOCK (20.0) SEED (' in table.asked[1]
    assert copy.loaded_rows == 95 and copy.plan['percent'] == 20.0 and copy.method.startswith('SAMPLE BLOCK (20%)')
    assert 'gave only 3 rows; read once more with 20%' in copy.notes[0]
    copy.close()
    still_empty = FakeTable('oracle', [])                       # nothing the second time either: no third read
    copy = read_table(still_empty, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=big)
    assert len(still_empty.asked) == 2 and copy.loaded_rows == 0
    copy.close()
    enough = FakeTable('oracle', orders(60))                    # over a quarter of the target: no retry
    read_table(enough, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=big).close()
    assert len(enough.asked) == 1
    other = FakeTable('postgresql', orders(3))                  # the retry is Oracle's alone
    read_table(other, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=big).close()
    assert len(other.asked) == 1
    rows = FakeTable('oracle', orders(3))                       # ... and a block sample's alone: a row sample is even
    read_table(rows, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}).close()
    assert len(rows.asked) == 1 and 'SAMPLE (1.0) SEED (' in rows.asked[0]


def test_oracle_samples_rows_up_to_a_size_limit_and_blocks_above_it(monkeypatch):
    """The fix round, part C. SAMPLE BLOCK on the 2,000,000-row test table gave 0 to 204,672 rows from two
    to five stretches of the table. Up to DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB (1 GB) rows are sampled:
    SAMPLE (p) reads every block once and gives an even sample. Above it blocks - and it says so."""
    from app.profiling.profiler import Estimates, catalog_estimates, table_bytes
    gb = 1024 ** 3
    assert config.ORACLE_ROW_SAMPLE_MAX_BYTES == gb and 'DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB' in (
        config.BASE_DIR / 'app' / 'config.py').read_text(encoding='utf-8')
    label = 'block sample: a few stretches of the table, may not represent all of it'
    assert q.BLOCK_SAMPLE == label

    def sample(size, **settings):
        table = FakeTable('oracle', orders(60))
        copy = read_table(table, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100, **settings}, size=size)
        try:
            return table.asked, copy.method, copy.plan, copy.notes, copy.sampled
        finally:
            copy.close()

    for size in (None, 1, 115 * 1024 ** 2, gb):                 # not known, small, the test table, exactly the limit
        asked, method, plan, notes, sampled = sample(size)
        assert len(asked) == 1 and ' SAMPLE (1.0) SEED (' in asked[0] and 'BLOCK' not in asked[0], size
        assert method == 'SAMPLE (1%): a random sample of rows' and plan['block'] is False and sampled and notes == []
    asked, method, plan, notes, sampled = sample(gb + 1)
    assert len(asked) == 1 and ' SAMPLE BLOCK (1.0) SEED (' in asked[0]
    assert method == f'SAMPLE BLOCK (1%): {label}' and plan['block'] is True and sampled
    assert notes == [f'This table is about 1.0 GB, more than the 1.0 GB up to which rows are sampled '
                     f'(DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB), so whole blocks were sampled: {label}.']
    assert 'about 200 MB, more than the 100 MB up to which' in sample(200 * 1024 ** 2, row_sample_max_bytes=100 * 1024 ** 2)[3][0]
    # the limit is a setting
    assert 'SAMPLE BLOCK' in sample(200 * 1024 ** 2, row_sample_max_bytes=100 * 1024 ** 2)[0][0]
    assert 'SAMPLE BLOCK' not in sample(5 * gb, row_sample_max_bytes=10 * gb)[0][0]
    monkeypatch.setattr(config, 'ORACLE_ROW_SAMPLE_MAX_BYTES', 50)
    table = FakeTable('oracle', orders(60))
    read_table(table, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=51).close()
    assert 'SAMPLE BLOCK' in table.asked[0]
    # other engines have one way to sample: the size changes nothing
    for dialect in ('postgresql', 'mssql'):
        table = FakeTable(dialect, orders(60))
        copy = read_table(table, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100}, size=5 * gb)
        assert 'BLOCK' not in table.asked[0] and 'block' not in copy.plan and 'a random sample of' in copy.method
        copy.close()
    # a later read of the same sample ("show personal values") takes it the way it was taken
    for plan, clause in [({'kind': 'percent', 'percent': 5.0, 'seed': 7, 'limit': 200, 'block': False}, ' SAMPLE (5.0) SEED (7)'),
                         ({'kind': 'percent', 'percent': 5.0, 'seed': 7, 'limit': 200, 'block': True}, ' SAMPLE BLOCK (5.0) SEED (7)'),
                         # saved before the fix round: no 'block' - those samples were block samples
                         ({'kind': 'percent', 'percent': 5.0, 'seed': 7, 'limit': 200}, ' SAMPLE BLOCK (5.0) SEED (7)')]:
        table = FakeTable('oracle', orders(60))
        copy = read_table(table, ORDER_PLANS, estimate=10_000, plan=plan)
        assert clause in table.asked[0] and (label in copy.method) == ('BLOCK' in clause)
        copy.close()
    # the size: the table's blocks, or its rows x their average length - the larger, None when neither is known
    assert table_bytes(2_000_000, 14_078, 49) == 14_078 * 8192 and table_bytes(2_000_000, 100, 49) == 98_000_000
    assert table_bytes(1000, None, 100) == 100_000 and table_bytes(None, 50, None) == 50 * 8192
    assert table_bytes(None, None, None) is None and table_bytes(0, 0, 0) is None and table_bytes(-1, None, 12) is None
    statement = sql(q.estimate_query('oracle', 'TELECOM'), 'oracle')
    assert 'blocks' in statement and 'avg_row_len' in statement and statement.count('SELECT') == 1      # still one read

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            return self

        def fetchall(self):
            return [('BIG', 300_000_000, 4_000_000, 100), ('SMALL', 5000, 60, 80), ('NEVER_ANALYSED', None, None, None),
                    ('EMPTY_STATS', 0, 0, 0)]

    class Database:
        class profile:
            engine = 'oracle'

        def connect(self):
            return Connection()

    found = catalog_estimates(Database(), 'TELECOM')
    assert isinstance(found, Estimates) and found == {'BIG': 300_000_000, 'SMALL': 5000}
    assert found.sizes == {'BIG': 4_000_000 * 8192, 'SMALL': 60 * 8192}         # 30.5 GB: by blocks; no size where no statistics


def test_oracle_samples_blocks_whatever_the_size_when_asked():
    """Round 4: the first look of "Find links by values" asks for whole blocks (block_sample): SAMPLE (p) would
    read every block of a table up to 1 GB for a look at 2,000 rows. One read, even when it comes back nearly
    empty: the caller reads again its own way. Other engines sample as before."""
    small = 115 * 1024 ** 2
    for size in (None, 1, small):
        table = FakeTable('oracle', orders(2))
        copy = read_table(table, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100, 'block_sample': True},
                          size=size)
        assert len(table.asked) == 1 and ' SAMPLE BLOCK (1.0) SEED (' in table.asked[0], size
        assert table.asked[0].startswith('SELECT "order_id" AS c0, "status" AS c1 FROM "sales"."orders" SAMPLE BLOCK')
        assert copy.plan['block'] is True and copy.method.startswith('SAMPLE BLOCK (1%)') and copy.loaded_rows == 2
        assert copy.notes == [f'Whole blocks were sampled, as asked: {q.BLOCK_SAMPLE}.']
        copy.close()
    rows = FakeTable('oracle', orders(60))                      # not asked: rows, as before
    read_table(rows, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100, 'block_sample': False}, size=small).close()
    assert ' SAMPLE (1.0) SEED (' in rows.asked[0]
    whole = FakeTable('oracle', orders(80))                     # a table smaller than the sample: read whole, no clause
    read_table(whole, ORDER_PLANS, estimate=80, settings={'sample_rows': 100, 'block_sample': True}).close()
    assert 'SAMPLE' not in whole.asked[0]
    other = FakeTable('postgresql', orders(60))
    read_table(other, ORDER_PLANS, estimate=10_000, settings={'sample_rows': 100, 'block_sample': True}).close()
    assert 'TABLESAMPLE SYSTEM (1.0)' in other.asked[0] and 'BLOCK' not in other.asked[0]


@pytest.mark.parametrize('estimate', [None, 0, -1, 0.0, float('nan')])
def test_an_unknown_estimate_means_a_bounded_read_never_a_count(estimate):
    """A3: 0, -1 and NULL say nothing. Read target + 1 rows; fewer back = the exact count."""
    small = FakeTable('postgresql', orders(40))
    copy = read_table(small, ORDER_PLANS, estimate=estimate, settings={'sample_rows': 100})
    assert small.asked == ['SELECT "order_id" AS c0, "status" AS c1 FROM "sales"."orders" LIMIT 101']
    assert (copy.row_count, copy.row_count_kind, copy.sampled, copy.loaded_rows) == (40, 'exact', False, 40)
    copy.close()
    big = FakeTable('postgresql', orders(5000))
    copy = read_table(big, ORDER_PLANS, estimate=estimate, settings={'sample_rows': 100})
    assert len(big.asked) == 1 and 'count' not in big.asked[0].lower()
    assert (copy.row_count, copy.row_count_kind, copy.sampled, copy.loaded_rows) == (100, 'more_than', True, 100)
    # what to say while only "more than" is known is kept apart from the notes: it goes once the table is counted
    assert copy.method == 'first 100 rows, not random' and copy.notes == []
    assert copy.count_note == ('The table has more than 100 rows; how many is not known (the database has no row estimate '
                               'for it). "Exact count" counts them.')
    copy.close()


def test_a_stale_estimate_cannot_make_the_app_read_a_whole_big_table():
    table = FakeTable('mysql', orders(5000))
    copy = read_table(table, ORDER_PLANS, estimate=60, settings={'sample_rows': 100})      # "60 rows", really 5,000
    assert table.asked == ['SELECT `order_id` AS c0, `status` AS c1 FROM `sales`.`orders` LIMIT 101']
    assert (copy.row_count, copy.row_count_kind, copy.loaded_rows) == (100, 'more_than', 100)
    assert 'the estimate of 60 rows is out of date' in copy.count_note and copy.notes == []
    copy.close()


class SlowTable(DatabaseData):
    """A table whose one read does not come back in time."""

    def read(self, statement, chunk_rows=None):
        self.queries += 1
        raise QueryFailed('timed out after 30 s', timeout=True)
        yield


def test_a_table_that_times_out_is_not_retried(people_db):
    """A2: no fallback read, no column-by-column retry: one statement, then the table is given up."""
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db)))
    model, _ = db.read_schema()
    columns = [q.ColumnPlan.of(c.name, c.type, 'sqlite') for c in model.find('people').columns]
    data = SlowTable(db, None, 'people')
    with pytest.raises(QueryFailed) as failure:
        profile_table(data, columns, estimate=150)
    assert failure.value.timeout and data.queries == 1
    data.close()
    db.dispose()


def test_a_table_gets_a_time_budget_and_the_job_moves_on(client, people, monkeypatch):
    """A2: when a table's budget is used up it is marked "timed out"; the other tables are profiled."""
    real = budget_check
    seen = []

    def budget(seconds, also=None, clock=time.monotonic):
        seen.append(seconds)
        if len(seen) == 1:                                   # the first table of the job has no time at all
            return real(-1, also, clock)
        return real(seconds, also, clock)
    monkeypatch.setattr(service_module, 'budget_check', budget)
    job = profile_all(client, people)
    assert seen == [config.TABLE_BUDGET_S] * 3 == [120] * 3
    assert (job['state'], job['tables_done'], job['tables_total']) == ('done', 3, 3)
    (error,) = job['errors']
    first = error['table']
    assert error == {'table': first, 'message': 'timed out after -1 s', 'timeout': True}
    overview = {t['id']: t for t in client.get(f'/api/workspaces/{people}/profile').json()['tables']}
    assert overview[first]['status'] == 'timed_out' and overview[first]['profiled_at'] is None
    assert sorted(t['status'] for t in overview.values()) == ['profiled', 'profiled', 'timed_out']
    p = table_profile(client, people, first, sample=False)
    assert (p['profiled'], p['status'], p['status_message']) == (False, 'timed_out', 'timed out after -1 s')
    monkeypatch.setattr(service_module, 'budget_check', real)
    assert profile_all(client, people)['errors'] == []       # the next run clears the note
    assert table_profile(client, people, first, sample=False)['status'] == 'profiled'


def test_the_budget_check_raises_when_the_time_is_used_up():
    now = [100.0]
    check = budget_check(120, clock=lambda: now[0])
    check()
    now[0] = 219.0
    check()
    now[0] = 221.0
    with pytest.raises(TimedOut, match='timed out after 120 s'):
        check()
    cancelled = budget_check(120, also=lambda: (_ for _ in ()).throw(Cancelled()))
    with pytest.raises(Cancelled):                           # cancelling is asked first
        cancelled()


def test_one_failing_column_does_not_hide_the_others(people_db):
    """The per-column fallback still exists - on the in-memory copy, never against the database."""
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db)))
    columns = [q.ColumnPlan.of(c.name, c.type, 'sqlite') for c in db.read_schema()[0].find('people').columns]
    data = DatabaseData(db, None, 'people')
    sent = len(db.statements)
    copy = read_table(data, columns)
    data.close()
    real = copy.run

    def run(statement):
        text = str(statement.compile(dialect=DIALECTS['sqlite']))
        if '"amount"' in text and 'count(DISTINCT' in text:
            raise QueryFailed('no such operator for this type')
        return real(statement)
    copy.run = run
    found = profile_copy(copy)
    copy.close()
    assert found['amount']['error'] == 'no such operator for this type'
    assert found['city']['distinct'] == 4 and found['score']['distinct'] == 7 and found['city']['rows'] == N
    assert len([s for s in db.statements[sent:] if s.startswith('SELECT')]) == 1       # the database saw one read
    db.dispose()


def test_cancel_is_checked_before_every_step(people_db):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db)))
    columns = [q.ColumnPlan.of(c.name, c.type, 'sqlite') for c in db.read_schema()[0].find('people').columns]
    data = DatabaseData(db, None, 'people')
    calls = []

    def check():
        calls.append(data.queries)
        if len(calls) > 5:
            raise Cancelled()
    with pytest.raises(Cancelled):
        profile_table(data, columns, check=check)
    # asked before the read, before the chunk of rows, and before each statement on the copy
    assert calls == [0, 1, 1, 1, 1, 1] and data.queries == 1
    data.close()
    never = DatabaseData(db, None, 'people')
    with pytest.raises(Cancelled):                           # cancelled before anything was sent
        profile_table(never, columns, check=lambda: (_ for _ in ()).throw(Cancelled()))
    assert never.queries == 0
    never.close()
    db.dispose()


# ---------------------------------------------------------------------- jobs

def test_job_progress_and_result(client, people):
    started = client.post(f'/api/workspaces/{people}/profile', json={'tables': ['people', 'log_lines']})
    assert started.status_code == 200
    job = started.json()
    assert (job['state'], job['tables_total'], job['kind'], job['workspace_id']) == ('running', 2, 'profile', people)
    done = wait(client, job)
    assert (done['state'], done['tables_done'], done['tables_total'], done['current_table']) == ('done', 2, 2, None)
    assert done['elapsed_s'] >= 0 and done['finished_at'] and done['errors'] == []
    overview = client.get(f'/api/workspaces/{people}/profile').json()
    assert overview['job']['id'] == job['id'] and overview['job']['state'] == 'done'
    profiled = {t['id']: bool(t['profiled_at']) for t in overview['tables']}
    assert profiled == {'people': True, 'log_lines': True, 'empty_table': False}        # only what was asked for
    assert client.get('/api/jobs/nope').status_code == 404 and client.delete('/api/jobs/nope').status_code == 404
    assert client.post(f'/api/workspaces/{people}/profile', json={'tables': ['no_such']}).status_code == 404
    assert client.post('/api/workspaces/nope/profile', json={}).status_code == 404


def test_a_job_can_be_cancelled_and_keeps_what_it_finished(client, app, people, monkeypatch):
    real = service_module.Profiling._profile_one
    reached = []

    def slow(self, job, target, cache, estimates):
        if target.table.name == 'log_lines':             # the second table "runs" until it is cancelled
            reached.append(1)
            while True:
                job.check()
                time.sleep(0.005)
        return real(self, job, target, cache, estimates)
    monkeypatch.setattr(service_module.Profiling, '_profile_one', slow)
    job = client.post(f'/api/workspaces/{people}/profile', json={'tables': ['people', 'log_lines', 'empty_table']}).json()
    for _ in range(2000):
        if reached:
            break
        time.sleep(0.005)
    running = client.get(f'/api/jobs/{job["id"]}').json()
    assert (running['state'], running['tables_done'], running['current_table']) == ('running', 1, 'log_lines')

    busy = client.post(f'/api/workspaces/{people}/profile', json={'tables': 'all'})     # one job per workspace
    assert busy.status_code == 409 and busy.json()['job']['id'] == job['id']
    assert client.delete(f'/api/workspaces/{people}/profile').status_code == 409
    assert client.post(f'/api/workspaces/{people}/tables/people/count').status_code == 409

    asked = client.delete(f'/api/jobs/{job["id"]}')
    assert asked.status_code == 200
    done = wait(client, job)
    assert (done['state'], done['tables_done'], done['tables_total']) == ('cancelled', 1, 3)
    assert table_profile(client, people, 'people')['profiled'] is True           # finished before the cancel: kept
    assert table_profile(client, people, 'log_lines')['profiled'] is False
    assert table_profile(client, people, 'empty_table')['profiled'] is False
    monkeypatch.setattr(service_module.Profiling, '_profile_one', real)
    assert profile_all(client, people)['state'] == 'done'                        # a new job can start afterwards


def test_jobs_live_in_memory_but_results_are_saved(client, app, people, tmp_path):
    job = profile_all(client, people)
    restarted = create_app(tmp_path / 'data')                                    # the app is started again
    with local_client(restarted) as again:
        assert again.get(f'/api/jobs/{job["id"]}').status_code == 404            # the job is gone
        overview = again.get(f'/api/workspaces/{people}/profile').json()
        assert overview['job'] is None and all(t['profiled_at'] for t in overview['tables'])
        assert stats_of(table_profile(again, people, 'people'))['city']['distinct'] == 4       # the statistics are not


def test_row_counts_only(client, people):
    job = profile_all(client, people, counts_only=True)
    assert (job['state'], job['counts_only'], job['tables_done'], job['tables_total']) == ('done', True, 3, 3)
    overview = client.get(f'/api/workspaces/{people}/profile').json()
    assert {t['id']: (t['row_count'], t['profiled_at']) for t in overview['tables']} == {
        'people': (N, None), 'empty_table': (0, None), 'log_lines': (2, None)}


def test_exact_count_action(client, people):
    counted = client.post(f'/api/workspaces/{people}/tables/people/count')
    assert counted.status_code == 200
    assert {k: counted.json()[k] for k in ('table', 'row_count', 'row_count_kind')} == {
        'table': 'people', 'row_count': N, 'row_count_kind': 'exact'}
    counts = {t['id']: t['row_count'] for t in client.get(f'/api/workspaces/{people}/profile').json()['tables']}
    assert counts == {'people': N, 'empty_table': None, 'log_lines': None}
    assert client.post(f'/api/workspaces/{people}/tables/nope/count').status_code == 404


def test_an_estimate_never_replaces_an_exact_count(app, people):
    store = app.state.profiling.store
    store.set_count(people, 'PEOPLE', 's1', 190, 'estimate')
    assert app.state.profiling.row_counts(people)['PEOPLE'] == (190, 'estimate')
    store.set_count(people, 'PEOPLE', 's1', N, 'exact')
    store.set_count(people, 'PEOPLE', 's1', 195, 'estimate')
    assert app.state.profiling.row_counts(people)['PEOPLE'] == (N, 'exact')
    store.set_count(people, 'PEOPLE', 'another-source', 7, 'estimate')           # other data: the old count is void
    assert app.state.profiling.row_counts(people)['PEOPLE'] == (7, 'estimate')


def test_sql_files_alone_have_nothing_to_profile(client, ws, demo_sql):
    client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(demo_sql)})
    refused = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'})
    assert refused.status_code == 400 and 'structure only' in refused.json()['detail']
    p = table_profile(client, ws, 'customers')
    assert (p['can_profile'], p['reason'], p['sample'], p['profiled']) == (False, 'structure only, no data to profile', None, False)
    assert all(c['stats'] is None for c in p['columns'])
    assert p['columns'][1]['name'] == 'full_name' and p['columns'][1]['personal'] is True     # known from the name alone
    overview = client.get(f'/api/workspaces/{ws}/profile').json()
    assert all(not t['can_profile'] and t['reason'] == 'structure only, no data to profile' for t in overview['tables'])


def test_mixed_workspace_profiles_the_tables_that_have_data(client, ws, demo_dir, tmp_path):
    client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(demo_dir / 'telecom.sql')})
    exports = tmp_path / 'exports'
    exports.mkdir()
    (exports / 'plans.csv').write_bytes((demo_dir / 'csv' / 'plans.csv').read_bytes())
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(exports)})
    job = profile_all(client, ws)
    assert (job['tables_total'], job['tables_done'], job['errors']) == (1, 1, [])
    plans = table_profile(client, ws, 'plans')
    assert (plans['row_count'], plans['source']['kind'], plans['profiled']) == (10, 'files', True)
    st = stats_of(plans)
    assert st['plan_name']['distinct'] == 10 and st['plan_name']['masked'] is False       # a plan's name is not personal
    assert table_profile(client, ws, 'customers')['can_profile'] is False


def test_a_database_password_is_asked_for_before_a_job_starts(client, app, ws):
    store = app.state.workspaces
    settings = {'engine': 'postgresql', 'host': '127.0.0.1', 'port': 9, 'database': 'crm', 'username': 'reader',
                'schema_filter': '', 'include_views': False}
    snapshot = {'tables': [{'name': 'orders', 'schema': 'public', 'origins': [
        {'kind': 'db', 'source_id': 'src1', 'label': 'crm', 'detail': 'public', 'schema': 'public', 'table': 'orders'}],
        'columns': [{'name': 'order_id', 'type': 'INTEGER', 'origins': ['db'], 'types': {'db': 'INTEGER'}, 'pk': True}]}],
        'relationships': []}
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('src1', ws, 'database', 'crm', json.dumps(settings), json.dumps(snapshot), '[]', '2026', '2026'))
    asked = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'})
    assert asked.status_code == 409
    assert asked.json()['needs_password'] is True and asked.json()['source'] == {'id': 'src1', 'label': 'crm'}
    assert table_profile(client, ws, 'public.orders')['sample']['needs_password'] is True
    # with the password the job starts; nothing listens on that port, so the table fails cleanly
    job = wait(client, client.post(f'/api/workspaces/{ws}/profile',
                                   json={'tables': 'all', 'passwords': {'src1': 'S3cret-Pa55'}}).json())
    assert job['state'] in ('done', 'failed') and 'S3cret-Pa55' not in json.dumps(job)
    assert store.password(ws, 'src1') == 'S3cret-Pa55'                           # in memory only


# ------------------------------------------------------------- personal data

def test_personal_columns_are_found_by_name_and_by_values(people_profile):
    columns = {c['name']: c for c in people_profile['columns']}
    assert {n: c['personal'] for n, c in columns.items()} == {
        'person_id': False, 'full_name': True, 'contact': True, 'email': True, 'ref_no': True, 'city': False,
        'note': False, 'amount': False, 'score': False, 'joined': False, 'born': False, 'photo': False,
        'active': False, 'unused': False, 'rare': False}
    assert columns['full_name']['personal_reason'] == 'Looks like name'
    assert columns['contact']['stats']['detected'] == 'phone'                    # nothing in its name says so
    assert columns['contact']['personal_reason'] == 'Looks like phone number'
    assert columns['ref_no']['stats']['detected'] == 'national_id'
    assert columns['email']['personal_reason'] == 'Looks like e-mail'
    assert all(c['personal_marked'] is None for c in columns.values())


def test_personal_values_are_masked_but_counts_still_show(people_profile):
    st = stats_of(people_profile)
    name = st['full_name']
    assert name['masked'] is True and (name['nulls'], name['distinct'], name['rows']) == (0, N, N)
    assert name['min'] == mask('Ahmed Al-Test008') and name['max'] == mask('Sara Al-Test193')
    assert name['min'] == 'Ah************08' and (name['min_length'], name['max_length']) == (15, 17)
    contact = st['contact']
    assert contact['min'] == '05******37' and contact['max'] == '05******00' and contact['masked'] is True
    email = st['email']
    assert (email['min'], email['max'], email['nulls'], email['distinct']) == ('u***@m***.com', 'u***@m***.com', 20, 180)
    assert 'histogram' not in email and 'avg' not in st['ref_no']
    text = json.dumps(people_profile)
    for raw in ('Al-Test', '@mail.example.com', '0510000037', '2100000001'):
        assert raw not in text                                                   # not in statistics, not in sample rows


def test_top_values_of_a_personal_column_are_masked(client, ws, tmp_path):
    folder = tmp_path / 'csv'
    folder.mkdir()
    rows = ['agent_name,calls'] + [f'{name},{i}' for i, name in enumerate(['Ahmed Al-Harbi'] * 6 + ['Sara Al-Otaibi'] * 3
                                                                           + ['Omar Al-Ghamdi'])]
    (folder / 'calls.csv').write_text('\n'.join(rows) + '\n')
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    profile_all(client, ws)
    st = stats_of(table_profile(client, ws, 'calls'))['agent_name']
    assert st['top'] == [['Ah**********bi', 6], ['Sa**********bi', 3], ['Om**********di', 1]]
    assert on_disk(tmp_path, 'Ahmed Al-Harbi') == [] and on_disk(tmp_path, 'Sara Al-Otaibi') == []
    assert on_disk(tmp_path, 'Ah**********bi') == ['explorer.db']                # what is saved is the mask


def test_the_user_can_mark_and_unmark_a_column(client, people, people_profile, tmp_path):
    def post(column, personal):
        return client.post(f'/api/workspaces/{people}/personal', json={'table': 'people', 'column': column,
                                                                       'personal': personal})
    assert on_disk(tmp_path, 'Riyadh') == ['explorer.db']                        # city's top values are saved as they are
    marked = post('city', True)
    assert marked.status_code == 200 and marked.json() == {
        'table': 'people', 'column': 'city', 'personal': True, 'personal_marked': True,
        'personal_reason': 'Marked personal by you', 'reprofile': False}
    assert on_disk(tmp_path, 'Riyadh') == [] and on_disk(tmp_path, 'Jeddah') == []       # masked on disk at once
    p = table_profile(client, people, 'people')
    city = next(c for c in p['columns'] if c['name'] == 'city')
    assert city['personal'] and city['personal_marked'] is True and city['stats']['masked'] is True
    assert city['stats']['top'][0] == ['****', 50] and city['stats']['min'] == '****'      # four characters: nothing kept
    assert dict(zip(p['sample']['columns'], p['sample']['rows'][0]))['city'] == 'J****h'

    unmarked = post('email', False)                                              # the other way: not personal
    assert unmarked.json()['personal'] is False and unmarked.json()['reprofile'] is True
    p = table_profile(client, people, 'people')
    email = next(c for c in p['columns'] if c['name'] == 'email')
    assert (email['personal'], email['personal_marked'], email['personal_reason']) == (False, False, 'Marked not personal by you')
    assert email['stats']['stale_mask'] is True and email['stats']['min'] == 'u***@m***.com'   # masking is one-way
    assert dict(zip(p['sample']['columns'], p['sample']['rows'][0]))['email'] == 'user001@mail.example.com'
    profile_all(client, people)                                                  # the values return with a new profile
    email = stats_of(table_profile(client, people, 'people'))['email']
    assert email['min'] == 'user001@mail.example.com' and email['masked'] is False
    city = stats_of(table_profile(client, people, 'people'))['city']
    assert city['masked'] is True and city['top'][0][0] == '****'                # the mark survived the new profile

    assert post('city', None).json() == {'table': 'people', 'column': 'city', 'personal': False,
                                         'personal_marked': None, 'personal_reason': None, 'reprofile': True}
    assert post('nope', True).status_code == 404 and post('city', 'yes').status_code == 400
    assert client.post(f'/api/workspaces/{people}/personal', json={'table': 'nope', 'column': 'x'}).status_code == 404


def test_marks_are_saved_per_workspace(client, app, people, people_db, tmp_path):
    client.post(f'/api/workspaces/{people}/personal', json={'table': 'people', 'column': 'city', 'personal': True})
    other = client.post('/api/workspaces', json={'name': 'other'}).json()['id']
    client.post(f'/api/workspaces/{other}/sources/db', json={'engine': 'sqlite', 'database': str(people_db)})
    flags = lambda w, c: {x['name']: x['personal'] for x in table_profile(c, w, 'people', sample=False)['columns']}['city']
    assert flags(people, client) is True and flags(other, client) is False
    with local_client(create_app(tmp_path / 'data')) as again:                   # and they survive a restart
        assert flags(people, again) is True


def test_show_personal_values_is_a_switch_in_memory(client, app, people, people_profile, tmp_path):
    assert client.get('/api/session').json() == {'show_personal': False}
    assert client.get(f'/api/workspaces/{people}/profile').json()['show_personal'] is False
    assert client.put('/api/session', json={'show_personal': True}).json() == {'show_personal': True}
    p = table_profile(client, people, 'people')
    assert p['show_personal'] is True and p['sample']['masked'] is False
    st = stats_of(p)
    assert st['full_name']['min'] == 'Ahmed Al-Test008' and st['full_name']['live'] is True     # read again, live
    assert st['contact']['min'] == '0510000037' and st['email']['max'] == 'user199@mail.example.com'
    assert dict(zip(p['sample']['columns'], p['sample']['rows'][0]))['full_name'] == 'Sara Al-Test001'
    assert st['city'].get('live') is None                                        # not personal: as saved
    # the real values were shown, and still nothing of them is on disk
    for raw in ('Al-Test', '@mail.example.com', '0510000037'):
        assert on_disk(tmp_path, raw) == [], raw
    assert client.put('/api/session', json={'show_personal': 'yes'}).json() == {'show_personal': False}   # only true
    assert stats_of(table_profile(client, people, 'people'))['full_name']['min'] == mask('Ahmed Al-Test008')
    client.put('/api/session', json={'show_personal': True})
    with local_client(create_app(tmp_path / 'data')) as again:                   # a restart always begins masked
        assert again.get('/api/session').json() == {'show_personal': False}


# ---------------------------------------------------------------------- storage

def test_a_version_1_file_is_upgraded_in_place(tmp_path):
    folder = tmp_path / 'old'
    folder.mkdir()
    con = sqlite3.connect(folder / 'explorer.db')
    con.executescript(SCHEMA_V1)
    con.execute('PRAGMA user_version = 1')
    con.execute("INSERT INTO workspaces VALUES ('w1', 'Billing', '2026-10-01T10:00:00', '2026-10-01T10:00:00')")
    con.execute("INSERT INTO sources VALUES ('s1', 'w1', 1, 'sql_files', 'ddl', '{}', "
                "'{\"tables\": [{\"name\": \"orders\", \"origins\": [{\"kind\": \"ddl\"}], "
                "\"columns\": [{\"name\": \"order_id\", \"type\": \"INT\", \"pk\": true}]}], \"relationships\": []}', "
                "'[]', '2026-10-01T10:00:00', '2026-10-01T10:00:00')")
    con.execute("INSERT INTO profiles VALUES ('p1', 'prod', '{\"engine\": \"oracle\"}', '2026-10-01T10:00:00')")
    con.commit()
    con.close()

    store = Workspaces(folder)
    con = sqlite3.connect(folder / 'explorer.db')
    assert con.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION == 4
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    assert tables == {'workspaces', 'sources', 'profiles', 'table_stats', 'column_stats', 'personal_marks',
                      'relation_checks', 'value_links', 'sql_history', 'script_scans'}
    assert 'attempt' in {r[1] for r in con.execute('PRAGMA table_info(table_stats)')}
    con.close()
    assert [w['name'] for w in store.list_workspaces()] == ['Billing']           # nothing was lost
    assert [t.name for t in store.model('w1').tables] == ['orders'] and store.list_profiles()[0]['name'] == 'prod'
    Workspaces(folder)                                                            # opening it again changes nothing
    with local_client(create_app(folder)) as c:
        assert c.get('/api/workspaces/w1/quality').status_code == 200


def test_a_new_file_starts_at_the_current_version_and_a_newer_one_is_refused(tmp_path):
    Workspaces(tmp_path / 'fresh')
    con = sqlite3.connect(tmp_path / 'fresh' / 'explorer.db')
    assert con.execute('PRAGMA user_version').fetchone()[0] == 4
    con.execute('PRAGMA user_version = 9')
    con.close()
    with pytest.raises(Exception, match='newer version'):
        Workspaces(tmp_path / 'fresh')


def test_statistics_are_saved_with_a_timestamp_and_without_rows(client, people, people_profile, tmp_path):
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    con.row_factory = sqlite3.Row
    table = con.execute("SELECT * FROM table_stats WHERE table_key = 'PEOPLE'").fetchone()
    assert (table['row_count'], table['row_count_kind']) == (N, 'exact')
    assert table['profiled_at'][:4] == table['counted_at'][:4] == str(date.today().year)
    summary = json.loads(table['profile'])
    assert summary['sampled'] is False and summary['rows_profiled'] == N and summary['source_kind'] == 'database'
    assert 'sample_method' in summary and 'sample_plan' in summary
    rows = con.execute("SELECT column_key, position, stats, profiled_at FROM column_stats WHERE table_key = 'PEOPLE' "
                       'ORDER BY position').fetchall()
    assert [r['column_key'] for r in rows][:3] == ['PERSON_ID', 'FULL_NAME', 'CONTACT'] and len(rows) == 15
    assert all(r['profiled_at'] == table['profiled_at'] for r in rows)
    columns = {r[1] for r in con.execute('PRAGMA table_info(table_stats)')} | {
        r[1] for r in con.execute('PRAGMA table_info(column_stats)')}
    con.close()
    assert not any('row' in c and c not in ('row_count', 'row_count_kind') for c in columns) and 'sample' not in columns
    stored = {r['column_key']: json.loads(r['stats']) for r in rows}
    assert set(stored['CITY']) >= {'nulls', 'distinct', 'min', 'max', 'top', 'rows'}
    assert not any('sample_rows' in s or 'rows_data' in s for s in stored.values())


def test_sample_rows_are_never_written_to_disk(client, people, people_profile, tmp_path):
    """Like the password test: read every file in the data folder and look for the rows' values."""
    assert len(people_profile['sample']['rows']) == 20                           # the rows were read and shown ...
    client.get(f'/api/workspaces/{people}/tables/people/sample')
    client.put('/api/session', json={'show_personal': True})                     # ... even unmasked
    assert table_profile(client, people, 'people')['sample']['masked'] is False
    client.put('/api/session', json={'show_personal': False})
    files = data_files(tmp_path)
    assert [p.name for p in files] == ['explorer.db']
    blob = b''.join(p.read_bytes() for p in files)
    # "note" holds a different marker in every row. Only its smallest and largest value may be on
    # disk (they are the column's min and max); the other 198 could only get there with the rows.
    markers = [f'ROWMARKER-{i:04d}'.encode() for i in range(1, N + 1)]
    assert [m for m in markers if m in blob] == [b'ROWMARKER-0001', b'ROWMARKER-0200']
    # personal columns: no value of any row, in any form
    for i in range(1, N + 1):
        for value in (f'Al-Test{i:03d}', f'05{10000000 + i * 37:08d}', f'user{i:03d}@mail', f'{1 + i % 2}{100000000 + i:09d}'):
            assert value.encode() not in blob, value
    assert mask('Ahmed Al-Test008').encode() in blob and b'05******37' in blob and b'u***@m***.com' in blob


def test_min_max_and_top_values_of_personal_columns_are_saved_masked_only(people, people_profile, tmp_path):
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    stored = {key: json.loads(stats) for key, stats in
              con.execute("SELECT column_key, stats FROM column_stats WHERE table_key = 'PEOPLE'")}
    con.close()
    for key in ('FULL_NAME', 'CONTACT', 'EMAIL', 'REF_NO'):
        st = stored[key]
        assert st['masked'] is True and '*' in st['min'] and '*' in st['max'], key
        assert all('*' in value for value, _ in st.get('top', [])) and 'histogram' not in st
    assert stored['CITY']['masked'] is False and stored['CITY']['min'] == 'Abha'  # the others are saved as they are


def test_clear_saved_statistics(client, people, people_profile, tmp_path):
    client.post(f'/api/workspaces/{people}/personal', json={'table': 'people', 'column': 'score', 'personal': True})
    assert on_disk(tmp_path, 'Dammam') == ['explorer.db'] and on_disk(tmp_path, 'ROWMARKER-0001') == ['explorer.db']
    cleared = client.delete(f'/api/workspaces/{people}/profile')
    assert cleared.status_code == 200 and cleared.json() == {'cleared_tables': 3}
    for gone in ('Dammam', 'ROWMARKER-0001', mask('Ahmed Al-Test008'), '05******37'):
        assert on_disk(tmp_path, gone) == [], gone                               # not even in the file's free pages
    overview = client.get(f'/api/workspaces/{people}/profile').json()
    assert all(t['row_count'] is None and t['profiled_at'] is None for t in overview['tables'])
    p = table_profile(client, people, 'people', sample=False)
    assert p['profiled'] is False and all(c['stats'] is None for c in p['columns'])
    assert next(c for c in p['columns'] if c['name'] == 'score')['personal_marked'] is True     # marks are settings
    assert client.get(f'/api/workspaces/{people}/model').json()['workspace']['sources'][0]['tables'] == 3
    assert client.delete('/api/workspaces/nope/profile').status_code == 404


def test_statistics_go_when_their_source_goes(client, app, people, people_profile):
    source = client.get(f'/api/workspaces/{people}').json()['sources'][0]['id']
    assert len(app.state.profiling.store.tables(people)) == 3
    assert client.post(f'/api/workspaces/{people}/sources/{source}/refresh', json={}).status_code == 200
    assert len(app.state.profiling.store.tables(people)) == 3                     # reading it again keeps them
    client.delete(f'/api/workspaces/{people}/sources/{source}')
    assert app.state.profiling.store.tables(people) == {} and app.state.profiling.store.columns(people) == {}


def test_deleting_a_workspace_deletes_its_statistics(client, app, people, people_profile, tmp_path):
    client.delete(f'/api/workspaces/{people}')
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    assert [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            for t in ('table_stats', 'column_stats', 'personal_marks')] == [0, 0, 0]
    con.close()


# ------------------------------------------------------------------------ files

def test_csv_files_are_profiled_in_place(client, ws, demo_csv, tmp_path):
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(demo_csv)})
    job = profile_all(client, ws)
    assert (job['tables_done'], job['errors']) == (10, [])
    customers = table_profile(client, ws, 'customers')
    assert (customers['row_count'], customers['row_count_kind'], customers['source']['kind']) == (80, 'exact', 'files')
    st = stats_of(customers)
    assert st['city']['top'][0] == ['Medina', 13] and st['email']['nulls'] == 9
    assert st['national_id']['detected'] == 'national_id' and st['national_id']['masked'] is True
    assert st['phone']['min'] == '96********60' and st['created_at']['histogram']['kind'] == 'year'
    assert customers['sample']['rows'][0][:2] == [1, 'Mo*********mi']
    assert sorted(p.name for p in (tmp_path / 'data').iterdir()) == ['explorer.db']      # no copy, no temporary file
    assert not (tmp_path / '.tmp').exists()


def test_excel_sheets_and_other_encodings_are_profiled(client, ws, tmp_path):
    folder = tmp_path / 'exports'
    folder.mkdir()
    wb = Workbook()
    first = wb.active
    first.title = 'orders'
    first.append(['order_id', 'customer_mobile', 'total', 'placed'])
    for i in range(1, 41):
        first.append([i, f'05{50000000 + i:08d}', i * 2.5, date(2026, 1, 1) + timedelta(days=i)])
    second = wb.create_sheet('codes')
    second.append(['code', 'label'])
    for i in range(1, 6):
        second.append([str(100 + i), f'label {i}'])            # numbers typed as text
    wb.save(folder / 'shop.xlsx')
    (folder / 'cities.csv').write_bytes('id,city\n1,الرياض\n2,جدة\n3,الرياض\n'.encode('cp1256'))
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    job = profile_all(client, ws)
    assert (job['state'], job['tables_done'], job['errors']) == ('done', 3, [])

    orders = table_profile(client, ws, 'shop.orders')
    assert (orders['row_count'], orders['source']['kind']) == (40, 'files')
    st = stats_of(orders)
    assert (st['order_id']['min'], st['order_id']['max'], st['total']['avg']) == (1, 40, 51.25)
    assert st['placed']['family'] == 'date' and st['placed']['histogram']['kind'] == 'month'
    assert st['customer_mobile']['detected'] == 'phone' and st['customer_mobile']['masked'] is True
    assert orders['sample']['rows'][0][1] == '05******01'
    cities = table_profile(client, ws, 'cities')
    assert stats_of(cities)['city']['top'] == [['الرياض', 2], ['جدة', 1]]        # cp1256, decoded
    assert sorted(p.name for p in (tmp_path / 'data').iterdir()) == ['explorer.db']
    assert sorted(p.name for p in folder.iterdir()) == ['cities.csv', 'shop.xlsx']       # nothing written next to them


def test_big_files_are_sampled(tmp_path):
    path = tmp_path / 'big.csv'
    path.write_text('id,kind\n' + ''.join(f'{i},{"a" if i % 4 else "b"}\n' for i in range(1, 5001)))
    data = FileData(path, threshold=1000, sample_rows=500).open()
    assert data.total_rows == 5000 and data.sampled and 250 < data.loaded_rows < 800
    assert 'random sample of about 10.00% of the rows' in data.method
    columns = [q.ColumnPlan.of(n, t, q.DUCKDB) for n, t in data.columns]
    result = profile_table(data, columns)
    data.close()
    table = result['table']
    assert (table['row_count'], table['row_count_kind'], table['sampled']) == (5000, 'exact', True)
    assert table['rows_profiled'] == data.loaded_rows and table['sample_random'] is True
    assert 0.15 < result['columns']['kind']['top'][1][1] / data.loaded_rows < 0.35       # about a quarter are "b"
    whole = FileData(path).open()                                                # default threshold: all of it
    assert (whole.sampled, whole.loaded_rows, whole.method) == (False, 5000, None)
    whole.close()


def test_duckdb_writes_no_temporary_files_and_is_cut_off_after_loading(tmp_path, demo_csv):
    data = FileData(demo_csv / 'plans.csv').open()
    settings = dict(data._con.execute("SELECT name, value FROM duckdb_settings() WHERE name IN "
                                      "('temp_directory', 'autoinstall_known_extensions', 'enable_external_access')").fetchall())
    assert settings == {'temp_directory': '', 'autoinstall_known_extensions': 'false', 'enable_external_access': 'false'}
    with pytest.raises(Exception, match='disabled|Permission'):                  # after loading: memory only
        data._con.execute("SELECT * FROM read_csv('" + str(demo_csv / 'plans.csv').replace('\\', '/') + "')").fetchall()
    data.close()
    with pytest.raises(QueryFailed, match='file not found'):
        FileData(tmp_path / 'missing.csv').open()
