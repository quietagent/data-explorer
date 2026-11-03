"""Session 3, part C: the SQL tab's runner - sources, the row cap, the timeout, masking, CSV, history."""

import csv
import io
import json
import sqlite3
import threading
import time
from datetime import date

import pytest
from openpyxl import Workbook

from app import config
from app.analysis import sqlrunner
from app.analysis.sqlrunner import derived_columns, select_items
from app.profiling.personal import mask, personal_by_name
from app.sources.database import ReadOnlyDatabase
from app.sqlguard import Checked, tokenize

N = 1500


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'SQL'}).json()['id']


@pytest.fixture
def shop(client, ws, tmp_path):
    """A SQLite database with 1,500 customers and 1,000 orders. Returns (workspace, source id)."""
    path = tmp_path / 'shop.db'
    con = sqlite3.connect(path)
    con.executescript('''CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, city TEXT,
        ref TEXT, joined DATE, note TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL);''')
    con.executemany('INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)', [
        (i, f'Customer Al-Name{i:04d}', f'05{50000000 + i:08d}', ['Riyadh', 'Jeddah', 'Abha'][i % 3],
         f'{1 + i % 2}{100000000 + i:09d}', f'2025-{1 + i % 12:02d}-15', '=SUM(A1:A9)' if i == 1 else f'note {i}')
        for i in range(1, N + 1)])
    con.executemany('INSERT INTO orders VALUES (?, ?, ?)', [(i, 1 + i % 50, i * 1.5) for i in range(1, 1001)])
    con.commit()
    con.close()
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    assert added.status_code == 200, added.text
    return ws, added.json()['id']


def run(client, ws, source, sql, **extra):
    return client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': sql, **extra})


def ok(client, ws, source, sql):
    response = run(client, ws, source, sql)
    assert response.status_code == 200, response.text
    return response.json()


class Recorder(ReadOnlyDatabase):
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Recorder.opened.append(self)


@pytest.fixture
def recorder(monkeypatch):
    Recorder.opened = []
    monkeypatch.setattr(sqlrunner, 'ReadOnlyDatabase', Recorder)
    return Recorder


# ------------------------------------------------------------------- sources

def test_the_user_picks_a_database_or_a_files_source(client, ws, demo_db, demo_sql, demo_csv):
    for route, body in (('db', None), ('sql', {'path': str(demo_sql)}), ('files', {'path': str(demo_csv)})):
        if route == 'db':
            client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(demo_db)})
        else:
            client.post(f'/api/workspaces/{ws}/sources/{route}', data=body)
    info = client.get(f'/api/workspaces/{ws}/sql').json()
    assert [(s['kind'], s['dialect'], s['engine'], len(s['tables'])) for s in info['sources']] == [
        ('database', 'sqlite', 'sqlite', 10), ('files', 'duckdb', 'DuckDB (files)', 10)]      # SQL files hold no data
    assert (info['max_rows'], info['timeout_s'], info['history_size'], info['history']) == (1000, 30, 50, [])
    database, files = (s['id'] for s in info['sources'])
    sql_files = next(s['id'] for s in client.get(f'/api/workspaces/{ws}').json()['sources'] if s['kind'] == 'sql_files')
    assert ok(client, ws, database, 'SELECT COUNT(*) AS n FROM customers')['rows'] == [[80]]
    assert ok(client, ws, files, 'SELECT COUNT(*) AS n FROM customers')['rows'] == [[80]]
    refused = run(client, ws, sql_files, 'SELECT 1')
    assert refused.status_code == 400 and 'structure only' in refused.json()['detail']
    assert run(client, ws, None, 'SELECT 1').status_code == 404              # two sources: one has to be chosen
    assert run(client, ws, 'nope', 'SELECT 1').status_code == 404
    assert client.get('/api/workspaces/nope/sql').status_code == 404


# ------------------------------------------------------------- limits and results

def test_at_most_a_thousand_rows_and_whether_more_exist(client, shop, recorder):
    ws, source = shop
    big = ok(client, ws, source, 'SELECT customer_id, city FROM customers ORDER BY customer_id')
    assert (big['row_count'], big['more'], big['max_rows'], len(big['rows'])) == (1000, True, 1000, 1000)
    assert big['rows'][0] == [1, 'Jeddah'] and big['rows'][-1] == [1000, 'Jeddah']
    exact = ok(client, ws, source, 'SELECT order_id FROM orders')            # exactly 1,000: nothing more
    assert (exact['row_count'], exact['more']) == (1000, False)
    small = ok(client, ws, source, 'SELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY city')
    assert small['rows'] == [['Abha', 500], ['Jeddah', 500], ['Riyadh', 500]] and small['more'] is False
    assert [c['name'] for c in small['columns']] == ['city', 'n'] and small['elapsed_s'] >= 0
    assert small['source'] == {'id': source, 'label': 'SQLite · shop.db', 'dialect': 'sqlite'}
    # 1,001 rows were fetched for the big one - not 1,500 - and the statement was sent as typed
    assert len(recorder.opened) == 3
    for database in recorder.opened:
        assert database.blocked == [] and database.timeout_s == config.SQL_TIMEOUT_S == 30
        assert [s for s in database.statements if not s.startswith('PRAGMA')] == [database.statements[-1]]
    assert recorder.opened[0].statements[-1] == 'SELECT customer_id, city FROM customers ORDER BY customer_id'


def test_what_is_sent_is_the_checked_text_without_its_comments(client, shop, recorder):
    ws, source = shop
    result = ok(client, ws, source, "SELECT city -- the town\n, COUNT(*) AS n /* how many */ FROM customers "
                                    "WHERE note LIKE 'note 1%' GROUP BY city; ")
    assert result['sql'] == "SELECT city  \n, COUNT(*) AS n   FROM customers WHERE note LIKE 'note 1%' GROUP BY city"
    assert recorder.opened[0].statements[-1] == result['sql'] and len(result['rows']) == 3     # the % is not a placeholder


def test_a_query_that_runs_too_long_is_stopped(client, shop, monkeypatch):
    ws, source = shop
    monkeypatch.setattr(config, 'SQL_TIMEOUT_S', 1)
    started = time.monotonic()
    slow = run(client, ws, source, 'WITH RECURSIVE n (x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 100000000000) '
                                   'SELECT COUNT(*) FROM n')
    assert slow.status_code == 400 and slow.json()['detail'] == 'The query timed out after 1 s and was stopped.'
    assert time.monotonic() - started < 15
    assert ok(client, ws, source, 'SELECT 1 AS one')['rows'] == [[1]]        # and the next one runs


def test_a_refused_statement_is_never_sent(client, shop, recorder):
    ws, source = shop
    for sql, why in [('DELETE FROM customers', "Only read-only queries can run (statement starts with 'DELETE')."),
                     ('SELECT 1; DROP TABLE orders', 'Only one statement at a time is allowed.'),
                     ("SELECT load_extension('x')", 'load_extension() is not on the list of functions'),
                     ('SELECT * FROM customers FOR UPDATE', 'Locking reads'), ('', 'Empty statement.')]:
        refused = run(client, ws, source, sql)
        assert refused.status_code == 400 and refused.json()['detail'].startswith('Blocked by the read-only guard: ' + why[:25])
    assert recorder.opened == []                                         # no connection was even opened
    con = sqlite3.connect(client.app.state.workspaces.db_path)
    assert [r[0] for r in con.execute('SELECT outcome FROM sql_history')] == ['blocked'] * 4
    con.close()


def test_one_query_at_a_time_per_workspace(client, shop, monkeypatch):
    ws, source = shop
    inside, release = threading.Event(), threading.Event()
    real = sqlrunner.SqlRunner._run_database

    def held(self, workspace_id, src, checked):
        inside.set()
        release.wait(10)
        return real(self, workspace_id, src, checked)
    monkeypatch.setattr(sqlrunner.SqlRunner, '_run_database', held)
    answers = []
    first = threading.Thread(target=lambda: answers.append(run(client, ws, source, 'SELECT 1 AS a')))
    first.start()
    assert inside.wait(10)
    busy = run(client, ws, source, 'SELECT 2 AS a')
    assert busy.status_code == 409 and 'already running' in busy.json()['detail']
    release.set()
    first.join(10)
    assert answers[0].status_code == 200 and ok(client, ws, source, 'SELECT 3 AS a')['rows'] == [[3]]


def test_a_database_password_is_asked_for(client, app, ws):
    store = app.state.workspaces
    settings = {'engine': 'postgresql', 'host': '127.0.0.1', 'port': 9, 'database': 'crm', 'username': 'reader',
                'schema_filter': '', 'include_views': False}
    snapshot = {'tables': [{'name': 'orders', 'schema': 'public', 'origins': [
        {'kind': 'db', 'source_id': 'src1', 'label': 'crm', 'schema': 'public', 'table': 'orders'}],
        'columns': [{'name': 'order_id', 'type': 'INTEGER', 'origins': ['db']}]}], 'relationships': []}
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('src1', ws, 'database', 'crm', json.dumps(settings), json.dumps(snapshot), '[]', '2026', '2026'))
    assert client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]['needs_password'] is True
    asked = run(client, ws, 'src1', 'SELECT 1')
    assert asked.status_code == 409 and asked.json()['needs_password'] is True and asked.json()['source']['id'] == 'src1'
    failed = run(client, ws, 'src1', 'SELECT 1', password='S3cret-Pa55')      # nothing listens there: a clean error
    assert failed.status_code == 400 and 'S3cret-Pa55' not in failed.text
    assert store.password(ws, 'src1') == 'S3cret-Pa55' and 'S3cret' not in store.db_path.read_bytes().decode('latin-1')


# ------------------------------------------------------------- personal columns

def test_personal_columns_of_a_result_are_masked(client, shop):
    ws, source = shop
    r = ok(client, ws, source, 'SELECT customer_id, full_name, phone, city, ref FROM customers ORDER BY customer_id LIMIT 3')
    assert [(c['name'], c['personal'], c['reason']) for c in r['columns']] == [
        ('customer_id', False, None), ('full_name', True, 'customers.full_name: looks like name'),
        ('phone', True, 'customers.phone: looks like phone number'),
        ('city', False, None), ('ref', True, 'Its values look like national ID / Iqama')]
    assert r['rows'][0] == [1, mask('Customer Al-Name0001'), '05******01', 'Jeddah', '21******01']
    assert (r['masked'], r['personal'], r['show_personal']) == (True, ['full_name', 'phone', 'ref'], False)
    assert 'Al-Name' not in json.dumps(r) and '0550000001' not in json.dumps(r)
    star = ok(client, ws, source, 'SELECT * FROM customers WHERE customer_id = 7')
    assert star['personal'] == ['full_name', 'phone'] and star['rows'][0][1] == mask('Customer Al-Name0007')


NAME, PHONE = 'customers.full_name: looks like name', 'customers.phone: looks like phone number'
FROM_NAME, FROM_PHONE = 'Built from customers.full_name (looks like name)', 'Built from customers.phone (looks like phone number)'


@pytest.mark.parametrize('sql, personal', [
    ('SELECT full_name AS who FROM customers LIMIT 5', {'who': NAME}),
    ('SELECT c.full_name who, c.city town FROM customers c LIMIT 5', {'who': NAME}),
    ("SELECT UPPER(full_name) || ' / ' || city AS label FROM customers LIMIT 5", {'label': FROM_NAME}),
    ('SELECT MAX(full_name) AS last_one, MIN(phone) AS lowest FROM customers', {'last_one': FROM_NAME, 'lowest': FROM_PHONE}),
    ('WITH people AS (SELECT full_name AS n, city FROM customers) SELECT n, city FROM people LIMIT 5', {'n': NAME}),
    ('SELECT x FROM (SELECT y AS x FROM (SELECT phone AS y FROM customers)) LIMIT 5', {'x': PHONE}),
    ('SELECT customer_id, (SELECT full_name FROM customers c2 WHERE c2.customer_id = c.customer_id) AS looked_up '
     'FROM customers c LIMIT 5', {'looked_up': FROM_NAME}),
    ("SELECT CASE WHEN city = 'Abha' THEN phone ELSE NULL END AS maybe FROM customers WHERE city = 'Abha' LIMIT 5",
     {'maybe': FROM_PHONE}),
])
def test_a_personal_column_under_another_name_is_still_masked(client, shop, sql, personal):
    ws, source = shop
    r = ok(client, ws, source, sql)
    assert r['personal'] == list(personal) and r['masked'] is True
    text = json.dumps(r['rows'])
    assert 'Al-Name' not in text and 'AL-NAME' not in text and '05500' not in text
    # the reason names the table column it comes from: as it is, or built from it
    assert {c['name']: c['reason'] for c in r['columns'] if c['personal']} == personal


def test_counts_of_personal_columns_are_not_personal(client, shop):
    ws, source = shop
    r = ok(client, ws, source, 'SELECT city, COUNT(full_name) AS names, COUNT(DISTINCT phone) AS phones, COUNT(*) AS n '
                                'FROM customers GROUP BY city ORDER BY city')
    assert r['personal'] == [] and r['masked'] is False and r['rows'][0] == ['Abha', 500, 500, 500]


def test_values_decide_when_the_name_says_nothing(client, shop):
    ws, source = shop
    r = ok(client, ws, source, "SELECT '05' || SUBSTR(ref, 3, 8) AS code, customer_id + 1 AS next_id FROM customers LIMIT 20")
    assert [(c['name'], c['personal'], c['reason']) for c in r['columns']] == [
        ('code', True, 'Its values look like phone number'), ('next_id', False, None)]
    assert r['rows'][0][0].startswith('05') and '*' in r['rows'][0][0] and r['rows'][0][1] == 2


def test_a_column_the_user_marked_is_masked_in_results(client, shop):
    ws, source = shop
    assert ok(client, ws, source, 'SELECT city FROM customers LIMIT 1')['personal'] == []
    client.post(f'/api/workspaces/{ws}/personal', json={'table': 'customers', 'column': 'city', 'personal': True})
    r = ok(client, ws, source, 'SELECT city, city AS town FROM customers WHERE customer_id = 1')
    assert r['personal'] == ['city', 'town'] and r['rows'] == [['J****h', 'J****h']]
    assert [c['reason'] for c in r['columns']] == ['customers.city: marked personal by you'] * 2


def profiled(client, ws):
    job = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json()
    for _ in range(6000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            assert state['state'] == 'done' and state['errors'] == [], state
            return
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def test_the_demo_is_masked_in_the_sql_tab_exactly_when_the_profile_tab_masks_it(client, app):
    """Every column of the --demo telecom workspace, on both of its sources that hold data (the SQLite
    database, and the CSV exports through DuckDB): by its name, under another name, through SELECT *."""
    ws = app.state.workspaces.ensure_demo()
    profiled(client, ws)
    sources = {s['dialect']: s['id'] for s in client.get(f'/api/workspaces/{ws}/sql').json()['sources']}
    assert sorted(sources) == ['duckdb', 'sqlite']
    walked, masked = 0, set()
    for table in sorted(t['id'] for t in client.get(f'/api/workspaces/{ws}/model').json()['tables']):
        columns = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': 'false'}).json()['columns']
        for dialect, source in sources.items():
            star = ok(client, ws, source, f'SELECT * FROM {table}')
            assert [c['name'] for c in star['columns']] == [c['name'] for c in columns], (dialect, table)
            for i, c in enumerate(columns):
                key = f'{table}.{c["name"]}'
                reason = f'{key}: l{c["personal_reason"][1:]}' if c['personal'] else None      # "Looks like name" in the Profile tab
                plain = ok(client, ws, source, f'SELECT {c["name"]} FROM {table}')['columns'][0]
                alias = ok(client, ws, source, f'SELECT t.{c["name"]} AS v FROM {table} t')['columns'][0]
                for column in (plain, alias, star['columns'][i]):
                    assert (column['personal'], column['reason']) == (c['personal'], reason), (dialect, key, column)
                masked.update([key] if c['personal'] else [])
                walked += 1
    assert walked == 2 * 70
    assert masked == {'customers.full_name', 'customers.national_id', 'customers.email', 'customers.phone', 'devices.imei',
                      'subscriptions.msisdn'}
    # the names of things in it: shown, on both sources
    for source in sources.values():
        r = ok(client, ws, source, 'SELECT b.branch_name, p.plan_name, c.full_name FROM customers c JOIN branches b '
                                   'ON b.branch_id = c.branch_id CROSS JOIN plans p ORDER BY c.customer_id, p.plan_id LIMIT 3')
        assert r['personal'] == ['full_name'] and r['columns'][2]['reason'] == 'customers.full_name: looks like name'


def test_what_profiling_found_in_the_values_counts_in_the_sql_tab_too(client, shop):
    """customers.ref holds national ID numbers under a name that says nothing. One row of it is too
    little to tell by - but the Profile tab knows the column once the table is profiled."""
    ws, source = shop
    one = 'SELECT ref FROM customers WHERE customer_id = 7'
    assert ok(client, ws, source, one)['columns'] == [{'name': 'ref', 'personal': False, 'reason': None}]
    profiled(client, ws)
    profile = {c['name']: c for c in client.get(f'/api/workspaces/{ws}/tables/customers/profile').json()['columns']}
    assert profile['ref']['personal'] is True and profile['ref']['personal_reason'] == 'Looks like national ID / Iqama'
    r = ok(client, ws, source, one)
    assert r['columns'] == [{'name': 'ref', 'personal': True, 'reason': 'customers.ref: its values look like national ID / Iqama'}]
    assert r['rows'] == [['21******07']]
    r = ok(client, ws, source, "SELECT 'no. ' || ref AS label FROM customers WHERE customer_id = 7")
    assert r['columns'][0]['reason'] == 'Built from customers.ref (its values look like national ID / Iqama)' and '*' in r['rows'][0][0]
    # marked "not personal": shown, and the values do not overrule the mark - not for one row, not for a thousand
    client.post(f'/api/workspaces/{ws}/personal', json={'table': 'customers', 'column': 'ref', 'personal': False})
    assert ok(client, ws, source, one)['rows'] == [['2100000007']]
    r = ok(client, ws, source, 'SELECT ref, c.ref AS again FROM customers c ORDER BY customer_id')
    assert r['personal'] == [] and r['rows'][0] == ['2100000001', '2100000001'] and len(r['rows']) == 1000


def test_a_table_that_changed_since_the_source_was_read_is_judged_by_names(client, shop, tmp_path):
    """SELECT * is expanded from the source's structure. When the engine's own column names do not
    match it, nothing of the result is taken as resolved."""
    ws, source = shop
    r = ok(client, ws, source, 'SELECT * FROM customers WHERE customer_id = 7')
    assert [c['reason'] for c in r['columns'] if c['personal']] == ['customers.full_name: looks like name',
                                                                    'customers.phone: looks like phone number']
    con = sqlite3.connect(tmp_path / 'shop.db')                          # the same columns in another order, and one more
    con.executescript('''ALTER TABLE customers RENAME TO old;
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, city TEXT, full_name TEXT, nickname TEXT, phone TEXT, ref TEXT,
                                joined DATE, note TEXT);
        INSERT INTO customers SELECT customer_id, city, full_name, 'Nick' || customer_id, phone, ref, joined, note FROM old;
        DROP TABLE old;''')
    con.commit()
    con.close()
    r = ok(client, ws, source, 'SELECT * FROM customers WHERE customer_id = 7')
    assert [(c['name'], c['reason']) for c in r['columns'] if c['personal']] == [
        ('full_name', 'Looks like name'), ('nickname', 'Looks like name'), ('phone', 'Looks like phone number')]
    assert r['rows'][0][:5] == [7, 'Jeddah', mask('Customer Al-Name0007'), 'N***7', '05******07']
    # a column that is named is still found in its table
    assert ok(client, ws, source, 'SELECT city, full_name FROM customers WHERE customer_id = 7')['columns'][1]['reason'] == (
        'customers.full_name: looks like name')


def test_show_personal_values_unmasks_results_too(client, shop):
    ws, source = shop
    client.put('/api/session', json={'show_personal': True})
    r = ok(client, ws, source, 'SELECT full_name, phone FROM customers WHERE customer_id = 1')
    assert r['rows'] == [['Customer Al-Name0001', '0550000001']] and (r['masked'], r['show_personal']) == (False, True)
    assert r['personal'] == ['full_name', 'phone']                       # still flagged, for the page's banner
    shown = client.get(f'/api/workspaces/{ws}/sql/results/{r["id"]}.csv')
    assert shown.status_code == 200 and 'Customer Al-Name0001' in shown.text       # exactly as shown
    client.put('/api/session', json={'show_personal': False})
    gone = client.get(f'/api/workspaces/{ws}/sql/results/{r["id"]}.csv')            # what was shown unmasked leaves memory
    assert gone.status_code == 404 and 'Run the query again' in gone.json()['detail']
    assert ok(client, ws, source, 'SELECT full_name FROM customers WHERE customer_id = 1')['rows'] == [[mask('Customer Al-Name0001')]]


def test_select_lists_are_read_for_aliases():
    tokens = [t for t in tokenize('SELECT a, b AS bee, UPPER(c) cee, t.d, COUNT(*), e + 1 FROM t', 'postgresql')]
    (_, items), = select_items(tokens)
    assert [i['alias'] for i in items] == ['A', 'BEE', 'CEE', 'D', None, None]
    assert [len(i['tokens']) for i in items] == [1, 1, 4, 3, 4, 3]
    star = select_items(tokenize('SELECT *, t.* FROM t', 'mysql'))[0][1]
    assert [i['star'] for i in star] == [True, True]
    nested = select_items(tokenize('SELECT x FROM (SELECT full_name AS x FROM (SELECT 1) q) z', 'mysql'))
    assert [depth for depth, _ in nested] == [0, 1, 2]
    outer, tainted = derived_columns(tokenize('SELECT x, 1 AS one FROM (SELECT full_name AS x FROM t) z', 'mysql'), personal_by_name)
    assert outer == ['name', None] and tainted == {'X': 'name'}
    outer, _ = derived_columns(tokenize('SELECT * FROM t', 'mysql'), personal_by_name)
    assert outer is None                                                 # a star: the result's own column names decide


# ------------------------------------------------------------------------ CSV

def test_csv_export_is_exactly_what_was_shown(client, shop):
    ws, source = shop
    r = ok(client, ws, source, 'SELECT customer_id, full_name, phone, city, note, joined, NULL AS empty_one FROM customers '
                                'ORDER BY customer_id LIMIT 3')
    response = client.get(f'/api/workspaces/{ws}/sql/results/{r["id"]}.csv')
    assert response.status_code == 200 and response.headers['content-type'] == 'text/csv; charset=utf-8'
    assert response.headers['content-disposition'] == f'attachment; filename="query-{r["id"]}.csv"'
    assert response.content.startswith(b'\xef\xbb\xbf')                  # so that Excel reads it as UTF-8
    rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert rows[0] == ['customer_id', 'full_name', 'phone', 'city', 'note', 'joined', 'empty_one']
    assert rows[1] == ['1', mask('Customer Al-Name0001'), '05******01', 'Jeddah', "'=SUM(A1:A9)", '2025-02-15', '']
    for shown, exported in zip(r['rows'], rows[1:]):                     # the same masks, cell by cell
        assert exported[:4] == [str(v) for v in shown[:4]]
    assert 'Al-Name' not in response.text and '0550000001' not in response.text
    # a cell that a spreadsheet would run as a formula gets an apostrophe; numbers do not
    assert r['rows'][0][4] == '=SUM(A1:A9)' and rows[1][4] == "'=SUM(A1:A9)"
    negative = ok(client, ws, source, "SELECT -5 AS n, '-5 below' AS t, '+966' AS p, '@x' AS a")
    exported = list(csv.reader(io.StringIO(client.get(f'/api/workspaces/{ws}/sql/results/{negative["id"]}.csv').text.lstrip('﻿'))))
    assert exported[1] == ['-5', "'-5 below", "'+966", "'@x"]
    assert client.get(f'/api/workspaces/{ws}/sql/results/nope.csv').status_code == 404
    other = client.post('/api/workspaces', json={'name': 'other'}).json()['id']
    assert client.get(f'/api/workspaces/{other}/sql/results/{r["id"]}.csv').status_code == 404     # another workspace's result


def test_only_a_few_results_are_kept_in_memory(client, shop, app):
    ws, source = shop
    ids = [ok(client, ws, source, f'SELECT {i} AS n')['id'] for i in range(sqlrunner.KEEP_RESULTS + 3)]
    assert len(app.state.sql._results) == sqlrunner.KEEP_RESULTS == 8
    assert client.get(f'/api/workspaces/{ws}/sql/results/{ids[0]}.csv').status_code == 404
    assert client.get(f'/api/workspaces/{ws}/sql/results/{ids[-1]}.csv').status_code == 200
    client.delete(f'/api/workspaces/{ws}/sources/{source}')              # they go with their source
    assert app.state.sql._results == {}


# -------------------------------------------------------------------- history

def test_the_last_fifty_query_texts_are_kept_per_workspace(client, shop, tmp_path):
    ws, source = shop
    for i in range(55):
        ok(client, ws, source, f'SELECT {i} AS n, full_name FROM customers WHERE customer_id = {i + 1}')
    run(client, ws, source, 'DELETE FROM customers')                     # a refused one is remembered too, as refused
    run(client, ws, source, 'SELECT nope FROM customers')
    history = client.get(f'/api/workspaces/{ws}/sql').json()['history']
    assert len(history) == 50 == config.SQL_HISTORY
    assert [h['outcome'] for h in history[:3]] == ['failed', 'blocked', 'ok']
    assert history[0]['sql'] == 'SELECT nope FROM customers' and history[2]['sql'].startswith('SELECT 54 AS n')
    assert set(history[0]) == {'id', 'source_id', 'sql', 'outcome', 'ran_at'} and history[0]['source_id'] == source
    assert history[-1]['sql'].startswith('SELECT 7 AS n')                # the oldest five and more fell out
    ok(client, ws, source, 'SELECT 30 AS n, full_name FROM customers WHERE customer_id = 31')    # run again: moves to the top
    again = client.get(f'/api/workspaces/{ws}/sql').json()['history']
    assert len(again) == 50 and again[0]['sql'].startswith('SELECT 30 AS n') and sum(h['sql'].startswith('SELECT 30 AS') for h in again) == 1
    # text only: not one value of a result is on disk
    blob = (tmp_path / 'data' / 'explorer.db').read_bytes()
    assert b'Al-Name' not in blob and b'05500000' not in blob and b'SELECT 30 AS n' in blob
    other = client.post('/api/workspaces', json={'name': 'other'}).json()['id']
    assert client.get(f'/api/workspaces/{other}/sql').json()['history'] == []
    cleared = client.delete(f'/api/workspaces/{ws}/sql/history')
    assert cleared.json() == {'cleared': 50} and client.get(f'/api/workspaces/{ws}/sql').json()['history'] == []
    assert b'SELECT 30 AS n' not in (tmp_path / 'data' / 'explorer.db').read_bytes()


def test_errors_keep_the_names_typed_and_lose_the_data(client, shop, ws, tmp_path, demo_csv):
    _, source = shop
    unknown = run(client, ws, source, 'SELECT nope FROM customers')
    assert unknown.status_code == 400 and unknown.json()['detail'] == 'no such column: nope'
    files = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(demo_csv)}).json()['id']
    wrong = run(client, ws, files, 'SELECT CAST(full_name AS INTEGER) FROM customers')
    assert wrong.status_code == 400 and 'Could not convert string' in wrong.json()['detail']
    assert 'Al-' not in wrong.json()['detail'] and "'…'" in wrong.json()['detail']       # the value it tripped over is gone
    missing = run(client, ws, files, 'SELECT * FROM no_such_table')
    assert missing.status_code == 400 and 'no_such_table' in missing.json()['detail']


# ---------------------------------------------------------------------- files

@pytest.fixture
def exports(client, ws, tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'FILE_TYPE_ROWS', 50)        # column types come from a file's first rows
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'customers.csv').write_text('customer_id,full_name,msisdn,branch_code,joined\n' + ''.join(
        f'{i},Person Al-File{i:03d},05{60000000 + i:08d},00{i % 5},2025-{1 + i % 12:02d}-10\n' for i in range(1, 41)), encoding='utf-8')
    (folder / 'payments.csv').write_text('payment_id,customer_id,amount,paid_on\n' + ''.join(
        f'{i},{1 + i % 40},{i}.5,2025-03-{1 + i % 28:02d}\n' for i in range(1, 101)) + '101,7,not-a-number,never\n', encoding='utf-8')
    (folder / 'cities.csv').write_bytes('id,city\n1,الرياض\n2,جدة\n'.encode('cp1256'))
    wb = Workbook()
    first = wb.active
    first.title = 'orders'
    first.append(['order_id', 'customer_id', 'placed'])
    for i in range(1, 21):
        first.append([i, 1 + i % 40, date(2026, 1, i)])
    second = wb.create_sheet('codes')
    second.append(['code', 'label'])
    second.append(['001', 'first'])
    wb.save(folder / 'shop.xlsx')
    (tmp_path / 'secret.csv').write_text('k\nTOP-SECRET\n', encoding='utf-8')
    added = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    assert added.status_code == 200, added.text
    return ws, added.json()['id']


def test_files_are_queried_through_duckdb_with_every_table_as_a_view(client, exports, tmp_path):
    ws, source = exports
    info = client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]
    assert (info['dialect'], info['tables']) == ('duckdb', ['cities', 'customers', 'payments', 'shop.codes', 'shop.orders'])
    joined = ok(client, ws, source, 'SELECT c.customer_id, c.branch_code, SUM(p.amount) AS paid, COUNT(*) AS n FROM customers c '
                                    'JOIN payments p ON p.customer_id = c.customer_id GROUP BY ALL ORDER BY c.customer_id LIMIT 2')
    assert joined['rows'] == [[1, '001', 121.0, 2], [2, '002', 124.5, 3]]      # leading zeros kept; amounts are numbers
    typed = ok(client, ws, source, 'SELECT typeof(customer_id), typeof(branch_code), typeof(joined), MIN(joined), MAX(joined) '
                                   'FROM customers GROUP BY ALL')
    assert typed['rows'] == [['BIGINT', 'VARCHAR', 'DATE', '2025-01-10', '2025-12-10']]
    # payments.csv has a row, after the ones its types were detected from, that is no number and no date:
    # it reads as empty there, and fails nothing
    bad = ok(client, ws, source, 'SELECT COUNT(*) AS n, COUNT(amount) AS amounts, COUNT(paid_on) AS dates FROM payments')
    assert bad['rows'] == [[101, 100, 100]]
    excel = ok(client, ws, source, 'SELECT o.order_id, o.placed, c.full_name FROM shop.orders o JOIN customers c USING (customer_id) '
                                   'ORDER BY 1 LIMIT 1')
    assert excel['rows'] == [[1, '2026-01-01', mask('Person Al-File002')]] and excel['personal'] == ['full_name']
    assert ok(client, ws, source, 'SELECT code, label FROM shop.codes')['rows'] == [['001', 'first']]
    assert ok(client, ws, source, 'SELECT city FROM cities ORDER BY id')['rows'] == [['الرياض'], ['جدة']]       # cp1256
    masked = ok(client, ws, source, 'SELECT full_name, msisdn FROM customers ORDER BY customer_id LIMIT 1')
    assert masked['rows'] == [[mask('Person Al-File001'), '05******01']]
    capped = ok(client, ws, source, 'SELECT a.payment_id FROM payments a, payments b')
    assert (capped['row_count'], capped['more']) == (1000, True)
    assert sorted(p.name for p in (tmp_path / 'data').iterdir()) == ['explorer.db']      # nothing was written
    assert sorted(p.name for p in (tmp_path / 'exports').iterdir()) == ['cities.csv', 'customers.csv', 'payments.csv', 'shop.xlsx']


def test_the_guard_refuses_duckdbs_file_functions(client, exports, tmp_path):
    ws, source = exports
    secret = (tmp_path / 'secret.csv').as_posix()
    for sql in (f"SELECT * FROM read_csv('{secret}')", f"SELECT * FROM read_csv_auto('{secret}')", f"SELECT * FROM '{secret}'",
                f"SELECT * FROM read_text('{secret}')", f"SELECT * FROM glob('{tmp_path.as_posix()}/*')",
                f"COPY customers TO '{tmp_path.as_posix()}/out.csv'", f"ATTACH '{tmp_path.as_posix()}/x.db' AS x",
                'INSTALL httpfs', 'LOAD httpfs', "SELECT getenv('USERNAME')", 'PRAGMA database_list',
                "SELECT * FROM duckdb_settings()", f"SELECT * FROM customers; COPY customers TO '{tmp_path.as_posix()}/out.csv'"):
        refused = run(client, ws, source, sql)
        assert refused.status_code == 400 and refused.json()['detail'].startswith('Blocked by the read-only guard'), sql
    assert not (tmp_path / 'out.csv').exists() and not (tmp_path / 'x.db').exists()


def test_past_the_guard_duckdb_itself_can_reach_nothing_but_the_sources_files(client, app, exports, tmp_path):
    """The second line: file access is cut off before the statement runs. The guard is skipped here on purpose."""
    ws, source_id = exports
    runner = app.state.sql
    source = next(s for s in app.state.workspaces.source_rows(ws) if s['id'] == source_id)
    secret, out = (tmp_path / 'secret.csv').as_posix(), tmp_path.as_posix()

    def unchecked(sql):
        return runner._run_files(source, Checked(sql, tokenize(sql, 'duckdb'), 'duckdb'))
    assert unchecked('SELECT COUNT(*) FROM customers')[1] == [(40,)]
    for sql, why in [(f"SELECT * FROM read_csv('{secret}')", 'Permission Error'), (f"SELECT * FROM '{secret}'", 'Permission Error'),
                     (f"SELECT * FROM read_text('{secret}')", 'Permission Error'), (f"SELECT * FROM glob('{out}/*')", 'Permission Error'),
                     (f"COPY customers TO '{out}/out.csv'", 'Permission Error'), (f"ATTACH '{out}/x.db' AS x", 'Permission Error'),
                     ('INSTALL httpfs', 'Permission Error'), ('LOAD httpfs', 'Permission Error'),
                     (f"EXPORT DATABASE '{out}/dump'", 'Permission Error'),
                     ('SET enable_external_access = true', 'configuration has been locked'),
                     ("SET allowed_paths = ['/']", 'configuration has been locked'),
                     ("SELECT getenv('USERNAME')", 'does not exist'), ('SELECT * FROM tmp_path', 'does not exist')]:
        with pytest.raises(sqlrunner.SourceError) as failure:
            unchecked(sql)
        assert why in str(failure.value), (sql, str(failure.value))
        assert 'TOP-SECRET' not in str(failure.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['data', 'exports', 'secret.csv']       # nothing was written


def test_a_query_on_files_times_out_too(client, exports, monkeypatch):
    ws, source = exports
    monkeypatch.setattr(config, 'SQL_TIMEOUT_S', 1)
    started = time.monotonic()
    slow = run(client, ws, source, 'SELECT COUNT(*) FROM payments a, payments b, payments c, payments d, payments e '
                                   'WHERE a.amount + b.amount + c.amount + d.amount + e.amount < 0')
    assert slow.status_code == 400 and slow.json()['detail'] == 'The query timed out after 1 s and was stopped.'
    assert time.monotonic() - started < 20


def oracle_source(app, ws, catalog='read'):
    """An Oracle source as it is saved: its structure and - catalog - what its database can call.
    Nothing listens at its address, so a statement that gets past the guard fails to connect."""
    store = app.state.workspaces
    settings = {'engine': 'oracle', 'host': '127.0.0.1', 'port': 9, 'database': 'X', 'username': 'reader',
                'schema_filter': 'BILLING', 'include_views': False}
    snapshot = {'tables': [{'name': 'INVOICES', 'schema': 'BILLING', 'origins': [
        {'kind': 'db', 'source_id': 'ora', 'label': 'ora', 'schema': 'BILLING', 'table': 'INVOICES'}],
        'columns': [{'name': 'INVOICE_ID', 'type': 'NUMBER(10, 0)', 'origins': ['db']},
                    {'name': 'AMOUNT', 'type': 'NUMBER(10, 2)', 'origins': ['db']},
                    {'name': 'STATUS', 'type': 'VARCHAR2(10)', 'origins': ['db']}]}], 'relationships': []}
    if catalog == 'read':
        snapshot['catalog'] = {'callable': ['POST_AND_RETURN', 'STATUS', 'DBMS_RANDOM'], 'schemas': ['BILLING', 'SYS'],
                               'error': None, 'read_at': '2026-10-12T10:00:00'}
    elif catalog is not None:
        snapshot['catalog'] = catalog
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('ora', ws, 'database', 'ora', json.dumps(settings), json.dumps(snapshot), '[]', '2026', '2026'))
    store.set_password(ws, 'ora', 'pw')
    return store


def test_oracle_gets_the_names_the_source_knows(client, app, ws, recorder):
    """The guard's check of names (a function called without brackets) is fed from the source's structure
    and from what its catalog said can be called."""
    store = oracle_source(app, ws)
    known = app.state.sql._names(store.source_rows(ws)[0])
    assert known.tables == {('BILLING', 'INVOICES'): {'INVOICE_ID', 'AMOUNT', 'STATUS'}}
    assert known.callable == {'POST_AND_RETURN', 'STATUS', 'DBMS_RANDOM'} and known.schemas == {'BILLING', 'SYS'}
    assert client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]['blocked'] is None
    for sql in ('SELECT post_and_return FROM dual', 'SELECT amount, billing.post_and_return FROM billing.invoices',
                'SELECT invoice_id FROM billing.invoices WHERE post_and_return = 1',
                'SELECT invoice_id post_and_return FROM billing.invoices',                  # an alias called like it
                'SELECT post_and_return FROM billing.invoices post_and_return',
                'SELECT "POST_AND_RETURN" FROM dual',
                'SELECT status FROM dual', 'SELECT status FROM billing.invoices',           # a column, and a function
                'SELECT amount FROM dual'):                                                # a column of no table of that block
        refused = run(client, ws, 'ora', sql)
        assert refused.status_code == 400 and refused.json()['detail'].startswith('Blocked by the read-only guard'), sql
        assert 'function' in refused.json()['detail']
    assert recorder.opened == []                                                            # none of them opened a connection
    for sql in ('SELECT invoice_id, SUM(amount) total FROM billing.invoices GROUP BY invoice_id ORDER BY total',
                'SELECT i.status, i.amount FROM billing.invoices i WHERE i.status IS NOT NULL'):
        passed = run(client, ws, 'ora', sql)
        assert passed.status_code == 400 and 'Blocked' not in passed.json()['detail'], sql  # past the guard; nothing listens there
    assert len(recorder.opened) == 2


@pytest.mark.parametrize('catalog, why', [
    (None, 'this source was added before the app read them'),
    ({'callable': None, 'schemas': [], 'error': 'ORA-00942: table or view does not exist'},
     'they could not be read from the catalog (ORA-00942: table or view does not exist)'),
    ({'callable': None, 'schemas': [], 'error': 'no answer within 30 s'},
     'they could not be read from the catalog (no answer within 30 s)'),
])
def test_an_oracle_source_without_the_list_of_what_can_be_called_runs_nothing(client, app, ws, recorder, catalog, why):
    """Fail closed: there is no weaker check to fall back to. The SQL tab says that it cannot run
    queries on that source yet, and why - also for a statement that is harmless."""
    oracle_source(app, ws, catalog)
    listed = client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]
    assert listed['blocked'].startswith('The SQL tab cannot run queries on this Oracle source yet.') and why in listed['blocked']
    assert 'Refresh the source' in listed['blocked']
    for sql in ('SELECT invoice_id FROM billing.invoices', 'SELECT 1 FROM dual', 'SELECT post_and_return FROM dual'):
        refused = run(client, ws, 'ora', sql)
        assert refused.status_code == 400 and refused.json()['detail'] == listed['blocked'], sql
    assert recorder.opened == []
    # ... and the guard itself refuses too, should a caller ever get that far
    known = app.state.sql._names(app.state.workspaces.source_rows(ws)[0])
    assert known.callable is None
    with pytest.raises(sqlrunner.ReadOnlyViolation, match='have not been read'):
        sqlrunner.check_read_only_sql('SELECT 1 FROM dual', 'oracle', strict=True, names=known)


def test_other_engines_need_no_such_list(client, shop):
    ws, source = shop
    assert client.get(f'/api/workspaces/{ws}/sql').json()['sources'][0]['blocked'] is None
    assert ok(client, ws, source, 'SELECT COUNT(*) AS n FROM customers')['rows'] == [[N]]


def test_what_can_be_called_is_worked_out_from_one_read_of_the_catalog():
    from app.security import check_read_only_sql
    from app.sources.database import ORACLE_CALLABLE_SQL, callable_names
    check_read_only_sql(ORACLE_CALLABLE_SQL, dialect='oracle')                              # the app's own guard lets it through
    assert ORACLE_CALLABLE_SQL.count('SELECT') == 3 and ';' not in ORACLE_CALLABLE_SQL      # one statement
    for word in ('ALL_OBJECTS', 'ALL_SYNONYMS', 'ALL_USERS', 'FUNCTION', 'PROCEDURE', 'PACKAGE', 'TYPE', 'OPERATOR'):
        assert word in ORACLE_CALLABLE_SQL.upper()
    rows = [('O', 'APP', 'POST_AND_RETURN', None, None, None), ('O', 'SYS', 'DBMS_RANDOM', None, None, None),
            ('O', 'APP', 'MONEY_T', None, None, None),
            ('Y', 'PUBLIC', 'DBMS_RANDOM', 'SYS', 'DBMS_RANDOM', None),
            ('Y', 'PUBLIC', 'POST_IT', 'APP', 'POST_AND_RETURN', None),                     # a synonym for a function
            ('Y', 'READER', 'P2', 'PUBLIC', 'P1', None), ('Y', 'PUBLIC', 'P1', 'PUBLIC', 'POST_IT', None),   # ... for a synonym for one
            ('Y', 'PUBLIC', 'FAR_AWAY', 'X', 'Y', 'OTHER_DB'),                              # over a database link: not knowable
            ('Y', 'PUBLIC', 'DUAL', 'SYS', 'DUAL', None), ('Y', 'PUBLIC', 'INVOICES', 'APP', 'INVOICES', None),   # tables
            ('U', None, 'APP', None, None, None), ('U', None, 'SYS', None, None, None)]
    assert callable_names(rows) == {
        'callable': ['DBMS_RANDOM', 'FAR_AWAY', 'MONEY_T', 'P1', 'P2', 'POST_AND_RETURN', 'POST_IT'], 'schemas': ['APP', 'SYS']}


def test_a_catalog_read_that_fails_is_kept_as_the_reason(monkeypatch):
    from app.sources.database import ConnectionProfile, ReadOnlyDatabase as Database

    class Connection:
        def __init__(self, rows=None, error=None):
            self.rows, self.error, self.sent = rows, error, []

        def exec_driver_sql(self, sql):
            self.sent.append(sql)
            if self.error:
                raise self.error
            return self

        def fetchall(self):
            return self.rows

    db = Database(ConnectionProfile.from_dict({'engine': 'oracle', 'host': 'h', 'database': 'X', 'username': 'u'}), 'secret-pw')
    good = Connection([('O', 'A', 'F', None, None, None), ('U', None, 'A', None, None, None)])
    read = db.read_callable(good)
    assert (read['callable'], read['schemas'], read['error']) == (['F'], ['A'], None) and len(good.sent) == 1
    failed = db.read_callable(Connection(error=RuntimeError('ORA-00942: table or view "ALL_OBJECTS" does not exist; secret-pw')))
    assert failed['callable'] is None and 'ORA-00942' in failed['error'] and 'secret-pw' not in failed['error']
    assert db.read_callable(Connection([]))['callable'] is None                             # an empty catalog is not an answer
    monkeypatch.setattr(db, 'timeout_s', 0)
    assert db.read_callable(Connection(error=RuntimeError('DPY-4024: call timeout')))['error'] == 'no answer within 0 s'
    db.dispose()


def test_the_list_is_saved_with_the_structure_of_the_source():
    from app.model import SchemaModel
    model = SchemaModel()
    assert 'catalog' not in model.to_dict() and SchemaModel.from_dict(model.to_dict()).catalog is None
    model.catalog = {'callable': ['F'], 'schemas': ['A'], 'error': None}
    assert SchemaModel.from_dict(json.loads(json.dumps(model.to_dict()))).catalog == model.catalog
