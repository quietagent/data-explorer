"""The SQL tab on real servers, as an account that CAN write: root, sa, system, postgres.

The point of running as such an account: nothing but the app stands between a hostile statement
and the data. If the guard let one through, these servers would run it. So the tests show, on
Oracle, SQL Server, PostgreSQL and MySQL in the local Docker containers:

  * the account really can write (a write on the driver's own connection succeeds, and is rolled back)
  * every statement of the hostile battery (tests/hostile.py) is refused by the guard - with the
    guard's own message, before a connection is even opened
  * the data, the sequence and the list of tables are the same afterwards, and no lock is left
  * reads that hold the same words run and give rows (the guard does not just refuse words)

and, as the read-only account, that a result column is masked by the table it comes from: the name
of a thing is shown, a person's name is masked, a mark is obeyed, AVG(salary) is masked without an
alias, a UNION that carries a personal column is masked, COUNT(salary) stays visible.

    scripts\\live_db.ps1 start     and then     pytest tests\\test_live_sql.py
"""

import re
import time

import pytest
from sqlalchemy import text

import hostile
from app import sqlguard
from app.analysis import sqlrunner
from app.profiling.personal import mask
from app.sources.database import ReadOnlyDatabase
from scripts import live_engines as le

pytestmark = pytest.mark.live
HOSTILE_WORDS = re.compile(r'hostile|x_touch|x_seq\b|x_audit|dx_hostile|sleep|dblink|openrowset|openquery|waitfor|'
                           r'for update|for share|updlock|xlock|holdlock|tablock|into outfile|nextval|'
                           r'x_ping|x_pkg|x_peek|x_calls', re.I)


@pytest.fixture(scope='module', params=list(le.ENGINES))
def engine(request):
    if not le.reachable(request.param):
        pytest.skip(f'{request.param}: test container not running or not seeded (scripts\\live_db.ps1 up)')
    le.ensure_extras(request.param)
    return request.param


class Recorder(ReadOnlyDatabase):
    """Every connection object the runner makes: a refused statement must not make one."""
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Recorder.opened.append(self)


@pytest.fixture
def recorder(monkeypatch):
    Recorder.opened = []
    monkeypatch.setattr(sqlrunner, 'ReadOnlyDatabase', Recorder)
    return Recorder


@pytest.fixture
def admin_ws(engine, client):
    """A workspace whose database source logs in as the admin account. Returns (workspace, source id)."""
    spec = le.ENGINES[engine]
    ws = client.post('/api/workspaces', json={'name': f'{engine} as {spec["admin_user"]}'}).json()['id']
    body = {**le.reader_profile(engine, username=spec['admin_user'], schema_filter=le.extra_filter(engine)),
            'password': le.ADMIN_PASSWORD}
    added = client.post(f'/api/workspaces/{ws}/sources/db', json=body)
    assert added.status_code == 200, added.text
    assert added.json()['settings']['username'] == spec['admin_user'] and added.json()['tables'] >= 20
    return ws, added.json()['id']


def run(client, ws, source, sql):
    return client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': sql})


def telecom_state(engine):
    return le.table_counts(engine)


def test_the_account_can_write(engine):
    """Without the app in between, this account changes data: the battery below is not refused by the server."""
    x = le.extra_schema(engine)
    eng = le.admin_engine(engine)
    with eng.connect() as conn:
        changed = conn.execute(text(f"UPDATE {x}.x_parents SET label = 'changed'")).rowcount
        made = conn.execute(text(f"INSERT INTO {x}.x_audit (id, note) VALUES (1, 'a write')")).rowcount
        conn.rollback()                                           # nothing of it stays
    eng.dispose()
    assert (changed, made) == (200, 1)
    assert le.extra_state(engine)['x_audit'] == 0


def test_oracle_a_function_writes_from_inside_a_read_only_transaction(engine):
    """Why functions are allow-listed: x_touch() writes through an autonomous transaction, and a
    read-only transaction - the app's own protection on Oracle - does not stop it."""
    if engine != 'oracle':
        pytest.skip("Oracle's autonomous transactions")
    eng = le.admin_engine(engine)
    try:
        with eng.connect() as conn:
            raw = conn.connection.dbapi_connection
            cursor = raw.cursor()
            cursor.execute('SET TRANSACTION READ ONLY')
            with pytest.raises(Exception, match='ORA-01456'):         # a plain write is refused there ...
                cursor.execute("INSERT INTO dx_extra.x_audit (id, note) VALUES (2, 'plain')")
            cursor.execute('SELECT x_touch FROM dual')                # ... a function, called without brackets, is not
            assert cursor.fetchall() == [(1,)]
            cursor.close()
            raw.rollback()
        assert le.extra_state(engine)['x_audit'] == 1                 # it wrote, and committed, by itself
    finally:
        with eng.connect() as conn:
            conn.execute(text('DELETE FROM dx_extra.x_audit'))
            conn.commit()
        eng.dispose()
    assert le.extra_state(engine)['x_audit'] == 0


def test_calls_without_brackets_run_on_the_server_and_are_refused_by_the_app(engine, client, recorder):
    """The fix round after session 3's audit. Oracle calls a function that takes no argument without
    brackets; PostgreSQL calls f(t) for t.f. First the proof that the server really does it, on the raw
    driver as the READ-ONLY account (scripts/call_proof.py): the functions leave a row in x_calls. Then
    the same statements through the SQL tab as that account: refused, and x_calls stays empty."""
    if engine not in hostile.NO_BRACKET_CALLS:
        pytest.skip('a call needs its brackets on this engine')
    from scripts import call_proof
    spec = le.ENGINES[engine]
    shapes = dict((what.split()[0], sql) for what, sql in hostile.calls(engine, le.extra_schema(engine), spec['schema'], proof=True))
    call_proof.logged(engine)
    # 1. on the raw driver the server runs them
    runs = {'oracle': ['1', '2', '3', '4', '5', '8', '9', '10', '11', '26', '31'],
            'postgresql': ['1', '2', '3', '4', '6', '7', '9', '14', '15', '16']}[engine]
    for shape in runs:
        answer, calls = call_proof.on_server(engine, shapes[shape], read_only=engine == 'oracle')
        assert answer.startswith('ran') and calls, (shape, shapes[shape], answer)
    if engine == 'oracle':
        # an alias called like a schema or a package, and a real alias in front of a function name: Oracle refuses
        for shape in ('6', '7', '7b', '29'):
            answer, calls = call_proof.on_server(engine, shapes[shape])
            assert 'ORA-00904' in answer and not calls, (shape, answer)
    else:
        # in the read-only transaction the app uses, the function is called too - and stopped at its INSERT
        answer, calls = call_proof.on_server(engine, shapes['1'])
        assert 'cannot execute INSERT in a read-only transaction' in answer and not calls
    # 2. the source as the app reads it, as the read-only account: on Oracle with what the database can call
    ws = client.post('/api/workspaces', json={'name': f'{engine} calls'}).json()['id']
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={
        **le.reader_profile(engine, schema_filter=le.extra_filter(engine)), 'password': le.READER_PASSWORD})
    assert added.status_code == 200, added.text
    source = added.json()['id']
    listed = client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]
    assert listed['blocked'] is None
    if engine == 'oracle':
        catalog = client.app.state.workspaces.source_rows(ws)[0]['model'].catalog
        assert catalog['error'] is None and {'X_PING', 'STATUS', 'ONE', 'X_PKG', 'DBMS_RANDOM'} <= set(catalog['callable'])
        assert 'X_TOUCH' not in catalog['callable']               # this account may not execute it: it cannot call it
        assert {'DX_EXTRA', 'TELECOM', 'SYS'} <= set(catalog['schemas']) and 'X_PARENTS' not in catalog['callable']
    # 3. through the SQL tab: every one of them is refused before a connection is made
    recorder.opened = []
    for shape, sql in shapes.items():
        response = run(client, ws, source, sql)
        if shape == '29' and engine == 'oracle':                  # not a call: the guard lets it through, Oracle says ORA-00904
            assert response.status_code == 400 and 'ORA-00904' in response.json()['detail']
            continue
        assert response.status_code == 400 and response.json()['detail'].startswith('Blocked by the read-only guard: '), (shape, sql)
    assert len(recorder.opened) == (1 if engine == 'oracle' else 0)
    assert call_proof.logged(engine) == {}                        # not one call
    # 4. the ordinary way to write these runs
    fine = {'oracle': f'SELECT s.status, COUNT(*) AS n FROM {spec["schema"]}.subscriptions s GROUP BY s.status ORDER BY n DESC',
            'postgresql': f'SELECT p.label, (p).code FROM {le.extra_schema(engine)}.x_parents p ORDER BY p.parent_id LIMIT 3'}[engine]
    result = run(client, ws, source, fine)
    assert result.status_code == 200 and result.json()['row_count'] >= 1, result.text
    assert call_proof.logged(engine) == {}


def test_the_hostile_battery_is_refused_by_the_guard(engine, client, admin_ws, recorder, capsys):
    ws, source = admin_ws
    spec = le.ENGINES[engine]
    x = le.extra_schema(engine)
    before, telecom_before = le.extra_state(engine), telecom_state(engine)
    statements = hostile.battery(engine, x, spec['schema'])
    assert before[le.CALL_LOG] == 0                               # the log of the functions that are called without brackets
    recorder.opened = []
    refused = {}
    with le.server_statements(engine, user=spec['admin_user']) as received:
        for group, sql in statements:
            response = run(client, ws, source, sql)
            assert response.status_code == 400, (group, sql, response.text)
            detail = response.json()['detail']
            assert detail.startswith('Blocked by the read-only guard: '), (group, sql, detail)
            refused[group] = refused.get(group, 0) + 1
        # 1. refused by the guard itself: not one connection object was made, so nothing could be sent
        assert recorder.opened == []
        time.sleep(0.5)
    # 2. the server's own log of this account: nothing of the battery arrived
    arrived = [s for s in received if HOSTILE_WORDS.search(s) and 'general_log' not in s and 'dx_audit' not in s
               and 'v$sql' not in s.lower()]
    assert arrived == [], arrived
    # 3. nothing changed: rows, the sequence's position, the list of tables, the demo schema - and no function
    #    that can be called without brackets was called (each call leaves a row in x_calls, whatever the transaction)
    assert le.extra_state(engine) == before and telecom_state(engine) == telecom_before
    assert before['x_audit'] == 0 and 'x_hostile' not in before['tables'] and le.extra_state(engine)[le.CALL_LOG] == 0
    # 4. no lock is held and no transaction is left open
    assert le.open_locks(engine) == []
    assert sum(refused.values()) == len(statements) >= 60
    history = client.get(f'/api/workspaces/{ws}/sql').json()['history']
    assert history and all(h['outcome'] == 'blocked' for h in history)
    with capsys.disabled():
        print(f'\n  {engine} as {spec["admin_user"]}: {len(statements)} hostile statements refused by the guard, 0 sent - '
              + ', '.join(f'{group} {refused[group]}' for group in hostile.GROUPS if group in refused))


def test_reads_that_hold_the_same_words_run(engine, client, admin_ws, recorder):
    """The other half: the guard does not refuse words. These run, as the same account, and give rows."""
    ws, source = admin_ws
    x = le.extra_schema(engine)
    before = le.extra_state(engine)
    for sql in hostile.allowed(engine, x):
        response = run(client, ws, source, sql)
        assert response.status_code == 200, (sql, response.text)
        assert response.json()['row_count'] >= 1, sql
    counted = run(client, ws, source, hostile.allowed(engine, x)[0]).json()
    assert counted['rows'] == [[200]] and [c['name'].lower() for c in counted['columns']] == ['n']
    # each of them was one statement on a guarded connection of its own, in a read-only transaction where there is one
    assert len(recorder.opened) == len(hostile.allowed(engine, x)) + 1
    for database in recorder.opened:
        own = [s for s in database.statements if not s.upper().startswith('SET ')]
        assert len(own) == 1 and database.blocked == []
        if engine != 'mssql':
            assert any('READ ONLY' in s.upper() for s in database.statements)
    assert le.extra_state(engine) == before and le.open_locks(engine) == []


def test_a_thousand_rows_and_a_timeout_on_the_big_table(engine, client, admin_ws, monkeypatch):
    ws, source = admin_ws
    spec = le.ENGINES[engine]
    big = f'{spec["schema"]}.{le.BIG_TABLE}'
    started = time.monotonic()
    result = run(client, ws, source, f'SELECT event_id, msisdn, event_type FROM {big}')       # 2,000,000 rows
    assert result.status_code == 200, result.text
    r = result.json()
    assert (r['row_count'], r['more'], len(r['rows'])) == (1000, True, 1000) and time.monotonic() - started < 25
    assert r['personal'] == [next(c['name'] for c in r['columns'] if c['name'].lower() == 'msisdn')]      # masked by its name
    assert all('*' in row[1] for row in r['rows'])
    exported = client.get(f'/api/workspaces/{ws}/sql/results/{r["id"]}.csv')
    assert exported.status_code == 200 and exported.text.count('\n') == 1001 and '96********' in exported.text
    from app import config
    monkeypatch.setattr(config, 'SQL_TIMEOUT_S', 2)
    slow = run(client, ws, source, f'SELECT COUNT(*) FROM {big} a CROSS JOIN {big} b WHERE a.duration_s + b.data_mb < -1')
    assert slow.status_code == 400 and slow.json()['detail'] == 'The query timed out after 2 s and was stopped.'
    # The server needs a moment to end what was stopped: SQL Server still lists the shared page locks of that
    # query for up to half a second after the app has its answer (measured: 11 to 16 locks, gone within 0.5 s).
    waited = time.monotonic()
    while le.open_locks(engine) and time.monotonic() - waited < 5:
        time.sleep(0.2)
    assert le.open_locks(engine) == []


def test_errors_from_the_server_keep_the_names_and_lose_the_data(engine, client, admin_ws):
    ws, source = admin_ws
    x = le.extra_schema(engine)
    missing = run(client, ws, source, f'SELECT * FROM {x}.no_such_table')
    assert missing.status_code == 400 and 'Blocked' not in missing.json()['detail']
    wrong = {'oracle': f"SELECT TO_NUMBER(label) FROM {x}.x_parents",
             'mssql': f"SELECT CAST(label AS INT) FROM {x}.x_parents",
             'postgresql': f"SELECT CAST(label AS INT) FROM {x}.x_parents",
             'mysql': f"SELECT CAST(label AS DATE) FROM {x}.x_parents WHERE STR_TO_DATE(label, '%Y') > NOW()"}[engine]
    failed = run(client, ws, source, wrong)
    assert 'parent 1' not in failed.text and 'parent 2' not in failed.text                    # a value of the label column


# ------------------------------------------- masking by table (the SQL masking round)

@pytest.fixture
def reader_ws(engine, client):
    """The demo schema and the third one, as the read-only account. Returns (workspace, source id)."""
    ws = client.post('/api/workspaces', json={'name': f'{engine} masking'}).json()['id']
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={
        **le.reader_profile(engine, schema_filter=le.extra_filter(engine)), 'password': le.READER_PASSWORD})
    assert added.status_code == 200, added.text
    return ws, added.json()['id']


def test_a_result_column_is_masked_by_the_table_it_comes_from(engine, client, reader_ws):
    """x_stores is a table of things with a column called just "name"; x_staff holds people, with
    first_name and salary. Each engine keeps and returns names in its own way (Oracle in upper case,
    SQL Server gives an unnamed AVG no name at all); what is masked must not depend on that."""
    ws, source = reader_ws
    x, demo = le.extra_schema(engine), le.ENGINES[engine]['schema']
    up = str.upper if engine == 'oracle' else str
    stores, staff = f'{x}.x_stores', f'{x}.x_staff'
    first_name, salary = up('x_staff.first_name') + ': looks like name', up('x_staff.salary')
    shops, people = [shop for shop, _ in le.STORES], list(le.STAFF)

    def shown(sql):
        response = run(client, ws, source, sql)
        assert response.status_code == 200, (sql, response.text)
        r = response.json()
        return [(c['personal'], c['reason']) for c in r['columns']], r['rows']

    def mark(table, column, personal):
        answer = client.post(f'/api/workspaces/{ws}/personal', json={'table': f'{x}.{up(table)}', 'column': up(column),
                                                                     'personal': personal})
        assert answer.status_code == 200, answer.text

    # 1. a table of things: its bare "name" is shown
    assert shown(f'SELECT name FROM {stores} ORDER BY store_id') == ([(False, None)], [[shop] for shop in shops])
    # 2. a person's name is masked - by its own name, under another, next to a shop's, built into something else
    assert shown(f'SELECT first_name FROM {staff} ORDER BY staff_id') == ([(True, first_name)], [[mask(p)] for p in people])
    columns, rows = shown(f'SELECT {"TOP 3 " if engine == "mssql" else ""}s.name, p.first_name AS shop_name, s.city '
                          f'FROM {staff} p JOIN {stores} s ON s.store_id = p.store_id ORDER BY p.staff_id')
    assert columns == [(False, None), (True, first_name), (False, None)]
    assert rows[:3] == [['Corniche Outlet', 'N***a', 'Jeddah'], ['Mountain Kiosk', 'F****l', 'Abha'], ['Airport Shop', 'L***a', 'Riyadh']]
    columns, rows = shown(f"SELECT UPPER(p.first_name) FROM {staff} p WHERE p.staff_id = 1")
    assert columns == [(True, f'Built from {up("x_staff.first_name")} (looks like name)')] and rows == [['N***A']]
    # 3. AVG(salary) without an alias: masked, whatever the engine calls that column
    pay = f'Built from {salary} (looks like salary / pay)'
    assert shown(f'SELECT AVG(salary) FROM {staff}') == ([(True, pay)], [['*****']])
    assert shown(f'SELECT MAX(p.salary), MIN(p.salary) + 1 FROM {staff} p') == ([(True, pay), (True, pay)], [['*****', '*****']])
    columns, rows = shown(f'SELECT job_title, salary FROM {staff} ORDER BY staff_id')
    assert columns == [(False, None), (True, f'{salary}: looks like salary / pay')] and rows[0] == ['Cashier', '*****']
    # 4. COUNT(salary) stays visible
    assert shown(f'SELECT COUNT(salary), COUNT(*), COUNT(DISTINCT first_name) FROM {staff}') == ([(False, None)] * 3, [[12, 12, 12]])
    # 5. a UNION that carries a personal column under the name of a thing: masked, the shops with it
    columns, rows = shown(f'SELECT name FROM {stores} UNION ALL SELECT first_name FROM {staff}')
    assert columns == [(True, first_name)] and sorted(r[0] for r in rows) == sorted(mask(v) for v in shops + people)
    columns, rows = shown(f'SELECT name FROM (SELECT first_name AS name FROM {staff}) q')
    assert columns == [(True, first_name)] and sorted(r[0] for r in rows) == sorted(mask(p) for p in people)
    # 6. SELECT *: expanded from what the catalog said, and it lines up with what this engine returns
    columns, rows = shown(f'SELECT * FROM {staff} p JOIN {stores} s ON s.store_id = p.store_id ORDER BY p.staff_id')
    assert columns == [(False, None), (True, first_name), (False, None), (True, f'{salary}: looks like salary / pay'),
                       (False, None), (False, None), (False, None), (False, None)]
    assert rows[0][:4] == [1, 'N***a', 'Cashier', '*****'] and rows[0][5:] == [2, 'Corniche Outlet', 'Jeddah'] and len(rows) == 11
    # 7. the demo schema: the customer is a person, the branch is a thing
    columns, rows = shown(f'SELECT c.full_name, b.branch_name FROM {demo}.customers c JOIN {demo}.branches b '
                          f'ON b.branch_id = c.branch_id WHERE c.customer_id = 1')
    assert columns == [(True, up('customers.full_name') + ': looks like name'), (False, None)] and '*' in rows[0][0]
    if engine in ('postgresql',):
        # the name of a table as a value is the whole row: every column of it, the personal ones too
        columns, rows = shown(f'SELECT p FROM {staff} p WHERE p.staff_id = 1')
        assert columns == [(True, pay)] and rows == [['*****']]

    # 8. your mark on a table column is obeyed, and only there
    mark('x_staff', 'first_name', False)
    mark('x_stores', 'name', True)
    assert shown(f'SELECT first_name FROM {staff} ORDER BY staff_id') == ([(False, None)], [[p] for p in people])
    assert shown(f'SELECT name FROM {stores} ORDER BY store_id') == (
        [(True, up('x_stores.name') + ': marked personal by you')], [[mask(shop)] for shop in shops])
    columns, rows = shown(f'SELECT s.name, p.first_name, s.city FROM {staff} p JOIN {stores} s ON s.store_id = p.store_id '
                          f'WHERE p.staff_id = 1')
    assert [c[0] for c in columns] == [True, False, False] and rows == [[mask('Corniche Outlet'), 'Noura', 'Jeddah']]
    assert shown(f'SELECT c.full_name FROM {demo}.customers c WHERE c.customer_id = 1')[0] == [
        (True, up('customers.full_name') + ': looks like name')]                 # another table's name: as before
    mark('x_staff', 'first_name', None)
    mark('x_stores', 'name', None)
    assert shown(f'SELECT first_name FROM {staff} WHERE staff_id = 1') == ([(True, first_name)], [['N***a']])
    assert shown(f'SELECT name FROM {stores} WHERE store_id = 1') == ([(False, None)], [['Olaya Gallery']])

    # 9. with "show personal values" on, the same statements give the real values
    client.put('/api/session', json={'show_personal': True})
    try:
        columns, rows = shown(f'SELECT AVG(salary) FROM {staff}')
        assert columns == [(True, pay)] and float(rows[0][0]) == pytest.approx(12128.25)
        assert shown(f'SELECT first_name FROM {staff} WHERE staff_id = 1')[1] == [['Noura']]
    finally:
        client.put('/api/session', json={'show_personal': False})
    assert le.open_locks(engine) == []


# ------------------------------- each engine's own reserved words (the demo masking round)

ORACLE_ONLY = ['public', 'session', 'comment', 'level', 'resource', 'size']        # reserved on Oracle; names elsewhere


def test_the_reader_takes_for_a_name_exactly_what_this_server_takes_for_one(engine):
    """sqlguard.RESERVED against the server itself: every word of the candidates (about 1,500) is asked
    as a table - SELECT 1 FROM <word> must answer "no such table" - and as the alias of one. On
    PostgreSQL and MySQL the server's own list of key words is read as well."""
    from scripts import reserved_words
    words = reserved_words.candidates([engine])
    answers = reserved_words.ask(engine, words)
    not_a_name = {word for word, (is_table, is_alias) in answers.items() if not (is_table and is_alias)}
    guard = sqlguard.RESERVED[engine]
    assert len(words) > 1000 and len(not_a_name) > 90
    if engine == 'oracle':
        # Oracle's list is the one of before this round. Oracle takes none of its reserved words for a name; the
        # three others are built-ins that are never a call. (The callable list is what protects Oracle, not this one.)
        assert sqlguard.ORACLE_RESERVED <= not_a_name and guard is sqlguard.ORACLE_NEVER_A_CALL
        assert guard - not_a_name == {'DBTIMEZONE', 'ORA_ROWSCN', 'SESSIONTIMEZONE'}
        assert all(answers[w.upper()] == (False, False) for w in ORACLE_ONLY)
        return
    # the direction that matters: what the reader takes for a name is a name to this server
    assert not_a_name <= guard, sorted(not_a_name - guard)
    # and the list is no longer than it has to be, the words added by hand aside
    assert guard - not_a_name == sqlguard.RESERVED_BY_HAND.get(engine, set()), sorted(guard - not_a_name)
    own = reserved_words.own_keywords(engine)
    if engine == 'postgresql':
        assert {w for w, category in own.items() if category in ('R', 'T')} == guard and len(own) > 400
        # and the names that are not a column's label without AS (x::character varying is not "x, called varying")
        assert reserved_words.needs_as() - guard == sqlguard.NEEDS_AS['postgresql']
    elif engine == 'mysql':
        assert {w for w, flag in own.items() if flag == '1'} == guard - sqlguard.RESERVED_BY_HAND['mysql'] and len(own) > 600
    for word in ORACLE_ONLY:
        assert answers[word.upper()] == ((False, False) if (engine, word) == ('mssql', 'public') else (True, True)), word


def test_a_table_under_a_name_that_only_oracle_reserves(engine, client, reader_ws):
    """FROM x_stores size: an alias on PostgreSQL, MySQL and SQL Server, and the reader follows it - the name
    of a shop is shown, which it is only when the column was followed to its table. On Oracle the word is
    reserved: the guard says so, as before, and nothing is sent."""
    ws, source = reader_ws
    x = le.extra_schema(engine)
    stores, staff = f'{x}.x_stores', f'{x}.x_staff'
    shops = [[shop] for shop, _ in le.STORES]
    for word in ORACLE_ONLY:
        thing = run(client, ws, source, f'SELECT {word}.name FROM {stores} {word} ORDER BY {word}.store_id')
        if engine == 'oracle' or (engine, word) == ('mssql', 'public'):
            assert thing.status_code == 400, (word, thing.text)
            detail = thing.json()['detail']
            assert detail.startswith('Blocked by the read-only guard: ') == (engine == 'oracle'), detail
            assert engine != 'oracle' or 'FROM clause' in detail
            continue
        assert thing.status_code == 200, (word, thing.text)
        r = thing.json()
        assert (r['columns'], r['rows'], r['masked']) == ([{'name': 'name', 'personal': False, 'reason': None}], shops, False), word
        person = run(client, ws, source, f'SELECT {word}.first_name AS name, s.name AS shop FROM {staff} AS {word} '
                                         f'JOIN {stores} s ON s.store_id = {word}.store_id WHERE {word}.staff_id = 1').json()
        assert [(c['personal'], c['reason']) for c in person['columns']] == [(True, 'x_staff.first_name: looks like name'), (False, None)]
        assert person['rows'] == [['N***a', 'Corniche Outlet']], word
        # a subquery and a column called like the word
        nested = run(client, ws, source, f'SELECT v.{word}, v.shop FROM (SELECT p.first_name AS {word}, s.name AS shop FROM {staff} p '
                                         f'JOIN {stores} s ON s.store_id = p.store_id WHERE p.staff_id = 2) v')
        assert nested.status_code == 200, (word, nested.text)
        assert nested.json()['rows'] == [['F****l', 'Mountain Kiosk']] and nested.json()['personal'] == [word], word
    assert le.open_locks(engine) == []


def test_postgresql_from_public_passes_the_guard_and_is_masked_by_its_table(engine, client):
    """The case that was found on 13 Oct 2026: FROM public.<table> was refused, "public" being a reserved
    word of Oracle. public.x_guests holds three people (first_name) and their rooms."""
    if engine != 'postgresql':
        pytest.skip('the schema called public is a PostgreSQL matter')
    ws = client.post('/api/workspaces', json={'name': 'postgresql public'}).json()['id']
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={
        **le.reader_profile(engine, schema_filter=f'{le.extra_filter(engine)}, public'), 'password': le.READER_PASSWORD})
    assert added.status_code == 200, added.text
    source = added.json()['id']
    guests, x = f'public.{le.PUBLIC_TABLE}', le.extra_schema(engine)
    name = 'x_guests.first_name: looks like name'

    def shown(sql):
        response = run(client, ws, source, sql)
        assert response.status_code == 200, (sql, response.text)
        r = response.json()
        return [(c['name'], c['personal'], c['reason']) for c in r['columns']], r['rows']

    people = [[mask(person), room] for person, room in le.GUESTS]
    assert people == [['S***a', 'A-101'], ['****', 'B-204'], ['****', 'C-310']]
    assert shown(f'SELECT first_name, room FROM {guests} ORDER BY guest_id') == ([('first_name', True, name), ('room', False, None)], people)
    assert shown(f'SELECT g.first_name, g.room FROM {guests} g ORDER BY g.guest_id')[1] == people
    assert shown(f'SELECT x_guests.first_name, {guests}.room FROM {guests} ORDER BY 2')[1] == people
    columns, rows = shown(f'SELECT * FROM {guests} ORDER BY guest_id')
    assert columns == [('guest_id', False, None), ('first_name', True, name), ('room', False, None)] and rows[0] == [1, 'S***a', 'A-101']
    # followed to its table, not judged by the name it is given: the room under the name of a person, and the other way round
    columns, rows = shown(f'SELECT g.room AS first_name, g.first_name AS room FROM {guests} g WHERE g.guest_id = 1')
    assert columns == [('first_name', False, None), ('room', True, name)] and rows == [['A-101', 'S***a']]
    columns, rows = shown(f'SELECT p.label, UPPER(public.first_name) AS who FROM {x}.x_parents p JOIN {guests} public '
                          f'ON public.guest_id = p.parent_id ORDER BY p.parent_id')
    assert columns[1] == ('who', True, 'Built from x_guests.first_name (looks like name)') and rows[0] == ['parent 1', 'S***A']
    # your mark on public.x_guests.first_name is obeyed - which it can only be when the column is followed
    for personal, expected in ((False, [['Salma'], ['Omar'], ['Huda']]), (None, [[mask(p)] for p, _ in le.GUESTS])):
        answer = client.post(f'/api/workspaces/{ws}/personal', json={'table': guests, 'column': 'first_name', 'personal': personal})
        assert answer.status_code == 200, answer.text
        assert shown(f'SELECT first_name FROM {guests} ORDER BY guest_id')[1] == expected
    # the Profile tab on that table, and the two tabs agree
    done = client.post(f'/api/workspaces/{ws}/profile', json={'tables': [guests]}).json()
    for _ in range(600):
        if client.get(f'/api/jobs/{done["id"]}').json()['state'] != 'running':
            break
        time.sleep(0.05)
    profile = client.get(f'/api/workspaces/{ws}/tables/{guests}/profile', params={'sample': 'false'}).json()
    assert [(c['name'], c['personal']) for c in profile['columns']] == [('guest_id', False), ('first_name', True), ('room', False)]
    # what stays refused: a function of the row, a function with the schema in front of it - and nothing was called
    for sql in (f'SELECT g.x_peek FROM {guests} g', f'SELECT public.x_peek(p) FROM {x}.x_parents p',
                f'SELECT {guests}.x_peek FROM {guests}', f'SELECT g.first_name FROM {guests} g FOR UPDATE'):
        refused = run(client, ws, source, sql)
        assert refused.status_code == 400 and refused.json()['detail'].startswith('Blocked by the read-only guard: '), sql
    from scripts import call_proof
    assert call_proof.logged(engine) == {} and le.open_locks(engine) == []
