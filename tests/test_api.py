"""The HTTP API end to end: workspaces, the three kinds of source, the merged model, export."""

import json
import sqlite3
import struct
import xml.etree.ElementTree as ET

import pytest

from app import demo
from expected import COLUMN_COUNTS, TABLES

PASSWORD = 'Sup3r-S3cret-Pa55'
FULL = {'tables': 10, 'columns': 70, 'relationships': 16, 'declared': 13, 'joins': 2, 'values': 0, 'names': 1}


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Test space'}).json()['id']


def add_demo_sources(client, ws, demo_dir):
    db = client.post(f'/api/workspaces/{ws}/sources/db', json={
        'engine': 'sqlite', 'database': str(demo_dir / 'telecom.db'), 'label': 'live'})
    sql = client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(demo_dir / 'telecom.sql')})
    files = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(demo_dir / 'csv'), 'label': 'exports'})
    assert (db.status_code, sql.status_code, files.status_code) == (200, 200, 200), (db.text, sql.text, files.text)
    return db.json(), sql.json(), files.json()


def tables_in(drawio):
    root = ET.fromstring(drawio)
    return sum(1 for c in root.iter('mxCell') if 'shape=table;' in (c.get('style') or ''))


def edges_in(drawio):
    return [c.get('style') for c in ET.fromstring(drawio).iter('mxCell') if c.get('edge') == '1']


# --------------------------------------------------------------------- basics

def test_health_and_meta(client):
    assert client.get('/api/health').json() == {'status': 'ok', 'app': 'Data Explorer', 'version': '0.3.0'}
    meta = client.get('/api/meta').json()
    assert [e['id'] for e in meta['engines']] == ['oracle', 'mssql', 'postgresql', 'mysql', 'sqlite']
    assert all(e['installed'] for e in meta['engines'])
    assert {e['id']: e['port'] for e in meta['engines']} == {
        'oracle': 1521, 'mssql': 1433, 'postgresql': 5432, 'mysql': 3306, 'sqlite': None}
    # every session switched its features on here; session 3 the three before the last, lineage round 3 scripts
    assert meta['features'] == {'er_graph': True, 'profile': True, 'quality': True, 'relations': True,
                                'sql': True, 'report': True, 'scripts': True}


def test_front_end_is_served(client):
    page = client.get('/')
    assert page.status_code == 200 and '<title>Data Explorer</title>' in page.text
    for path in ('/static/app.js', '/static/profile.js', '/static/styles.css', '/static/vendor/cytoscape.min.js',
                 '/static/vendor/chart.umd.min.js',
                 '/static/vendor/cytoscape-dagre.min.js', '/static/vendor/cytoscape-fcose.js'):
        assert client.get(path).status_code == 200, path


def test_workspace_lifecycle(client):
    assert client.get('/api/workspaces').json() == []
    created = client.post('/api/workspaces', json={'name': '  Billing project  '}).json()
    assert created['name'] == 'Billing project' and created['sources'] == []
    ws = created['id']
    assert [w['id'] for w in client.get('/api/workspaces').json()] == [ws]
    assert client.patch(f'/api/workspaces/{ws}', json={'name': 'Billing 2026'}).json()['name'] == 'Billing 2026'
    assert client.patch(f'/api/workspaces/{ws}', json={'name': ' '}).status_code == 400
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['tables'] == [] and model['stats']['tables'] == 0
    assert client.delete(f'/api/workspaces/{ws}').json() == {'deleted': ws}
    assert client.get(f'/api/workspaces/{ws}').status_code == 404
    assert client.get(f'/api/workspaces/{ws}/model').status_code == 404
    assert client.delete(f'/api/workspaces/{ws}').status_code == 404


# -------------------------------------------------------------------- sources

def test_add_sql_files_by_upload(client, ws, demo_sql, app):
    extra = b'SELECT u.used_gb FROM usage_summary u JOIN subscriptions s ON s.subscription_id = u.subscription_id;'
    response = client.post(f'/api/workspaces/{ws}/sources/sql', files=[
        ('files', ('telecom.sql', demo_sql.read_bytes(), 'application/sql')),
        ('files', ('usage report.sql', extra, 'application/sql'))])
    assert response.status_code == 200, response.text
    source = response.json()
    assert source['kind'] == 'sql_files' and source['tables'] == 11 and source['relationships'] == 16
    assert source['label'] == 'telecom.sql + 1 more' and source['settings']['mode'] == 'upload'
    assert source['settings']['files'] == ['telecom.sql', 'usage report.sql']
    stored = app.state.workspaces.upload_dir(ws, source['id'])
    assert sorted(p.name for p in stored.iterdir()) == ['telecom.sql', 'usage report.sql']

    model = client.get(f'/api/workspaces/{ws}/model').json()
    usage = next(t for t in model['tables'] if t['name'] == 'usage_summary')
    assert usage['complete'] is False and usage['origins'][0]['kind'] == 'query'
    assert usage['origins'][0]['detail'] == 'usage report.sql' and usage['column_names'] == ['used_gb', 'subscription_id']

    assert client.delete(f'/api/workspaces/{ws}/sources/{source["id"]}').status_code == 200
    assert not stored.exists()                                     # the uploaded copies go with the source
    assert client.get(f'/api/workspaces/{ws}/model').json()['tables'] == []


def test_add_files_by_upload_and_by_folder(client, ws, demo_csv):
    upload = client.post(f'/api/workspaces/{ws}/sources/files', data={'label': 'two tables'}, files=[
        ('files', ('customers.csv', (demo_csv / 'customers.csv').read_bytes(), 'text/csv')),
        ('files', ('branches.csv', (demo_csv / 'branches.csv').read_bytes(), 'text/csv'))])
    assert upload.status_code == 200, upload.text
    assert (upload.json()['label'], upload.json()['tables'], upload.json()['relationships']) == ('two tables', 2, 1)

    folder = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(demo_csv)})
    assert folder.status_code == 200 and folder.json()['tables'] == 10 and folder.json()['label'] == 'csv'
    assert folder.json()['settings'] == {'mode': 'path', 'path': str(demo_csv)}
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['stats']['tables'] == 10 and model['stats']['names'] == 15       # merged, not doubled
    customers = next(t for t in model['tables'] if t['name'] == 'customers')
    assert [o['label'] for o in customers['origins']] == ['two tables', 'csv']


def test_bad_sources_are_refused_with_a_clear_message(client, ws, tmp_path):
    def post(route, **kw):
        return client.post(f'/api/workspaces/{ws}/sources/{route}', **kw)
    assert 'Choose files' in post('sql', data={}).json()['detail']
    assert 'Not found' in post('sql', data={'path': str(tmp_path / 'missing')}).json()['detail']
    wrong = post('sql', files=[('files', ('data.csv', b'a,b\n1,2\n', 'text/csv'))])
    assert wrong.status_code == 400 and 'Not accepted here: data.csv' in wrong.json()['detail']
    assert post('files', files=[('files', ('evil.exe', b'MZ', 'application/octet-stream'))]).status_code == 400
    assert 'No tables' in post('sql', files=[('files', ('empty.sql', b'-- nothing', 'application/sql'))]).json()['detail']
    assert 'Choose a database engine' in post('db', json={'engine': 'access'}).json()['detail']
    assert 'not found' in post('db', json={'engine': 'sqlite', 'database': str(tmp_path / 'no.db')}).json()['detail']
    assert client.get(f'/api/workspaces/{ws}').json()['sources'] == []            # nothing half-added
    assert client.post('/api/workspaces/nope/sources/sql', data={'path': 'x'}).status_code == 404
    uploads = tmp_path / 'data' / 'uploads' / ws
    assert not uploads.exists() or list(uploads.iterdir()) == []                   # failed uploads are cleaned up


def test_upload_names_cannot_escape_the_upload_folder(client, ws, demo_sql, app):
    response = client.post(f'/api/workspaces/{ws}/sources/sql', files=[
        ('files', ('..\\..\\..\\evil.sql', demo_sql.read_bytes(), 'application/sql')),
        ('files', ('../../other/evil.sql', demo_sql.read_bytes(), 'application/sql'))])
    assert response.status_code == 200
    folder = app.state.workspaces.upload_dir(ws, response.json()['id'])
    assert sorted(p.name for p in folder.iterdir()) == ['evil.sql', 'evil_2.sql']
    assert not list(folder.parent.parent.parent.glob('evil*.sql'))


# ------------------------------------------------------------- live database

def test_connection_test_endpoint(client, demo_db, tmp_path):
    ok = client.post('/api/db/test', json={'engine': 'sqlite', 'database': str(demo_db)})
    assert ok.status_code == 200 and ok.json()['ok'] and ok.json()['tables'] == 10
    bad = client.post('/api/db/test', json={'engine': 'sqlite', 'database': str(tmp_path / 'missing.db')})
    assert bad.status_code == 400 and 'not found' in bad.json()['detail']
    assert client.get('/api/workspaces').json() == []              # testing saves nothing


def test_passwords_are_never_stored(client, ws, demo_db, app, tmp_path):
    body = {'engine': 'sqlite', 'database': str(demo_db), 'username': 'reader', 'password': PASSWORD,
            'schema_filter': '', 'save_profile': True, 'profile_name': 'demo db'}
    source = client.post(f'/api/workspaces/{ws}/sources/db', json=body)
    assert source.status_code == 200, source.text
    assert client.post('/api/profiles', json={**body, 'name': 'second profile'}).status_code == 200

    profiles = client.get('/api/profiles').json()
    assert {p['name'] for p in profiles} == {'demo db', 'second profile'}
    assert all('password' not in p for p in profiles)
    for text in (source.text, client.get(f'/api/workspaces/{ws}').text, client.get(f'/api/workspaces/{ws}/model').text,
                 client.get('/api/profiles').text):
        assert PASSWORD not in text and 'password' not in json.loads(text if text[0] != '[' else '{}')

    # nothing on disk contains it: the app database (and its journal), and every other file in the data folder
    data = tmp_path / 'data'
    for path in data.rglob('*'):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path
    con = sqlite3.connect(data / 'explorer.db')
    columns = {r[1] for table in ('workspaces', 'sources', 'profiles') for r in con.execute(f'PRAGMA table_info({table})')}
    stored = con.execute('SELECT settings FROM sources').fetchone()[0]
    con.close()
    assert not any('pass' in c.lower() for c in columns)
    assert 'password' not in stored and json.loads(stored)['username'] == 'reader'

    # it is in memory for this session only
    assert app.state.workspaces._passwords == {(ws, source.json()['id']): PASSWORD}
    assert client.delete(f'/api/profiles/{profiles[0]["id"]}').status_code == 200


def test_a_saved_database_source_needs_its_password_again_after_a_restart(client, ws, app):
    # A PostgreSQL source saved by an earlier run of the app: settings are on disk, the password is not.
    store = app.state.workspaces
    settings = {'engine': 'postgresql', 'host': '127.0.0.1', 'port': 9, 'database': 'crm', 'username': 'reader',
                'schema_filter': '', 'include_views': False}
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('src1', ws, 'database', 'crm', json.dumps(settings), json.dumps({'tables': [], 'relationships': []}),
                     '[]', '2026-10-01T10:00:00', '2026-10-01T10:00:00'))
    (source,) = client.get(f'/api/workspaces/{ws}').json()['sources']
    assert source['needs_password'] is True
    refused = client.post(f'/api/workspaces/{ws}/sources/src1/refresh', json={})
    assert refused.status_code == 409 and refused.json()['needs_password'] is True
    # with a password it tries to connect (nothing listens on that port) and fails cleanly, without echoing it
    failed = client.post(f'/api/workspaces/{ws}/sources/src1/refresh', json={'password': PASSWORD})
    assert failed.status_code == 400 and PASSWORD not in failed.text
    assert store._passwords == {}                                   # a failed attempt is not remembered


# ------------------------------------------------------------- merged model

def test_merged_model_of_three_sources(client, ws, demo_dir):
    db, sql, files = add_demo_sources(client, ws, demo_dir)
    assert (db['tables'], db['relationships']) == (10, 13)
    assert (sql['tables'], sql['relationships']) == (10, 15)
    assert (files['tables'], files['relationships']) == (10, 15)
    assert sql['label'] == 'telecom.sql' and not db['needs_password']

    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['stats'] == FULL and model['schemas'] == ['']
    assert {t['name'] for t in model['tables']} == TABLES
    assert {t['name']: t['column_count'] for t in model['tables']} == COLUMN_COUNTS
    assert all([o['kind'] for o in t['origins']] == ['db', 'ddl', 'file'] for t in model['tables'])
    assert 'columns' not in model['tables'][0] and len(model['tables'][0]['column_names']) > 0
    by_id = {r['id']: r for r in model['relationships']}
    assert len(by_id) == 16 and sorted(by_id) == sorted(f'r{i}' for i in range(16))
    inferred = sorted((r['from'], r['from_columns'][0], r['to'], r['source'], r['confidence'], r['cardinality'])
                      for r in model['relationships'] if r['source'] != 'declared')
    assert inferred == [('accounts', 'customer_id', 'tickets', 'joins', 0.5, 'unknown'),
                        ('payments', 'account_id', 'accounts', 'joins', 0.85, '1-N'),
                        ('tickets', 'branch_id', 'branches', 'names', 0.85, '1-N')]
    assert [s['kind'] for s in model['workspace']['sources']] == ['database', 'sql_files', 'files']


def test_table_details(client, ws, demo_dir):
    add_demo_sources(client, ws, demo_dir)
    table = client.get(f'/api/workspaces/{ws}/tables/tickets').json()
    assert table['name'] == 'tickets' and table['aliases'] == ['t'] and table['profile'] is None
    columns = {c['name']: c for c in table['columns']}
    assert list(columns) == ['ticket_id', 'customer_id', 'subscription_id', 'branch_id', 'opened_at', 'closed_at',
                             'category', 'priority', 'status']
    assert columns['ticket_id']['pk'] and columns['ticket_id']['nullable'] is False
    assert columns['customer_id']['fk'] and not columns['customer_id']['fk_inferred']
    assert not columns['branch_id']['fk'] and columns['branch_id']['fk_inferred']
    assert columns['branch_id']['types'] == {'db': 'INTEGER', 'ddl': 'INTEGER', 'file': 'BIGINT'}
    assert columns['closed_at']['nullable'] is True
    links = {(l['role'], l['other'], l['source']): l for l in table['relationships']}
    assert set(links) == {('references', 'customers', 'declared'), ('references', 'subscriptions', 'declared'),
                          ('references', 'branches', 'names'), ('match', 'accounts', 'joins')}
    assert links[('references', 'branches', 'names')]['columns'] == ['branch_id']
    assert links[('references', 'branches', 'names')]['confidence'] == 0.85

    accounts = client.get(f'/api/workspaces/{ws}/tables/ACCOUNTS').json()        # case-insensitive
    roles = sorted((l['role'], l['other']) for l in accounts['relationships'])
    assert roles == [('match', 'tickets'), ('referenced_by', 'invoices'), ('referenced_by', 'payments'),
                     ('referenced_by', 'subscriptions'), ('references', 'customers'), ('self', 'accounts')]
    assert client.get(f'/api/workspaces/{ws}/tables/no_such_table').status_code == 404


def test_tables_with_a_schema_in_their_id(client, ws):
    ddl = b'CREATE TABLE sales.orders (order_id INT PRIMARY KEY, customer_id INT REFERENCES crm.customers (customer_id));'
    assert client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('s.sql', ddl, 'text/plain'))]).status_code == 200
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['schemas'] == ['crm', 'sales'] and {t['id'] for t in model['tables']} == {'sales.orders', 'crm.customers'}
    assert client.get(f'/api/workspaces/{ws}/tables/sales.orders').json()['schema'] == 'sales'
    only_sales = client.get(f'/api/workspaces/{ws}/export?format=drawio&schema=sales')
    assert tables_in(only_sales.text) == 1


def test_refresh_reads_a_source_again(client, ws, tmp_path):
    folder = tmp_path / 'sql'
    folder.mkdir()
    (folder / 'a.sql').write_text('CREATE TABLE a (a_id INT PRIMARY KEY);')
    source = client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(folder)}).json()
    assert source['tables'] == 1
    (folder / 'b.sql').write_text('CREATE TABLE b (b_id INT PRIMARY KEY, a_id INT REFERENCES a (a_id));')
    again = client.post(f'/api/workspaces/{ws}/sources/{source["id"]}/refresh', json={}).json()
    assert again['tables'] == 2 and again['relationships'] == 1
    assert client.get(f'/api/workspaces/{ws}/model').json()['stats']['tables'] == 2
    assert client.post(f'/api/workspaces/{ws}/sources/nope/refresh', json={}).status_code == 404


def test_removing_a_source_updates_the_model(client, ws, demo_dir):
    db, sql, files = add_demo_sources(client, ws, demo_dir)
    assert client.delete(f'/api/workspaces/{ws}/sources/{sql["id"]}').status_code == 200
    stats = client.get(f'/api/workspaces/{ws}/model').json()['stats']
    assert (stats['declared'], stats['joins'], stats['names']) == (13, 0, 2)     # the join evidence is gone
    assert client.delete(f'/api/workspaces/{ws}/sources/{sql["id"]}').status_code == 404


# --------------------------------------------------------------------- export

def test_export_whole_diagram(client, ws, demo_dir):
    add_demo_sources(client, ws, demo_dir)
    drawio = client.get(f'/api/workspaces/{ws}/export?format=drawio')
    assert drawio.status_code == 200 and drawio.headers['content-type'] == 'application/xml'
    assert drawio.headers['content-disposition'] == 'attachment; filename="Test_space.drawio"'
    assert tables_in(drawio.text) == 10
    styles = edges_in(drawio.text)
    assert len(styles) == 16
    assert sum('dashed=1' in s for s in styles) == 3              # the three inferred links
    assert sum('ERmany' in s for s in styles) == 15               # all but the value match have a direction
    assert 'solid line = declared key, dashed line = inferred relationship' in drawio.text

    png = client.get(f'/api/workspaces/{ws}/export?format=png')
    assert png.status_code == 200 and png.headers['content-type'] == 'image/png'
    assert png.headers['content-disposition'] == 'attachment; filename="Test_space.png"'
    assert png.content[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('>II', png.content[16:24])
    assert width > 1500 and height > 800
    every = client.get(f'/api/workspaces/{ws}/export?format=png&all_columns=true')
    assert struct.unpack('>II', every.content[16:24])[1] > height           # all columns: taller boxes


def test_export_focus_view_matches_the_page(client, ws, demo_dir):
    add_demo_sources(client, ws, demo_dir)
    one = client.get(f'/api/workspaces/{ws}/export?format=drawio&focus=accounts&hops=1')
    assert one.headers['content-disposition'] == 'attachment; filename="Test_space-accounts-1hop.drawio"'
    assert tables_in(one.text) == 6 and len(edges_in(one.text)) == 9
    assert tables_in(client.get(f'/api/workspaces/{ws}/export?format=drawio&focus=accounts&hops=2').text) == 10
    declared_only = client.get(f'/api/workspaces/{ws}/export?format=drawio&focus=tickets&hops=1&kinds=declared')
    assert tables_in(declared_only.text) == 3 and not any('dashed=1' in s for s in edges_in(declared_only.text))
    confident = client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=declared,joins,names&min_confidence=0.6')
    assert len(edges_in(confident.text)) == 15
    png = client.get(f'/api/workspaces/{ws}/export?format=png&focus=plans&hops=1')
    assert png.content[:4] == b'\x89PNG' and 'plans-1hop.png' in png.headers['content-disposition']


def test_export_errors(client, ws, demo_dir):
    assert client.get(f'/api/workspaces/{ws}/export?format=png').json() == {'detail': 'There are no tables in this view.'}
    add_demo_sources(client, ws, demo_dir)
    assert client.get(f'/api/workspaces/{ws}/export?format=pdf').status_code == 400
    assert client.get(f'/api/workspaces/{ws}/export?format=png&focus=nope').status_code == 404
    assert client.get(f'/api/workspaces/{ws}/export?format=png&schema=other').status_code == 400
    assert client.get('/api/workspaces/nope/export?format=png').status_code == 404


# ----------------------------------------------------------------------- demo

def test_demo_workspace(client, app):
    assert app.state.workspaces.ensure_demo() == demo.DEMO_WORKSPACE_ID == 'demo'
    model = client.get('/api/workspaces/demo/model').json()
    assert model['workspace']['name'] == 'Demo - telecom' and model['stats'] == FULL
    assert [s['kind'] for s in model['workspace']['sources']] == ['database', 'sql_files', 'files']
    assert all(s['warnings'] == [] for s in model['workspace']['sources'])
    app.state.workspaces.ensure_demo()                             # running --demo again starts clean
    assert client.get('/api/workspaces/demo/model').json()['stats'] == FULL
    assert len(client.get('/api/workspaces').json()) == 1


def test_demo_data_is_complete_and_consistent(demo_dir):
    con = sqlite3.connect(f'file:{(demo_dir / "telecom.db").as_posix()}?mode=ro', uri=True)
    rows = {t: con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in demo.TABLES}
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    orphans = con.execute('SELECT COUNT(*) FROM payments p LEFT JOIN accounts a ON a.account_id = p.account_id '
                          'WHERE a.account_id IS NULL').fetchone()[0]
    con.close()
    assert set(rows) == TABLES and orphans == 0
    assert rows == {'branches': 8, 'customers': 80, 'accounts': 100, 'plans': 10, 'subscriptions': 150,
                    'devices': 110, 'invoices': 200, 'invoice_lines': 513, 'payments': 177, 'tickets': 90}
    for table, count in rows.items():
        lines = (demo_dir / 'csv' / f'{table}.csv').read_text(encoding='utf-8').splitlines()
        assert len(lines) == count + 1, table                      # the CSV exports hold the same rows
    sql = (demo_dir / 'telecom.sql').read_text(encoding='utf-8')
    assert sql.count('CREATE TABLE') == 10 and sql.count('JOIN') >= 8
