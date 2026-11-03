"""The live adapter on real servers: Oracle, SQL Server, PostgreSQL and MySQL in local Docker containers.

    scripts\\live_db.ps1 up        start and seed the containers (see scripts/live_engines.py)
    scripts\\live_db.ps1 test      run these tests
    scripts\\live_db.ps1 stop

Every test is marked `live` and is skipped when its container is not running, so a plain
`pytest` works without Docker. The app connects as the read-only account explorer_ro; the admin
account is only used here to check afterwards that the data did not change.
"""

import re
import time

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from app import config
from app.profiling import queries as q
from app.profiling import service as service_module
from app.profiling.data import DatabaseData, QueryFailed
from app.profiling.profiler import catalog_estimates, profile_table, read_table
from app.security import ReadOnlyViolation, check_read_only_sql
from app.sources import SourceError
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from expected import COLUMN_COUNTS, DECLARED_KEYS, PRIMARY_KEYS, declared
from scripts import live_engines as le

pytestmark = pytest.mark.live

BIG = le.BIG_TABLE
BIG_COLUMNS = ['event_id', 'subscription_id', 'event_time', 'event_type', 'duration_s', 'data_mb', 'msisdn', 'cell_id']
# customers.customer_id, customers.full_name, customers.created_at, tickets.opened_at, accounts.credit_limit
TYPES = {
    'oracle': ['NUMBER(10, 0)', 'VARCHAR(120 CHAR)', 'DATE', 'TIMESTAMP', 'NUMBER(10, 2)'],
    'mssql': ['INTEGER', 'VARCHAR(120)', 'DATE', 'DATETIME2', 'DECIMAL(10, 2)'],
    'postgresql': ['INTEGER', 'VARCHAR(120)', 'DATE', 'TIMESTAMP WITHOUT TIME ZONE', 'NUMERIC(10, 2)'],
    'mysql': ['INTEGER', 'VARCHAR(120)', 'DATE', 'DATETIME', 'DECIMAL(10, 2)'],
}
# schema filter -> number of tables it must give (the account's default schema when empty)
FILTERS = {
    'oracle': {'TELECOM': 11, 'telecom': 11, 'OTHER': 1, 'TELECOM, OTHER': 12, '': 0},
    'mssql': {'': 11, 'dbo': 11, 'other': 1, 'dbo, other': 12, 'telecom.dbo': 11},
    'postgresql': {'telecom': 11, 'other': 1, 'telecom, other': 12, '': 1},      # public: x_guests (dx_extra version 7)
    'mysql': {'': 11, 'telecom': 11, 'other': 1, 'telecom, other': 12},
}
# what a write is refused with when it gets past the guard, on the raw driver connection
REFUSAL = {'oracle': 'ORA-01456|ORA-01031|ORA-41900|read.only', 'mssql': 'permission was denied',
           'postgresql': 'read-only transaction', 'mysql': 'READ ONLY transaction|command denied'}
TIMED_OUT = {'oracle': 'DPY-4024|DPY-4011|ORA-03156|timeout', 'mssql': 'timed out|20003|20047',
             'postgresql': 'statement timeout', 'mysql': 'maximum statement execution time|3024|interrupted'}


def upper(engine):
    return engine == 'oracle'


def name(engine, value):
    return value.upper() if upper(engine) else value


def qualified(engine, table):
    spec = le.ENGINES[engine]
    return f'{spec["schema"]}.{name(engine, table)}'


@pytest.fixture(scope='module', params=list(le.ENGINES))
def engine(request):
    if not le.reachable(request.param):
        pytest.skip(f'{request.param}: test container not running or not seeded (scripts\\live_db.ps1 up)')
    return request.param


def open_db(engine, timeout_s=None, **overrides):
    profile = ConnectionProfile.from_dict(le.reader_profile(engine, **overrides))
    return ReadOnlyDatabase(profile, le.READER_PASSWORD, timeout_s=timeout_s)


@pytest.fixture
def db(engine):
    database = open_db(engine)
    yield database
    database.dispose()


@pytest.fixture(scope='module')
def schema(engine):
    database = open_db(engine)
    try:
        model, notes = database.read_schema('live', 'src')
        return model, notes, list(database.statements), list(database.blocked)
    finally:
        database.dispose()


def lower_keys(model):
    return {tuple(part.lower() for part in key) for key in declared(model)}


# ----------------------------------------------------------- connecting

def test_connection_test_works(engine, db):
    info = db.test()
    assert info['ok'] and info['engine'] == engine and info['server_version']
    assert info['tables'] == FILTERS[engine][le.ENGINES[engine]['schema_filter']] == 11
    assert le.ENGINES[engine]['schema'] in info['schemas'] and le.ENGINES[engine]['other'] in info['schemas']
    assert '30 s statement timeout' in info['protections']
    if engine == 'mssql':                       # no read-only session mode there: the guard and the account
        assert info['protections'] == ['30 s statement timeout']
    else:
        assert {'read-only transaction', 'read-only session'} & set(info['protections'])
    assert info['default_schema'] == {'oracle': 'EXPLORER_RO', 'mssql': 'dbo', 'postgresql': 'public',
                                      'mysql': 'telecom'}[engine]


def test_a_wrong_password_is_a_clean_error(engine):
    profile = ConnectionProfile.from_dict(le.reader_profile(engine))
    database = ReadOnlyDatabase(profile, 'not-the-password-123')
    with pytest.raises(SourceError) as failure:
        database.test()
    assert 'not-the-password-123' not in str(failure.value) and len(str(failure.value)) < 700
    database.dispose()


# ------------------------------------------------- tables, keys, names, types

def test_reading_gives_the_demo_tables_the_big_table_and_all_13_keys(engine, schema):
    model, notes, _, _ = schema
    assert notes == []
    assert {t.name.lower(): len(t.columns) for t in model.tables} == {**COLUMN_COUNTS, BIG: len(BIG_COLUMNS)}
    assert lower_keys(model) == DECLARED_KEYS and len(model.relationships) == 13
    assert all(r.source == 'declared' and r.cardinality == '1-N' and r.name.startswith('fk_')
               for r in model.relationships)
    for t in model.tables:
        assert t.kind == 'table' and t.complete and [o.kind for o in t.origins] == ['db']
        key = [c.name for c in t.columns if c.pk]
        assert [k.lower() for k in key] == [PRIMARY_KEYS.get(t.name.lower(), 'event_id')]
    big = model.find(name(engine, BIG))
    assert [c.name.lower() for c in big.columns] == BIG_COLUMNS
    assert not any(c.fk for c in big.columns)                  # its subscription_id has no declared key


def test_names_come_back_in_the_engines_own_case(engine, schema):
    model = schema[0]
    spec = le.ENGINES[engine]
    assert {t.schema for t in model.tables} == {spec['schema']}
    customers = model.find(name(engine, 'customers'))
    assert customers.id == f'{spec["schema"]}.{name(engine, "customers")}'
    assert [c.name for c in customers.columns] == [name(engine, c) for c in (
        'customer_id', 'full_name', 'national_id', 'email', 'phone', 'city', 'segment', 'branch_id', 'created_at')]
    if upper(engine):                                          # Oracle: upper case, not SQLAlchemy's lower case
        assert customers.id == 'TELECOM.CUSTOMERS' and customers.column('customer_id').name == 'CUSTOMER_ID'
        assert all(t.name == t.name.upper() and all(c.name == c.name.upper() for c in t.columns)
                   for t in model.tables)
    origin = customers.origins[0]                              # the catalog's own names, for profiling queries
    assert (origin.schema, origin.table) == (spec['schema'], name(engine, 'customers'))
    key = next(r for r in model.relationships if r.from_table is customers)
    assert (key.from_columns, key.to_table.name, key.to_columns) == (
        [name(engine, 'branch_id')], name(engine, 'branches'), [name(engine, 'branch_id')])


def test_types_and_nullability_look_right(engine, schema):
    model = schema[0]
    customers, tickets, accounts = (model.find(name(engine, t)) for t in ('customers', 'tickets', 'accounts'))
    seen = [customers.column('customer_id').type, customers.column('full_name').type,
            customers.column('created_at').type, tickets.column('opened_at').type, accounts.column('credit_limit').type]
    assert seen == TYPES[engine]
    assert not any('COLLATE' in (c.type or '').upper() for t in model.tables for c in t.columns)
    nullable = {c.name.lower(): c.nullable for c in customers.columns}
    assert nullable == {'customer_id': False, 'full_name': False, 'national_id': False, 'email': True, 'phone': True,
                        'city': True, 'segment': False, 'branch_id': True, 'created_at': False}
    assert customers.column('branch_id').fk and not customers.column('city').fk


def test_schema_filter(engine, extras):
    for schema_filter, expected in FILTERS[engine].items():
        database = open_db(engine, schema_filter=schema_filter)
        try:
            model, notes = database.read_schema()
            assert len(model.tables) == expected, (schema_filter, sorted(t.id for t in model.tables), notes)
            assert database.test()['tables'] == expected, schema_filter
            assert database.blocked == []
        finally:
            database.dispose()
    spec = le.ENGINES[engine]
    database = open_db(engine, schema_filter=spec['other'])
    (decoy,) = database.read_schema()[0].tables
    assert decoy.name.lower() == 'decoy' and decoy.schema.lower() == 'other'
    database.dispose()


# ---------------------------------------------------- the guard and the catalog

def test_no_catalog_statement_is_blocked_by_the_guard(engine, schema):
    _, _, statements, blocked = schema
    assert blocked == []                                       # nothing the inspector sends is refused
    assert len(statements) >= 5
    for statement in statements:
        check_read_only_sql(statement, dialect=engine)         # and every one sent passes the guard
    first_words = {s.split(None, 1)[0].upper() for s in statements}
    assert first_words <= {'SELECT', 'WITH', 'SHOW', 'SET', 'USE'}, first_words
    settings = {s for s in statements if s.upper().startswith('SET')}
    assert settings == {'oracle': {'SET TRANSACTION READ ONLY'}, 'mssql': set(),
                        'postgresql': {'SET TRANSACTION READ ONLY', 'SET LOCAL statement_timeout = 30000'},
                        'mysql': {'SET SESSION TRANSACTION READ ONLY', 'SET SESSION MAX_EXECUTION_TIME = 30000'}}[engine]


def test_everything_the_server_receives_is_a_read(engine):
    """The server's own log of the read-only account while the app connects, tests and reads the
    schema: every statement is a read that passes the guard, or one of the few fixed statements
    the driver sends by itself (listed in scripts/live_engines.py)."""
    with le.server_statements(engine) as received:
        database = open_db(engine)
        database.test()
        database.read_schema()
        sent = list(database.statements)
        database.dispose()
    assert len(received) >= len(sent) - 2 > 5, (len(received), len(sent))      # the log really saw the session
    not_reads = []
    for statement in received:
        if le.is_driver_statement(engine, statement):
            continue
        try:
            check_read_only_sql(statement, dialect=engine)
        except ReadOnlyViolation:
            not_reads.append(statement)
    assert not_reads == []
    words = {s.split(None, 1)[0].upper() for s in received}
    assert not words & {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'GRANT',
                        'COMMIT', 'CALL', 'EXEC', 'EXECUTE', 'DECLARE'}, words
    if engine == 'oracle':                      # no PL/SQL block: SQLAlchemy's isolation-level probe is switched off
        assert not any(s.upper().startswith('BEGIN') for s in received)


# ------------------------------------------------------------- read-only is real

WRITES = ["DELETE FROM {plans}", "UPDATE {plans} SET plan_name = 'x'", "INSERT INTO {plans} (plan_id) VALUES (999)",
          "DROP TABLE {plans}", "CREATE TABLE {plans}_copy (a INT)", "TRUNCATE TABLE {plans}",
          "SELECT * FROM {plans} FOR UPDATE"]


def test_writes_fail_and_the_data_is_unchanged(engine, db):
    before = le.table_counts(engine)
    plans = qualified(engine, 'plans')
    with le.admin_engine(engine).connect() as admin:
        names_before = admin.execute(text(f'SELECT plan_id, plan_name FROM {plans} ORDER BY plan_id')).fetchall()

    # 1. through the app's connection: the guard refuses every write before it is sent
    for template in WRITES:
        statement = template.format(plans=plans)
        with pytest.raises(ReadOnlyViolation):
            with db.connect() as conn:
                conn.exec_driver_sql(statement)
        assert statement in db.blocked and statement not in db.statements
    with pytest.raises(ReadOnlyViolation, match='never commits'):
        with db.connect() as conn:
            conn.exec_driver_sql(f'SELECT COUNT(*) FROM {plans}').scalar()
            conn.commit()

    # 2. past the guard, on the driver's own connection: the session / the account refuses it
    with db.connect() as conn:
        raw = conn.connection.dbapi_connection
        for statement in (f"UPDATE {plans} SET plan_name = 'changed'", f'DELETE FROM {plans}'):
            cursor = raw.cursor()
            with pytest.raises(Exception, match=REFUSAL[engine]):
                cursor.execute(statement)
            cursor.close()
            if engine == 'postgresql':                         # the failed statement aborted the transaction
                raw.rollback()
                break

    # 3. nothing changed
    assert le.table_counts(engine) == before and before['plans'] == 10 and before[BIG] == le.BIG_ROWS
    with le.admin_engine(engine).connect() as admin:
        assert admin.execute(text(f'SELECT plan_id, plan_name FROM {plans} ORDER BY plan_id')).fetchall() == names_before


def test_the_read_only_session_holds_even_for_an_account_that_may_write(engine):
    """PostgreSQL, Oracle, MySQL: the app's session is read-only whatever the account is allowed to do.
    (SQL Server has no such mode, so there the read-only account is what holds a write back.)"""
    if engine == 'mssql':
        pytest.skip('SQL Server has no read-only session mode')
    spec = le.ENGINES[engine]
    profile = ConnectionProfile.from_dict(le.reader_profile(engine, username=spec['admin_user']))
    database = ReadOnlyDatabase(profile, le.ADMIN_PASSWORD)
    plans = qualified(engine, 'plans')
    try:
        with database.connect() as conn:
            cursor = conn.connection.dbapi_connection.cursor()
            with pytest.raises(Exception, match='read.only|READ ONLY|ORA-01456'):
                cursor.execute(f"UPDATE {plans} SET plan_name = plan_name WHERE plan_id = -1")   # changes no row
            cursor.close()
            if engine == 'postgresql':
                conn.connection.dbapi_connection.rollback()
    finally:
        database.dispose()
    assert le.table_counts(engine)['plans'] == 10


# ------------------------------------------------------------ statement timeout

def test_the_statement_timeout_fires_on_a_slow_query(engine):
    database = open_db(engine, timeout_s=2)
    big = qualified(engine, BIG)
    slow = (f'SELECT COUNT(*) FROM {big} a CROSS JOIN {big} b '                 # 4,000,000,000,000 row pairs
            f'WHERE a.{name(engine, "duration_s")} + b.{name(engine, "data_mb")} < -1')
    started = time.monotonic()
    try:
        with pytest.raises(Exception, match=TIMED_OUT[engine]):
            with database.connect() as conn:
                conn.exec_driver_sql(slow).scalar()
        elapsed = time.monotonic() - started
        assert 1.5 < elapsed < 15, elapsed
        with database.connect() as conn:                       # the next connection works as usual
            assert conn.exec_driver_sql(f'SELECT COUNT(*) FROM {qualified(engine, "plans")}').scalar() == 10
        assert slow in database.statements and database.blocked == []
    finally:
        database.dispose()


# ------------------------------------------------------------ through the API

def test_adding_the_source_through_the_api(engine, client):
    body = {**le.reader_profile(engine), 'password': le.READER_PASSWORD, 'label': f'{engine} live'}
    tested = client.post('/api/db/test', json=body)
    assert tested.status_code == 200 and tested.json()['tables'] == 11, tested.text
    ws = client.post('/api/workspaces', json={'name': f'{engine} live'}).json()['id']
    added = client.post(f'/api/workspaces/{ws}/sources/db', json=body)
    assert added.status_code == 200, added.text
    source = added.json()
    assert (source['tables'], source['relationships'], source['warnings']) == (11, 13, [])
    assert source['has_password'] and 'password' not in source['settings'] and le.READER_PASSWORD not in added.text

    model = client.get(f'/api/workspaces/{ws}/model').json()
    # 13 declared keys, plus the three that are not declared and are found from the column names
    assert model['stats'] == {'tables': 11, 'columns': 78, 'relationships': 16, 'declared': 13, 'joins': 0, 'values': 0, 'names': 3}
    inferred = sorted((r['from'].split('.')[-1].lower(), r['from_columns'][0].lower(), r['to'].split('.')[-1].lower())
                      for r in model['relationships'] if r['source'] == 'names')
    assert inferred == [('payments', 'account_id', 'accounts'), ('tickets', 'branch_id', 'branches'),
                        (BIG, 'subscription_id', 'subscriptions')]
    table = client.get(f'/api/workspaces/{ws}/tables/{qualified(engine, "tickets")}').json()
    assert [c['name'].lower() for c in table['columns'] if c['pk']] == ['ticket_id']
    assert client.get(f'/api/workspaces/{ws}/export?format=drawio').status_code == 200


# ------------------------------------------------------------------ profiling

# Oracle samples ROWS up to DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB (1 GB); the test table is about 110 MB
SAMPLING = {'oracle': 'SAMPLE (5%): a random sample of rows', 'mssql': 'TABLESAMPLE (5%): a random sample of pages',
            'postgresql': 'TABLESAMPLE SYSTEM (5%): a random sample of blocks', 'mysql': 'first 100,000 rows, not random'}
CLAUSE = {'oracle': ' SAMPLE (5.0) SEED (', 'mssql': ' TABLESAMPLE (5.0 PERCENT) REPEATABLE (',
          'postgresql': ' TABLESAMPLE SYSTEM (5.0) REPEATABLE (', 'mysql': ' LIMIT '}


def is_read(statement):
    return statement.lstrip().upper().startswith(('SELECT', 'WITH'))


class Recorder(ReadOnlyDatabase):
    """Every ReadOnlyDatabase the service opens, so a test can see what was sent."""
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Recorder.opened.append(self)

    @classmethod
    def reads(cls):
        return [s for db in cls.opened for s in db.statements if is_read(s)]


@pytest.fixture
def recorder(monkeypatch):
    Recorder.opened = []
    monkeypatch.setattr(service_module, 'ReadOnlyDatabase', Recorder)
    return Recorder


@pytest.fixture(scope='module')
def extras(engine):
    """The third schema (dx_extra) exists in the version these tests expect."""
    le.ensure_extras(engine)
    return le.extra_schema(engine)


def wait_for(client, job, seconds=180):
    assert 'id' in job, job
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.05)
    raise AssertionError('profiling job did not finish')


@pytest.fixture
def live_ws(engine, client):
    ws = client.post('/api/workspaces', json={'name': f'{engine} profiling'}).json()['id']
    body = {**le.reader_profile(engine), 'password': le.READER_PASSWORD}
    assert client.post(f'/api/workspaces/{ws}/sources/db', json=body).status_code == 200
    return ws


def test_profiling_a_live_database(engine, client, live_ws):
    """Row counts from the catalog, the 2,000,000-row table on a sample, the rest in full."""
    ws = live_ws
    started = time.monotonic()
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    assert (job['state'], job['tables_done'], job['tables_total'], job['errors'], job['notes']) == ('done', 11, 11, [], [])
    assert time.monotonic() - started < 120

    overview = {t['id'].split('.')[-1].lower(): t for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    big, customers = overview[BIG], overview['customers']
    assert big['row_count_kind'] == 'estimate' and 1_800_000 <= big['row_count'] <= 2_200_000      # "~" in the page
    assert (customers['row_count'], customers['row_count_kind'], customers['sampled']) == (80, 'exact', False)
    assert {name: t['row_count'] for name, t in overview.items() if name != BIG} == {
        'branches': 8, 'customers': 80, 'accounts': 100, 'plans': 10, 'subscriptions': 150, 'devices': 110,
        'invoices': 200, 'invoice_lines': 513, 'payments': 177, 'tickets': 90}

    p = client.get(f'/api/workspaces/{ws}/tables/{qualified(engine, BIG)}/profile').json()
    assert (p['sampled'], p['sample_method'], p['row_count_kind']) == (True, SAMPLING[engine], 'estimate')
    assert p['sample_random'] is (engine != 'mysql')
    rows = p['rows_profiled']
    # a block or page sample is about 100,000 rows, never more than twice that; MySQL has no sampling clause;
    # Oracle's sample of rows is even (34 seeds gave 99,196 to 100,569: scripts/sample_spread.py)
    assert rows == 100_000 if engine == 'mysql' else (95_000 if engine == 'oracle' else 25_000) < rows <= (
        105_000 if engine == 'oracle' else 200_000), rows
    assert p['queries'] == 1 and p['notes'] == []
    st = {c['name'].lower(): c['stats'] for c in p['columns']}
    assert all(s['rows'] == rows and s.get('error') is None for s in st.values())
    assert st['event_type']['distinct'] == 4 and {v for v, _ in st['event_type']['top']} == {'data', 'voice', 'sms', 'roaming'}
    assert 0.66 < st['duration_s']['nulls'] / rows < 0.74 and 0.36 < st['data_mb']['nulls'] / rows < 0.44
    assert st['duration_s']['min'] >= 10 and st['duration_s']['max'] <= 599 and 250 < st['duration_s']['avg'] < 350
    assert st['subscription_id']['distinct'] == 150 and st['event_id']['unique'] is True
    assert st['duration_s']['histogram']['kind'] == 'number' and len(st['duration_s']['histogram']['bins']) == 12
    assert st['event_time']['histogram']['kind'] == 'month'
    assert st['msisdn']['masked'] is True and st['msisdn']['detected'] == 'phone' and '*' in st['msisdn']['min']
    assert st['event_type']['min_length'] == 3 and st['event_type']['max_length'] == 7
    sample = p['sample']
    assert len(sample['rows']) == 20 and sample['error'] is None and sample['masked'] is True
    first = dict(zip([c.lower() for c in sample['columns']], sample['rows'][0]))
    assert first['msisdn'].startswith('96') and '*' in first['msisdn'] and first['event_type'] in ('data', 'voice', 'sms', 'roaming')

    people = client.get(f'/api/workspaces/{ws}/tables/{qualified(engine, "customers")}/profile').json()
    st = {c['name'].lower(): c['stats'] for c in people['columns']}
    assert (people['sampled'], people['rows_profiled'], people['row_count_kind']) == (False, 80, 'exact')
    assert (st['email']['nulls'], st['city']['distinct'], st['customer_id']['min'], st['customer_id']['max']) == (9, 8, 1, 80)
    assert st['city']['top'][0] == ['Medina', 13] and st['created_at']['histogram']['kind'] == 'year'
    assert st['created_at']['min'] == '2019-01-24' and st['created_at']['max'] == '2025-06-21'
    assert [n for n, s in st.items() if s['masked']] == ['full_name', 'national_id', 'email', 'phone']
    assert st['national_id']['detected'] == 'national_id' and st['phone']['detected'] == 'phone'
    assert people['queries'] == 1

    quality = client.get(f'/api/workspaces/{ws}/quality').json()
    findings = {(f['table'].split('.')[-1].lower(), (f['column'] or '').lower(), f['rule']): f for f in quality['findings']}
    assert findings[(BIG, 'duration_s', 'high_null')]['sampled'] is True
    assert ('accounts', 'parent_account_id', 'high_null') in findings and quality['tables_profiled'] == 11
    assert not any(f['rule'] in ('no_primary_key', 'duplicate_key', 'empty_table') for f in quality['findings'])

    # the per-table "exact count" action, under the timeout
    counted = client.post(f'/api/workspaces/{ws}/tables/{qualified(engine, BIG)}/count')
    assert counted.status_code == 200 and counted.json()['row_count'] == le.BIG_ROWS, counted.text
    again = {t['id'].split('.')[-1].lower(): t for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    assert (again[BIG]['row_count'], again[BIG]['row_count_kind']) == (le.BIG_ROWS, 'exact')
    assert le.table_counts(engine)[BIG] == le.BIG_ROWS


def test_profiling_sends_only_reads_to_the_server(engine):
    """The server's own log while two tables are profiled, one of them on a sample: one read each."""
    database = open_db(engine)
    model, _ = database.read_schema()
    spec = le.ENGINES[engine]
    with le.server_statements(engine) as received:
        mark = len(database.statements)
        estimates = catalog_estimates(database, spec['schema'])
        for table_name in ('customers', BIG):
            table = model.find(name(engine, table_name))
            data = DatabaseData(database, spec['schema'], table.name)
            columns = [q.ColumnPlan.of(c.name, c.type, engine) for c in table.columns]
            result = profile_table(data, columns, estimate=estimates.get(table.name))
            data.close()
            assert result['table']['sampled'] is (table_name == BIG) and result['table']['queries'] == 1
    sent = database.statements[mark:]
    database.dispose()
    assert database.blocked == [] and estimates[name(engine, BIG)] > 1_800_000
    for statement in sent:
        check_read_only_sql(statement, dialect=engine)
    reads = [s for s in sent if is_read(s)]
    assert len(reads) == 3                                    # the estimates of the schema, and one read per table
    (sampled,) = [s for s in reads if name(engine, BIG) in s]
    assert CLAUSE[engine] in sampled and 'count(' not in sampled.lower() and 'group by' not in sampled.lower()
    # the server saw the same: one statement that touches the big table, and nothing that is not a read
    assert len([s for s in received if BIG in s.lower() and 'all_tables' not in s.lower()
                and 'information_schema' not in s.lower() and 'sys.tables' not in s.lower()]) == 1, received
    not_reads = []
    for statement in received:
        if le.is_driver_statement(engine, statement):
            continue
        try:
            check_read_only_sql(statement, dialect=engine)
        except ReadOnlyViolation:
            not_reads.append(statement)
    assert not_reads == []


def test_a_read_that_is_too_slow_fails_once_and_is_not_retried(engine):
    """A two-second timeout and a read that cannot finish: one statement, a clean "timed out", and the
    next table works. (The app's own reads never get there: they stop at a fixed number of rows.)"""
    database = open_db(engine, timeout_s=2)
    big = qualified(engine, BIG)
    data = DatabaseData(database, le.ENGINES[engine]['schema'], name(engine, BIG))
    slow = sa.text(f'SELECT COUNT(*) FROM {big} a CROSS JOIN {big} b '
                   f'WHERE a.{name(engine, "duration_s")} + b.{name(engine, "data_mb")} < -1')
    started = time.monotonic()
    with pytest.raises(QueryFailed) as failure:
        list(data.read(slow))
    assert failure.value.timeout and str(failure.value) == 'timed out after 2 s'
    assert 1.5 < time.monotonic() - started < 15 and data.queries == 1
    table = database.read_schema()[0].find(name(engine, 'plans'))
    plans = DatabaseData(database, le.ENGINES[engine]['schema'], table.name)
    copy = read_table(plans, [q.ColumnPlan.of(c.name, c.type, engine) for c in table.columns])
    assert (copy.loaded_rows, copy.row_count_kind) == (10, 'exact')
    copy.close()
    plans.close()
    database.dispose()


# ------------------------------------------------------ session 3, part A, live

@pytest.fixture
def extra_ws(engine, extras, client):
    """A workspace on the demo schema and the third schema together."""
    ws = client.post('/api/workspaces', json={'name': f'{engine} extras'}).json()['id']
    body = {**le.reader_profile(engine, schema_filter=le.extra_filter(engine)), 'password': le.READER_PASSWORD}
    added = client.post(f'/api/workspaces/{ws}/sources/db', json=body)
    assert added.status_code == 200, added.text
    return ws


def by_name(rows, key='id'):
    return {r[key].split('.')[-1].lower(): r for r in rows}


def test_a2_every_table_costs_one_read(engine, client, extra_ws, recorder):
    """The whole job, counted twice: by the app's own audit list and by the server's log."""
    ws = extra_ws
    recorder.opened = []
    with le.server_statements(engine) as received:
        job = wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    assert (job['state'], job['errors']) == ('done', []), job
    tables = by_name(client.get(f'/api/workspaces/{ws}/profile').json()['tables'])
    assert all(t['status'] == 'profiled' for t in tables.values())
    reads = recorder.reads()
    schemas = 2                                               # telecom and dx_extra: one catalog statement each
    assert len(reads) == schemas + len(tables), (len(reads), len(tables))
    for table in tables:
        hits = [r for r in reads if re.search(rf'[."`\[ ]{table}["`\] \n]', r, re.I) and not r.lower().startswith('with recursive')
                and 'all_tables' not in r.lower() and 'information_schema' not in r.lower() and 'sys.tables' not in r.lower()]
        assert len(hits) == 1, (table, hits)
        assert 'count(' not in hits[0].lower() and 'distinct' not in hits[0].lower(), hits[0]
    assert CLAUSE[engine] in next(r for r in reads if re.search(BIG, r, re.I) and 'c0' in r)
    # the server: nothing but reads, and one statement per table there too
    for table in (BIG, 'x_children', 'customers'):
        seen = [s for s in received if re.search(rf'[."`\[ ]{table}["`\] ]', s + ' ', re.I) and ' c0' in s.lower()]
        assert len(seen) == 1, (table, seen)
    assert not {s.split(None, 1)[0].upper() for s in received} & {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'ALTER',
                                                                'DROP', 'TRUNCATE', 'COMMIT', 'CALL', 'EXEC', 'DECLARE'}


def test_a2_large_and_binary_columns_are_not_fetched(engine, client, extra_ws, recorder):
    ws = extra_ws
    docs = f'{le.extra_schema(engine)}.{name(engine, "x_docs")}'
    recorder.opened = []
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [docs]}).json())
    assert job['errors'] == []
    (read,) = [r for r in recorder.reads() if ' c0' in r.lower() or ' AS c0' in r]
    # body and photo: a flag each. (PostgreSQL's TEXT is its ordinary string type and is profiled as one.)
    large_text = engine != 'postgresql'
    assert read.upper().count('CASE WHEN') == (2 if large_text else 1) and 'IS NOT NULL' in read.upper()
    st = {c['name'].lower(): c['stats'] for c in
          client.get(f'/api/workspaces/{ws}/tables/{docs}/profile?sample=false').json()['columns']}
    assert (st['body']['nulls'], st['photo']['family'], st['photo']['nulls']) == (10, 'lob', 20)
    assert st['body']['family'] == ('lob' if large_text else 'text') and st['title']['distinct'] == 30
    assert not {'min', 'max', 'top', 'distinct'} & set(st['photo'])
    assert not large_text or not {'min', 'max', 'top', 'distinct'} & set(st['body'])
    if engine == 'oracle':                                    # a LONG column: the same, with IS NOT NULL in the select list
        old = 'DX_EXTRA.X_OLD_DOCS'
        assert wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [old]}).json())['errors'] == []
        long_stats = client.get(f'/api/workspaces/{ws}/tables/{old}/profile?sample=false').json()['columns'][1]['stats']
        assert (long_stats['family'], long_stats['nulls'], long_stats['rows']) == ('lob', 1, 2)


def test_a3_a_table_without_statistics_is_read_not_counted(engine, client, extra_ws, recorder, monkeypatch):
    """x_never_counted has 150 rows and no optimizer statistics. Oracle and PostgreSQL then have no
    estimate (NULL, -1); MySQL and SQL Server always have one."""
    ws = extra_ws
    schema, table = le.extra_schema(engine), name(engine, 'x_never_counted')
    database = open_db(engine)
    estimates = catalog_estimates(database, schema)
    database.dispose()
    unknown = engine in ('oracle', 'postgresql')
    assert (table not in estimates) is unknown, estimates
    assert estimates[name(engine, 'x_children')] == 1000
    recorder.opened = []
    wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [f'{schema}.{table}']}).json())
    p = client.get(f'/api/workspaces/{ws}/tables/{schema}.{table}/profile?sample=false').json()
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled'], p['sampled'], p['queries']) == (150, 'exact', 150, False, 1)
    assert not any('count(' in r.lower() for r in recorder.reads() if table.lower() in r.lower())
    if not unknown:
        return
    # With a smaller limit the read stops before the end. The 150 rows that the read before it counted are kept
    # (a profile never throws an exact count away): 100 rows of 150, and nothing about "how many is not known".
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    recorder.opened = []
    wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [f'{schema}.{table}']}).json())
    p = client.get(f'/api/workspaces/{ws}/tables/{schema}.{table}/profile?sample=false').json()
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled'], p['sampled']) == (150, 'exact', 100, True)
    assert p['sample_method'] == 'first 100 rows, not random' and p['notes'] == []
    assert not any('count(' in r.lower() for r in recorder.reads())
    # Without that count the table is "more than 100 rows", still without a COUNT(*): an engine that HAS a catalog
    # of estimates is not counted table by table (SQLite, which has none, is - tests/test_dry_run.py).
    assert client.delete(f'/api/workspaces/{ws}/profile').status_code == 200
    recorder.opened = []
    wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [f'{schema}.{table}']}).json())
    p = client.get(f'/api/workspaces/{ws}/tables/{schema}.{table}/profile?sample=false').json()
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled'], p['sampled']) == (100, 'more_than', 100, True)
    assert p['sample_method'] == 'first 100 rows, not random' and 'how many is not known' in p['notes'][0] and len(p['notes']) == 1
    assert not any('count(' in r.lower() for r in recorder.reads())
    counts = wait_for(client, client.post(f'/api/workspaces/{ws}/profile',
                                          json={'tables': [f'{schema}.{table}'], 'counts_only': True}).json())
    assert counts['errors'] == []
    bounded = [r for r in recorder.reads() if 'count(' in r.lower()]
    assert len(bounded) == 1 and re.search(r'LIMIT|FETCH FIRST|ROWNUM|TOP', bounded[0], re.I)       # a bounded count
    counted = client.post(f'/api/workspaces/{ws}/tables/{schema}.{table}/count').json()            # on request: the full one
    assert (counted['row_count'], counted['row_count_kind']) == (150, 'exact')


def test_a3_a_partitioned_tables_estimate_is_the_sum_of_its_partitions(engine, extras):
    if engine != 'postgresql':
        pytest.skip('partitioned-table estimates are a PostgreSQL matter (reltuples is -1 on the parent)')
    with le.admin_engine(engine).connect() as admin:
        own = dict(admin.execute(text("SELECT relname, reltuples FROM pg_class WHERE relname LIKE 'x_events_p%'")).fetchall())
    assert own['x_events_p'] <= 0 and own['x_events_p_2025_01'] <= 0      # the parent, and a partition that is split again
    database = open_db(engine)
    estimates = catalog_estimates(database, 'dx_extra')
    sent = [s for s in database.statements if is_read(s)]
    database.dispose()
    assert estimates['x_events_p'] == 3000 and len(sent) == 1 and sent[0].startswith('WITH RECURSIVE')
    assert estimates['x_events_p_2025_02'] + estimates['x_events_p_2025_03'] + estimates['x_events_p_2025_01_a'] \
        + estimates['x_events_p_2025_01_b'] == 3000
    assert 'x_never_counted' not in estimates and estimates['x_parents'] == 200


def test_a1_call_records_are_recognised(engine, client, extra_ws):
    ws = extra_ws
    cdr = f'{le.extra_schema(engine)}.{name(engine, "x_cdr")}'
    assert wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [cdr]}).json())['errors'] == []
    p = client.get(f'/api/workspaces/{ws}/tables/{cdr}/profile').json()
    columns = {c['name'].lower(): c for c in p['columns']}
    assert {n: c['personal'] for n, c in columns.items()} == {
        'call_id': False, 'a_number': True, 'b_number': True, 'imsi': True, 'call_ts': False, 'duration_s': False,
        'contact': True}
    assert columns['a_number']['personal_reason'] == 'Looks like phone number'
    assert columns['imsi']['personal_reason'] == 'Looks like subscriber / SIM / device identifier'
    st = {n: c['stats'] for n, c in columns.items()}
    assert st['call_ts']['detected'] is None and st['call_ts']['min'] == 1_700_000_977       # epoch seconds, not IDs
    assert st['contact']['detected'] == 'phone' and st['contact']['nulls'] == 75             # landlines, by their values
    assert st['b_number']['min'].startswith('+9') and '*' in st['b_number']['min'] and st['imsi']['masked']
    first = dict(zip([c.lower() for c in p['sample']['columns']], p['sample']['rows'][0]))
    # whichever row the server gives first (Oracle keeps no order: after dx_extra was rebuilt it was call 206)
    number = first['a_number']
    assert len(number) == 12 and number[:2] == '96' and number[2:10] == '*' * 8 and number[10:].isdigit()
    assert 1_700_000_977 <= first['call_ts'] <= 1_700_000_000 + 300 * 977 and '*' in first['imsi']




# ------------------------------------------------------ session 3, part B, live

ROWS = le.extra_rows()
CHILDREN, ITEMS = ROWS['x_children'][1], ROWS['x_pair_items'][1]
KEYS = len({r[1] for r in CHILDREN if r[1] is not None})
ORPHAN_KEYS = len({r[1] for r in CHILDREN if r[1] is not None and r[1] > 200})
ORPHAN_ROWS = sum(1 for r in CHILDREN if r[1] is not None and r[1] > 200)
ITEM_ORPHANS = sum(1 for r in ITEMS if r[1] == 11)
ITEM_EMPTY = sum(1 for r in ITEMS if r[1] is None)
MANY = b'SELECT c.child_id, p.label FROM x_children c JOIN x_parents p ON p.grp = c.grp;'


def rel_of(view, child, columns, parent):
    return next(r for r in view['relationships'] if r['from'].split('.')[-1].lower() == child
                and [c.lower() for c in r['from_columns']] == columns and r['to'].split('.')[-1].lower() == parent)


def test_b_relationship_checks_on_a_live_database(engine, client, extra_ws, recorder, monkeypatch):
    """Coverage, orphans, null share and real cardinality, measured through the guarded connection - by reading the
    child's keys and looking them up, the method for what the database does not count itself (see part D below)."""
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', False)
    ws = extra_ws
    assert client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('many.sql', MANY, 'text/plain'))]).status_code == 200
    before = le.extra_state(engine)
    recorder.opened = []
    with le.server_statements(engine) as received:
        job = wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'suggest': True}).json())
    assert (job['kind'], job['state'], job['errors']) == ('relations', 'done', []), job
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    assert view['summary']['not_checked'] == 0 and view['summary']['checked'] == len(view['relationships']) >= 20

    # found from the names; 5% empty, 19 of the 209 values used are not in the parent
    c = rel_of(view, 'x_children', ['parent_id'], 'x_parents')['check']
    assert (c['rows'], c['null_rows'], c['keys_total'], c['orphan_keys'], c['orphan_rows']) == (1000, 50, KEYS, ORPHAN_KEYS, ORPHAN_ROWS)
    assert c['coverage'] == pytest.approx(1 - ORPHAN_ROWS / 950) and c['label'] == 'measured on all 1,000 rows'
    assert {e['key'][0] for e in c['orphan_examples']} == {r[1] for r in CHILDREN if r[1] and r[1] > 200}
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique_how']) == ('1-N', True, 'unique: primary key in the catalog')
    assert c['statements'] == 2                                 # the child's read, and one bound list of 209 keys

    # a declared two-column foreign key that the database never validated: it has orphans
    pair = rel_of(view, 'x_pair_items', ['a', 'b'], 'x_pairs')
    assert pair['kind'] == 'declared' and pair['name'].lower() == 'fk_x_pair_items_pair'
    c = pair['check']
    assert (c['rows'], c['null_rows'], c['orphan_rows'], c['orphan_keys']) == (500, ITEM_EMPTY, ITEM_ORPHANS, 10)
    assert all(e['key'][0] == 11 for e in c['orphan_examples']) and c['cardinality'] == '1-N'

    one = rel_of(view, 'x_profiles', ['parent_id'], 'x_parents')['check']      # the key of the child is its foreign key
    assert (one['cardinality'], one['cardinality_proven'], one['coverage'], one['orphan_rows']) == ('1-1', True, 1.0, 0)
    assert 'primary key on the child, in the catalog' in one['cardinality_note']

    many = next(r for r in view['relationships'] if [c.lower() for c in r['from_columns']] == ['grp'])['check']
    assert (many['cardinality'], many['parent_unique'], many['coverage']) == ('N-N', False, 1.0)

    # the 2,000,000-row table: its key column on a sample, 150 keys looked up in one statement
    big = rel_of(view, BIG, ['subscription_id'], 'subscriptions')
    c = big['check']
    assert big['kind'] == 'names' and c['sampled'] is True and c['sample_method'] == SAMPLING[engine]
    assert c['label'] == f'measured on a sample of {c["rows"]:,} rows' and 25_000 < c['rows'] <= 200_000
    assert (c['keys_total'], c['coverage'], c['orphan_rows'], c['cardinality'], c['statements']) == (150, 1.0, 0, '1-N', 2)
    assert c['elapsed_s'] < 30

    # a link nobody declared and no name gave away: parent_code -> x_parents.code (unique by a constraint)
    (s,) = [x for x in view['suggestions'] if x['from_columns'][0].lower() == 'parent_code']
    assert (s['to'].split('.')[-1].lower(), s['to_columns'][0].lower(), s['cardinality']) == ('x_parents', 'code', '1-N')
    assert s['confidence'] == pytest.approx(190 / 209, abs=1e-4) and s['evidence']['parent_unique_proven'] is True
    assert 'unique' in s['evidence']['parent_key'] and 'in the catalog' in s['evidence']['parent_key']

    # what was sent: reads only, every one through the guard, values always bound, lists of 500 at most
    assert all(db.blocked == [] for db in recorder.opened)
    reads = recorder.reads()
    for database in recorder.opened:
        for statement in database.statements:
            check_read_only_sql(statement, dialect=engine)
    lookups = [r for r in reads if re.search(r'\bIN \(', r) and 'count(*)' in r.lower()]
    assert lookups and max(len(re.findall(r'k_\d+', r)) for r in lookups) <= 500
    assert not any(re.search(r"'P0\d\d\d'|'9665", r) for r in reads)
    big_reads = [r for r in reads if re.search(BIG, r, re.I) and ' c0' in r.lower()]
    # TWO reads of the big table for the whole job, both sampled. The first look for links by values: about 2,000
    # rows of the columns that could point somewhere (not its own key, not the column that points already). Then
    # ONE read for everything that is measured: its key column and the columns of the pairs that passed the look.
    # On Oracle the look takes whole blocks (SAMPLE BLOCK: only those blocks are read). Blocks can come back with
    # few rows: then the look reads the table again the way the full check does, once, and the job says so.
    twice = [n for n in job['notes'] if n.startswith('Read twice for the first look: ')]
    again = engine == 'oracle' and len(big_reads) == 3
    assert len(big_reads) == (3 if again else 2), big_reads
    look, read = big_reads[0], big_reads[-1]
    assert 'subscription_id' not in look.lower() and 'event_id' not in look.lower() and look.lower().count(' as c') == 5
    assert CLAUSE[engine] in read and 'subscription_id' in read.lower()
    assert read.lower().count(' as c') <= 4 and 'event_id' not in read.lower()
    if engine == 'oracle':
        assert ' SAMPLE BLOCK (' in look and ' SAMPLE BLOCK (' not in read
        assert bool(twice and name(engine, BIG) in twice[0]) is again
        if again:                                               # the same look, by rows: SAMPLE (p) with the look's p
            assert ' SAMPLE BLOCK (' not in big_reads[1] and big_reads[1].split(' SAMPLE (')[0] == look.split(' SAMPLE BLOCK (')[0]
            assert big_reads[1].split(' SAMPLE (')[1].split(')')[0] == look.split(' SAMPLE BLOCK (')[1].split(')')[0]
    if engine != 'mysql':                                       # (MySQL reads the first rows; how many is a bound value)
        def percent(statement):
            return float(re.search(r'SAMPLE (?:SYSTEM |BLOCK )?\((\d+(?:\.\d+)?)', statement).group(1))
        assert percent(look) == pytest.approx(percent(read) / 50)      # 2,000 rows, where the read takes 100,000
    words = {s.split(None, 1)[0].upper() for s in received}
    assert not words & {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'COMMIT', 'CALL',
                        'EXEC', 'DECLARE', 'LOCK'}, words
    assert le.extra_state(engine) == before and le.open_locks(engine) == []


def test_b_an_accepted_link_is_checked_like_the_others(engine, client, extra_ws):
    ws = extra_ws
    wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': [], 'suggest': True}).json())
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    (s,) = [x for x in view['suggestions'] if x['from_columns'][0].lower() == 'parent_code']
    accepted = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': s['key'], 'action': 'accept'})
    assert accepted.status_code == 200
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['stats']['values'] == 1
    # accepting it measured it at once - and "Check selected" measures it again like any other
    assert wait_for(client, accepted.json()['job'])['state'] == 'done'
    first = next(r for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships'] if r['kind'] == 'values')
    assert (first['check']['status'], first['check']['orphan_keys']) == ('checked', ORPHAN_KEYS)
    wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': [s['key']]}).json())
    c = next(r for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships'] if r['kind'] == 'values')['check']
    assert (c['orphan_keys'], c['null_rows'], c['cardinality']) == (ORPHAN_KEYS, 50, '1-N')
    assert (c['method'], c['exact'], c['rows'], c['orphan_rows']) == ('database', True, 1000, ORPHAN_ROWS)      # text = text
    assert 'in the catalog' in c['parent_unique_how'] and 'unique' in c['parent_unique_how']
    assert sorted(e['key'][0] for e in c['orphan_examples'])[0] == 'P0202'              # codes are not personal: shown
    drawio = client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=values').text
    assert 'dashPattern=1 3' in drawio


def test_b_a_slow_parent_gives_not_checked_timeout(engine, client, extra_ws, monkeypatch):
    """A one-second budget and a look-up that cannot finish in it: "not checked (timeout)", once."""
    from app.analysis import relations as relations_module
    ws = extra_ws
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', False)
    monkeypatch.setattr(config, 'RELATION_CHECK_BUDGET_S', 1)
    big = qualified(engine, BIG)
    slow = sa.text(f'SELECT COUNT(*) FROM {big} a CROSS JOIN {big} b WHERE a.{name(engine, "duration_s")} '
                   f'+ b.{name(engine, "data_mb")} < -1')
    asked = []

    def lookup(self, columns, keys, check):
        asked.append(self.table)
        self.conn.run(slow)                                    # stands in for a parent with no index on its key
        return {}
    monkeypatch.setattr(relations_module.DatabaseParent, 'lookup', lookup)
    key = next(r['key'] for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships']
               if r['from'].split('.')[-1].lower() == 'x_children')
    started = time.monotonic()
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': [key]}).json())
    assert job['state'] == 'done' and time.monotonic() - started < 20
    assert job['errors'] == [{'table': job['errors'][0]['table'], 'message': 'not checked (timeout)', 'timeout': True}]
    rel = next(r for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships'] if r['key'] == key)
    assert rel['check']['label'] == 'not checked (timeout)' and len(asked) == 1


# ------------------------------------------- the fix round: Oracle samples rows, or blocks

def test_c_oracle_samples_blocks_above_the_size_limit_and_says_so(engine, client, live_ws, recorder, monkeypatch):
    """The big table is about 110 MB by the catalog. With the limit at its default of 1 GB it is sampled
    by rows (the tests above). With the limit under its size it is sampled by blocks - and every place
    that shows the sample says what a block sample is: the profile, a relationship's evidence, the report."""
    if engine != 'oracle':
        pytest.skip("Oracle's SAMPLE BLOCK")
    from app import config
    from app.profiling import queries
    from app.profiling.profiler import catalog_estimates
    ws, big = live_ws, qualified(engine, BIG)
    database = open_db(engine)
    try:
        found = catalog_estimates(database, le.ENGINES[engine]['schema'])
    finally:
        database.dispose()
    size = found.sizes[name(engine, BIG)]
    assert found[name(engine, BIG)] == le.BIG_ROWS and 80 * 1024 ** 2 < size < 300 * 1024 ** 2, size      # blocks x 8 KB
    assert set(found.sizes) == set(found)                         # every analysed table has a size, from the same statement
    monkeypatch.setattr(config, 'ORACLE_ROW_SAMPLE_MAX_BYTES', 50 * 1024 * 1024)
    recorder.opened = []
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [big]}).json())
    assert (job['state'], job['errors']) == ('done', [])
    reads = [r for r in recorder.reads() if re.search(BIG, r, re.I) and ' c0' in r.lower()]
    assert 1 <= len(reads) <= 2 and all(' SAMPLE BLOCK (' in r for r in reads)       # one read; one more if it came back nearly empty
    p = client.get(f'/api/workspaces/{ws}/tables/{big}/profile?sample=false').json()
    assert p['sampled'] and p['sample_method'].startswith('SAMPLE BLOCK (') and p['sample_method'].endswith(queries.BLOCK_SAMPLE)
    assert any('more than the 50 MB up to which rows are sampled (DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB)' in n for n in p['notes'])
    wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    c = rel_of(view, BIG, ['subscription_id'], 'subscriptions')['check']
    # the database counted the 2,000,000 rows itself; how often a key repeats was read from the block sample
    assert (c['method'], c['sampled'], c['rows'], c['cardinality_on_sample']) == ('database', False, le.BIG_ROWS, True)
    assert c['child_sample_method'].endswith(queries.BLOCK_SAMPLE)
    page = client.get(f'/api/workspaces/{ws}/report?format=html')
    assert page.status_code == 200 and page.text.count(queries.BLOCK_SAMPLE) >= 4      # the rule, the table (twice), the check
    # back at the default limit the same table is sampled by rows again
    monkeypatch.undo()
    monkeypatch.setattr(service_module, 'ReadOnlyDatabase', Recorder)
    wait_for(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': [big]}).json())
    p = client.get(f'/api/workspaces/{ws}/tables/{big}/profile?sample=false').json()
    assert p['sample_method'] == SAMPLING[engine] and p['notes'] == [] and 95_000 < p['rows_profiled'] < 105_000


# ------------------------------------------- the dry-run round, part A: counted by the database itself

DAYS, SHIFTS, BOOKINGS = ROWS['x_days'][1], ROWS['x_shifts'][1], ROWS['x_bookings'][1]
SLOTS, VISITS = ROWS['x_slots'][1], ROWS['x_visits'][1]
# how each engine names the two kinds of date key (app.sources.database.type_name)
DAY_TYPE = {'oracle': 'DATE', 'mssql': 'DATE', 'postgresql': 'DATE', 'mysql': 'DATE'}
MOMENT_TYPE = {'oracle': 'DATE', 'mssql': 'DATETIME2', 'postgresql': 'TIMESTAMP WITHOUT TIME ZONE', 'mysql': 'DATETIME'}


def test_d_the_database_counts_a_relationship_over_the_whole_table(engine, client, extra_ws, recorder):
    """One anti-join statement per relationship, through the guarded connection as the read-only account: the rows,
    the empty keys and the orphan rows of the WHOLE child table - also of the 2,000,000-row table, which was
    measured on a sample of about 100,000 rows before."""
    ws = extra_ws
    before = le.extra_state(engine)
    recorder.opened = []
    started = time.monotonic()
    with le.server_statements(engine) as received:
        job = wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    seconds = time.monotonic() - started
    assert (job['kind'], job['state'], job['errors']) == ('relations', 'done', []), job
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    summary = view['summary']
    assert summary['not_checked'] == 0 and summary['checked'] == len(view['relationships']) >= 23
    assert (summary['sampled'], summary['not_exact']) == (0, 0)
    assert {r['check']['method'] for r in view['relationships']} == {'database'}

    c = rel_of(view, 'x_children', ['parent_id'], 'x_parents')['check']
    assert (c['rows'], c['null_rows'], c['orphan_keys'], c['orphan_rows'], c['exact']) == (1000, 50, ORPHAN_KEYS, ORPHAN_ROWS, True)
    assert c['coverage'] == pytest.approx(1 - ORPHAN_ROWS / 950) and c['label'] == 'measured on all 1,000 rows'
    assert {e['key'][0] for e in c['orphan_examples']} == {r[1] for r in CHILDREN if r[1] and r[1] > 200}
    assert [e['rows'] for e in c['orphan_examples']] == sorted((e['rows'] for e in c['orphan_examples']), reverse=True)
    assert (c['cardinality'], c['cardinality_proven'], c['parent_unique_how']) == ('1-N', True, 'unique: primary key in the catalog')
    assert c['statements'] == 3 and c['shown']['orphans'] == str(ORPHAN_ROWS)      # the child's read, the count, the examples

    pair = rel_of(view, 'x_pair_items', ['a', 'b'], 'x_pairs')['check']            # two columns; an empty a is no orphan
    assert (pair['rows'], pair['null_rows'], pair['orphan_rows'], pair['orphan_keys']) == (500, ITEM_EMPTY, ITEM_ORPHANS, 10)
    assert all(e['key'][0] == 11 for e in pair['orphan_examples']) and len(pair['orphan_examples']) == 10
    one = rel_of(view, 'x_profiles', ['parent_id'], 'x_parents')['check']
    assert (one['cardinality'], one['coverage'], one['orphan_rows'], one['statements']) == ('1-1', 1.0, 0, 2)

    # the big table: every one of its 2,000,000 rows, exactly - where a sample said "about"
    big = rel_of(view, BIG, ['subscription_id'], 'subscriptions')['check']
    assert (big['method'], big['rows'], big['null_rows'], big['orphan_rows'], big['coverage']) == ('database', le.BIG_ROWS, 0, 0, 1.0)
    assert (big['sampled'], big['exact'], big['label']) == (False, True, 'measured on all 2,000,000 rows')
    assert big['cardinality_on_sample'] is True and big['child_sample_method'] == SAMPLING[engine] and big['cardinality'] == '1-N'
    assert big['elapsed_s'] < 30 and seconds < 120, (big['elapsed_s'], seconds)

    # what was sent: an anti-join per relationship, nothing looked up, every statement a read that passed the guard
    assert all(db.blocked == [] for db in recorder.opened)
    reads = recorder.reads()
    for database in recorder.opened:
        for statement in database.statements:
            check_read_only_sql(statement, dialect=engine)
    counts = [r for r in reads if 'NOT (EXISTS' in r and ' JOIN ' in r and ' a.n' in r]
    assert len(counts) == len(view['relationships'])
    assert not [r for r in reads if re.search(r'\bIN \(', r) and 'count(*)' in r.lower() and 'NOT (EXISTS' not in r]
    assert not any("'" in r for r in counts)
    seen = [s for s in received if 'NOT (EXISTS' in s.upper() or 'NOT EXISTS' in s.upper()]
    assert len(seen) >= len(view['relationships'])                 # the server saw them too
    words = {s.split(None, 1)[0].upper() for s in received}
    assert not words & {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'CREATE', 'ALTER', 'DROP', 'TRUNCATE', 'COMMIT', 'CALL',
                        'EXEC', 'DECLARE', 'LOCK'}, words
    assert le.extra_state(engine) == before and le.open_locks(engine) == []
    print(f'\n{engine}: Check all, {len(view["relationships"])} relationships in {seconds:.1f} s; '
          f'{BIG}: {big["elapsed_s"]} s for {le.BIG_ROWS:,} rows')


@pytest.mark.parametrize('whole_table', [True, False], ids=['counted in the database', 'looked up'])
def test_d_date_keys_match(engine, client, extra_ws, recorder, monkeypatch, whole_table):
    """A key that is a day (DATE) and one that is a moment (Oracle DATE, SQL Server datetime2, PostgreSQL timestamp,
    MySQL datetime), each with a child whose rows all match and one with real orphans. The dry run's problem 1 was
    this on SQLite: a day looked up as a timestamp found nothing. Both ways of measuring must agree with the rows."""
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', whole_table)
    ws = extra_ws
    model = client.get(f'/api/workspaces/{ws}/tables/{le.extra_schema(engine)}.{name(engine, "x_days")}').json()
    assert [c['type'] for c in model['columns'] if c['name'].lower() == 'the_day'] == [DAY_TYPE[engine]]
    model = client.get(f'/api/workspaces/{ws}/tables/{le.extra_schema(engine)}.{name(engine, "x_slots")}').json()
    assert [c['type'] for c in model['columns'] if c['name'].lower() == 'starts_at'] == [MOMENT_TYPE[engine]]
    keys = [r['key'] for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships']
            if r['from'].split('.')[-1].lower() in ('x_shifts', 'x_bookings', 'x_visits')]
    assert len(keys) == 3
    recorder.opened = []
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': keys}).json())
    assert (job['state'], job['errors']) == ('done', []), job
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    method = 'database' if whole_table else 'lookup'

    full = rel_of(view, 'x_shifts', ['the_day'], 'x_days')['check']             # every child row matches
    assert (full['method'], full['rows'], full['null_rows'], full['orphan_rows'], full['coverage']) == (method, 60, 0, 0, 1.0)
    assert (full['cardinality'], full['exact'], full['shown']['coverage']) == ('1-N', True, '100%')

    some = rel_of(view, 'x_bookings', ['the_day'], 'x_days')['check']           # real orphans, and empty keys
    assert (some['method'], some['rows'], some['null_rows'], some['orphan_rows'], some['orphan_keys']) == (method, 50, 5, 9, 3)
    assert some['coverage'] == pytest.approx(36 / 45) and some['shown']['coverage'] == '80%'
    assert [(e['key'][0], e['rows']) for e in some['orphan_examples']] == [('2025-02-01', 4), ('2025-02-02', 3), ('2024-12-31', 2)]

    moments = rel_of(view, 'x_visits', ['starts_at'], 'x_slots')['check']
    assert (moments['method'], moments['rows'], moments['null_rows'], moments['orphan_rows'], moments['orphan_keys']) == (method, 46, 0, 6, 2)
    assert [(e['key'][0], e['rows']) for e in moments['orphan_examples']] == [('2025-03-01 19:30:00', 4), ('2025-03-01', 2)]
    assert moments['coverage'] == pytest.approx(40 / 46)

    reads = recorder.reads()
    lookups = [r for r in reads if re.search(r'\bIN \(', r) and 'count(*)' in r.lower()]
    assert bool(lookups) is not whole_table
    assert not any(re.search(r"'20\d\d-", r) for r in reads)                   # a date is bound, never written into the text
    for database in recorder.opened:
        assert database.blocked == []
        for statement in database.statements:
            check_read_only_sql(statement, dialect=engine)


def test_d_a_count_that_times_out_falls_back_to_the_lookups(engine, client, extra_ws, monkeypatch):
    """A two-second statement timeout and a count that cannot finish in it: the server stops the statement, the keys
    are looked up as before, and the result says why it is not the database's own count."""
    ws = extra_ws
    monkeypatch.setattr(config, 'STATEMENT_TIMEOUT_S', 2)
    big = qualified(engine, BIG)
    slow = sa.text(f'SELECT COUNT(*), 0, 0, 0 FROM {big} a CROSS JOIN {big} b WHERE a.{name(engine, "duration_s")} '
                   f'+ b.{name(engine, "data_mb")} < -1')
    asked = []

    def count(dialect, child, columns, parent, parent_columns):
        asked.append(child[1])
        return slow
    monkeypatch.setattr(q, 'orphan_count_query', count)
    keys = [r['key'] for r in client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships']
            if r['from'].split('.')[-1].lower() == 'x_pair_items' or r['to'].split('.')[-1].lower() == 'x_days']
    assert len(keys) == 3
    started = time.monotonic()
    job = wait_for(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': keys}).json())
    assert (job['state'], job['errors']) == ('done', []) and time.monotonic() - started < 60, job
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    pair = rel_of(view, 'x_pair_items', ['a', 'b'], 'x_pairs')['check']
    assert (pair['method'], pair['not_whole']) == ('lookup', 'the count over the whole table ran out of time')
    assert (pair['rows'], pair['null_rows'], pair['orphan_rows'], pair['orphan_keys'], pair['exact']) == (500, ITEM_EMPTY, ITEM_ORPHANS, 10, True)
    some = rel_of(view, 'x_bookings', ['the_day'], 'x_days')['check']
    assert (some['method'], some['orphan_rows'], some['orphan_keys']) == ('lookup', 9, 3)
    assert sorted(a.lower() for a in asked) == ['x_bookings', 'x_pair_items', 'x_shifts']     # each child table: asked once
    assert le.open_locks(engine) == []
