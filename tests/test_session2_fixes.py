"""The fixes from the session 1 audit (part A of session 2), one group of tests per finding."""

import json
import re
import sqlite3
import struct

import pytest
from openpyxl import Workbook

from app import config, model as model_module
from app.er.render_png import DiagramTooLarge, choose_dpi
from app.model import DECLARED, JOINS, NAMES, Origin, SchemaModel, finalize_model, relationship_kinds
from app.sources import database
from app.sources.database import ConnectionProfile, ReadOnlyDatabase, ReadOnlyRefused
from app.sources.files import collect_data_files, read_files, source_root
from conftest import ROOT
from expected import links


def table(model, name, columns, origin='db', pk=()):
    t = model.table(name)
    t.add_origin(Origin(origin))
    for c in columns:
        column, kind = c if isinstance(c, tuple) else (c, 'BIGINT')
        t.add_column(column, origin, type=kind, pk=column in pk)
    return t


def inferred(*tables):
    """Build a model from (name, columns, pk) triples and return its names-inferred links."""
    m = SchemaModel()
    for name, columns, pk in tables:
        table(m, name, columns, pk=pk)
    finalize_model(m)
    assert all(r.source == NAMES for r in m.relationships)
    return {(r.from_table.name, r.from_columns[0], r.to_table.name, r.to_columns[0]): r.confidence
            for r in m.relationships}


# ------------------------------------------- A1: names inference and declared keys

def test_a_declared_key_named_id_is_linked():
    # The audit's example: this gave no link, while the same tables without declared keys did.
    assert inferred(('customers', ['id', 'name'], ['id']), ('orders', ['id', 'customer_id'], ['id'])) == {
        ('orders', 'customer_id', 'customers', 'id'): 0.85}
    assert inferred(('customers', ['id', 'name'], []), ('orders', ['id', 'customer_id'], [])) == {
        ('orders', 'customer_id', 'customers', 'id'): 0.85}


def test_a_declared_key_named_after_the_table_is_linked():
    assert inferred(('customers', ['customer_id', 'name'], ['customer_id']),
                    ('orders', ['id', 'customer_id'], ['id'])) == {
        ('orders', 'customer_id', 'customers', 'customer_id'): 0.85}


def test_a_declared_key_with_any_other_name_is_linked():
    found = inferred(('customers', ['cust_no', 'name'], ['cust_no']),
                     ('orders', ['order_no', 'customer_id', 'customerid', 'customer_key', 'billing_customer_id',
                                 'parent_order_id'], ['order_no']))
    assert found == {('orders', 'customer_id', 'customers', 'cust_no'): 0.85,
                     ('orders', 'customerid', 'customers', 'cust_no'): 0.85,
                     ('orders', 'customer_key', 'customers', 'cust_no'): 0.85,
                     ('orders', 'billing_customer_id', 'customers', 'cust_no'): 0.65,     # prefixed: less sure
                     ('orders', 'parent_order_id', 'orders', 'order_no'): 0.65}           # self-reference


def test_a_composite_key_is_not_linked_by_one_column():
    assert inferred(('customers', ['region', 'seq', 'name'], ['region', 'seq']),
                    ('orders', ['id', 'customer_id'], ['id'])) == {}


def test_two_candidate_tables_give_no_link():
    assert inferred(('customer', ['id'], ['id']), ('customers', ['id'], ['id']),
                    ('orders', ['id', 'customer_id'], ['id'])) == {}
    # one declared and one not declared: still two tables that fit equally well
    m = SchemaModel()
    table(m, 'customer', ['id'], pk=['id'])
    table(m, 'customers', ['id'], origin='file')
    table(m, 'orders', ['id', 'customer_id'], pk=['id'])
    assert finalize_model(m).relationships == []


def test_no_link_when_it_is_unclear_which_column_is_meant():
    # customers has its own, non-key customer_id next to the declared key: do not guess.
    assert inferred(('customers', ['id', 'customer_id'], ['id']), ('orders', ['id', 'customer_id'], ['id'])) == {}
    # a table's own key is not a pointer to itself
    assert inferred(('customers', ['id', 'customers_id'], ['id'])) == {}


def test_a_declared_foreign_key_is_never_replaced_by_a_name_guess():
    m = SchemaModel()
    customers = table(m, 'customers', ['id'], pk=['id'])
    people = table(m, 'people', ['id'], pk=['id'])
    orders = table(m, 'orders', ['id', 'customer_id'], pk=['id'])
    m.relate(orders, ['customer_id'], people, ['id'], DECLARED, cardinality='1-N')
    finalize_model(m)
    assert [(r.from_table, r.to_table, r.source) for r in m.relationships] == [(orders, people, DECLARED)]
    assert customers.column('id').pk


def test_declared_keys_in_a_live_database_get_inferred_links(client, tmp_path):
    path = tmp_path / 'shop.db'
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, total REAL);
    """)
    con.close()
    ws = client.post('/api/workspaces', json={'name': 'shop'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)}).status_code == 200
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert [(r['from'], r['from_columns'], r['to'], r['to_columns'], r['source'], r['cardinality'])
            for r in model['relationships']] == [('orders', ['customer_id'], 'customers', ['id'], 'names', '1-N')]
    orders = client.get(f'/api/workspaces/{ws}/tables/orders').json()
    assert {c['name']: c['fk_inferred'] for c in orders['columns']} == {'id': False, 'customer_id': True, 'total': False}


# ------------------------------------------------- A2: Excel sheets and file origins

def workbook(path, sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)


def test_sheets_with_the_same_name_in_two_workbooks_stay_apart(tmp_path):
    workbook(tmp_path / 'north.xlsx', {'Sheet1': [['order_id', 'amount'], [1, 10]], 'Notes': [['note'], ['x']]})
    workbook(tmp_path / 'south.xlsx', {'Sheet1': [['order_id', 'amount', 'city'], [2, 20, 'Abha']],
                                       'Notes': [['note'], ['y']]})
    workbook(tmp_path / 'plans.xlsx', {'Sheet1': [['plan_id', 'fee'], [1, 35]]})
    model, warnings = read_files(collect_data_files(tmp_path), 'exports', 'src', root=tmp_path)
    assert warnings == []
    assert sorted(t.id for t in model.tables) == ['north.Notes', 'north.Sheet1', 'plans', 'south.Notes', 'south.Sheet1']
    north, south = model.find('north.Sheet1'), model.find('south.Sheet1')
    assert north is not south and (north.schema, north.name) == ('north', 'Sheet1')
    assert [c.name for c in north.columns] == ['order_id', 'amount']
    assert [c.name for c in south.columns] == ['order_id', 'amount', 'city']         # not merged into one table
    assert model.find('plans').schema is None                                         # one sheet: the file's name


def test_file_origins_store_the_path_and_the_sheet(tmp_path):
    (tmp_path / 'crm' / '2026').mkdir(parents=True)
    (tmp_path / 'crm' / '2026' / 'customers.csv').write_text('customer_id,name\n1,Ann\n')
    (tmp_path / 'orders.csv').write_text('order_id,customer_id\n10,1\n')
    workbook(tmp_path / 'crm' / 'export.xlsx', {'customers_old': [['customer_id'], [1]], 'visits': [['visit_id'], [5]]})
    model, _ = read_files(collect_data_files(tmp_path), 'exports', 'src', root=tmp_path)
    origin = {t.id: t.origins[0] for t in model.tables}
    assert (origin['customers'].path, origin['customers'].sheet) == ('crm/2026/customers.csv', None)
    assert (origin['orders'].path, origin['orders'].detail) == ('orders.csv', 'orders.csv')
    assert (origin['export.visits'].path, origin['export.visits'].sheet) == ('crm/export.xlsx', 'visits')
    assert origin['export.visits'].detail == 'crm/export.xlsx · sheet visits'
    for o in origin.values():                                    # the path leads back to the real file
        assert (tmp_path / o.path).is_file() and o.kind == 'file' and o.source_id == 'src'
    again = SchemaModel.from_dict(json.loads(json.dumps(model.to_dict())))             # kept in the snapshot
    assert again.find('export.visits').origins[0].to_dict() == origin['export.visits'].to_dict()


def test_one_file_as_a_source_and_non_utf8_files(tmp_path):
    single = tmp_path / 'deep' / 'plans.csv'
    single.parent.mkdir()
    single.write_text('plan_id,fee\n1,35\n')
    assert source_root(single) == single.parent and source_root(tmp_path) == tmp_path
    model, _ = read_files(collect_data_files(single), root=source_root(single))
    assert model.find('plans').origins[0].path == 'plans.csv'
    arabic = tmp_path / 'cities.csv'
    arabic.write_bytes('id,city\n1,الرياض\n2,جدة\n'.encode('cp1256'))
    model, warnings = read_files([arabic], root=tmp_path)
    assert model.find('cities').origins[0].encoding == 'cp1256' and 'cities.csv: not UTF-8' in warnings[0]


def test_names_inference_still_works_inside_a_workbook(tmp_path):
    workbook(tmp_path / 'export.xlsx', {'customers': [['customer_id', 'name'], [1, 'Ann']],
                                        'orders': [['order_id', 'customer_id'], [10, 1]]})
    model, _ = read_files(collect_data_files(tmp_path), root=tmp_path)
    assert links(model, NAMES) == {('orders', 'customer_id', 'customers', 'customer_id')}
    assert {t.id for t in model.tables} == {'export.customers', 'export.orders'}


def test_workbook_tables_through_the_api(client, tmp_path):
    workbook(tmp_path / 'north.xlsx', {'Sheet1': [['order_id'], [1]], 'Sheet2': [['x'], [1]]})
    workbook(tmp_path / 'south.xlsx', {'Sheet1': [['order_id'], [2]], 'Sheet2': [['y'], [1]]})
    ws = client.post('/api/workspaces', json={'name': 'books'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(tmp_path)}).status_code == 200
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert sorted(t['id'] for t in model['tables']) == ['north.Sheet1', 'north.Sheet2', 'south.Sheet1', 'south.Sheet2']
    assert model['schemas'] == ['north', 'south']
    one = client.get(f'/api/workspaces/{ws}/tables/south.Sheet1').json()
    assert one['origins'][0]['path'] == 'south.xlsx' and one['origins'][0]['sheet'] == 'Sheet1'


def test_snapshots_saved_by_session_1_still_load():
    old = {'tables': [{'name': 'customers', 'schema': None, 'kind': 'table', 'aliases': [], 'comment': None,
                       'row_count': None, 'origins': [{'kind': 'file', 'source_id': 's', 'label': 'csv',
                                                       'detail': 'customers.csv'}],
                       'columns': [{'name': 'customer_id', 'type': 'BIGINT', 'origins': ['file'],
                                    'types': {'file': 'BIGINT'}}]}], 'relationships': []}
    origin = SchemaModel.from_dict(old).find('customers').origins[0]
    assert (origin.detail, origin.path, origin.sheet, origin.schema, origin.table) == ('customers.csv', None, None, None, None)


# ----------------------------------------------- A3: run.bat and a driver that fails

def test_run_bat_installs_the_core_first_and_each_driver_on_its_own():
    text = (ROOT / 'run.bat').read_bytes()
    assert b'\r\n' in text and b'\n' not in text.replace(b'\r\n', b'')          # CRLF, as cmd.exe needs
    text = text.decode('ascii')
    core, drivers = text.index('-r requirements.txt'), text.index('call :install_drivers')
    assert core < drivers < text.index('-m app %*')
    assert 'if errorlevel 1 goto :install_failed' in text                       # the core must install
    loop = re.search(r'for /f "[^"]*" %%D in \("requirements-drivers.txt"\) do call :install_driver "%%D"', text)
    assert loop, 'each driver line is installed by its own pip call'
    one = text[text.index(':install_driver\r\n'):text.index('\r\n:no_python')]
    assert 'pip install' in one and '%1' in one and 'WARNING' in one and 'driver not installed' in one
    assert 'goto :install_failed' not in one and one.rstrip().endswith('exit /b 0')   # a failure only warns

    core_file = (ROOT / 'requirements.txt').read_text()
    driver_file = (ROOT / 'requirements-drivers.txt').read_text()
    packages = [line.split()[0] for line in driver_file.splitlines() if line.strip() and not line.startswith('#')]
    assert [re.split(r'[<>=\[]', p)[0] for p in packages] == ['oracledb', 'pymssql', 'psycopg', 'PyMySQL']
    assert {database.ENGINES[e]['pip'] for e in ('oracle', 'mssql', 'postgresql', 'mysql')} == {
        re.split(r'[<>=]', p)[0] for p in packages}
    for name in ('oracledb', 'pymssql', 'psycopg', 'PyMySQL'):
        assert name.lower() not in core_file.lower()


def test_an_engine_without_its_driver_is_reported_not_fatal(client, monkeypatch):
    monkeypatch.setitem(database.ENGINES['mssql'], 'module', 'pymssql_is_not_installed')
    engines = {e['id']: e['installed'] for e in client.get('/api/meta').json()['engines']}
    assert engines == {'oracle': True, 'mssql': False, 'postgresql': True, 'mysql': True, 'sqlite': True}
    refused = client.post('/api/db/test', json={'engine': 'mssql', 'host': '127.0.0.1', 'database': 'x'})
    assert refused.status_code == 400 and 'driver is not installed' in refused.json()['detail']


# ------------------------------------------------- A4: read-only mode fails closed

def test_the_engines_that_have_a_read_only_mode_must_get_it():
    assert database.TRANSACTION_SETUP['postgresql'][0] == 'SET TRANSACTION READ ONLY'
    assert database.TRANSACTION_SETUP['oracle'] == ['SET TRANSACTION READ ONLY']
    assert database.SESSION_SETUP['mysql'] == ['SET SESSION TRANSACTION READ ONLY']
    for statement in ('SET TRANSACTION READ ONLY', 'SET SESSION TRANSACTION READ ONLY', 'PRAGMA query_only = ON'):
        assert database._is_read_only_setting(statement)
    assert not database._is_read_only_setting('SET LOCAL statement_timeout = 30000')


@pytest.mark.parametrize('where', ['TRANSACTION_SETUP', 'SESSION_SETUP'])
def test_a_refused_read_only_setting_refuses_the_connection(demo_db, monkeypatch, where):
    # SQLite does not know these statements, so it plays an engine that refuses read-only mode:
    # at the start of the transaction (PostgreSQL, Oracle) and when the session opens (MySQL).
    statement = 'SET TRANSACTION READ ONLY' if where == 'TRANSACTION_SETUP' else 'SET SESSION TRANSACTION READ ONLY'
    monkeypatch.setitem(getattr(database, where), 'sqlite', [statement])
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    reached = []
    with pytest.raises(ReadOnlyRefused, match='refused read-only mode.*connection was closed without being used'):
        with db.connect() as conn:
            reached.append(conn.exec_driver_sql('SELECT COUNT(*) FROM customers').scalar())
    assert reached == [] and db.statements[-1] == statement          # nothing ran after the refusal
    assert not any(s.upper().startswith('SELECT') for s in db.statements)
    for call in (db.test, db.read_schema):
        with pytest.raises(ReadOnlyRefused):
            call()
    db.dispose()


def test_a_refused_read_only_setting_is_a_clear_error_in_the_api(client, demo_db, monkeypatch):
    monkeypatch.setitem(database.TRANSACTION_SETUP, 'sqlite', ['SET TRANSACTION READ ONLY'])
    ws = client.post('/api/workspaces', json={'name': 'x'}).json()['id']
    for response in (client.post('/api/db/test', json={'engine': 'sqlite', 'database': str(demo_db)}),
                     client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(demo_db)})):
        assert response.status_code == 400
        assert 'SQLite refused read-only mode, so the connection was closed without being used' in response.json()['detail']
    assert client.get(f'/api/workspaces/{ws}').json()['sources'] == []


def test_a_failed_timeout_setting_does_not_cost_the_read_only_mode(demo_db, monkeypatch):
    # The optional setting fails and ends the transaction: read-only mode must be applied again.
    monkeypatch.setitem(database.TRANSACTION_SETUP, 'sqlite', ['PRAGMA query_only = ON', 'SET LOCAL statement_timeout = {ms}'])
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    with db.connect() as conn:
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM plans').scalar() == 10
    assert db.statements[-4:] == ['PRAGMA query_only = ON', 'SET LOCAL statement_timeout = 30000',
                                  'PRAGMA query_only = ON', 'SELECT COUNT(*) FROM plans']
    assert 'read-only transaction' in db.protections
    db.dispose()


def test_blocked_statements_are_recorded(demo_db):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    db.read_schema()
    assert db.blocked == []                                          # the catalog reads all pass
    with pytest.raises(Exception):
        with db.connect() as conn:
            conn.exec_driver_sql('DELETE FROM plans')
    assert db.blocked == ['DELETE FROM plans'] and 'DELETE FROM plans' not in db.statements
    db.dispose()


# --------------------------------------------------------- A5: very large PNG exports

def test_png_resolution_respects_both_limits():
    assert choose_dpi(2000, 1000) == 200                                         # the scripts' resolution
    assert choose_dpi(2000, 1000, 60_000_000, 65_000) == 200
    assert choose_dpi(10_000, 10_000, 60_000_000, 65_000) == 77                  # total pixels
    assert choose_dpi(500, 40_000, 60_000_000, 65_000) == 162                    # 173 for the pixels, 162 for the side
    assert choose_dpi(300, 50_000, 600_000_000, 65_000) == 130                   # the side alone
    for width, height in ((500, 40_000), (300, 50_000), (10_000, 10_000), (160_000, 600)):
        dpi = choose_dpi(width, height, 60_000_000, 65_000)
        assert max(width, height) * dpi / 100 <= 65_000 and width * height * (dpi / 100) ** 2 <= 60_000_000
    with pytest.raises(DiagramTooLarge):
        choose_dpi(400, 170_000, 60_000_000, 65_000)                             # would need less than 40 dpi
    with pytest.raises(DiagramTooLarge):
        choose_dpi(40_000, 40_000, 60_000_000, 65_000)


def wide_table_ddl(columns):
    return ('CREATE TABLE wide (' + ', '.join(f'c{i} INT' for i in range(columns)) + ');').encode()


def test_a_very_tall_png_is_scaled_to_fit_matplotlibs_side_limit(client):
    ws = client.post('/api/workspaces', json={'name': 'tall'}).json()['id']
    client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('w.sql', wide_table_ddl(1400), 'text/plain'))])
    png = client.get(f'/api/workspaces/{ws}/export?format=png&all_columns=true')    # 67,000 px tall at 200 dpi
    assert png.status_code == 200 and png.content[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('>II', png.content[16:24])
    assert 60_000 < height <= config.PNG_MAX_SIDE < 65_536


def test_a_png_that_cannot_fit_is_a_400_with_advice(client, monkeypatch):
    ws = client.post('/api/workspaces', json={'name': 'huge'}).json()['id']
    client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('w.sql', wide_table_ddl(300), 'text/plain'))])
    monkeypatch.setattr(config, 'PNG_MAX_SIDE', 2000)               # as if the diagram were 30 times larger
    refused = client.get(f'/api/workspaces/{ws}/export?format=png&all_columns=true')
    assert refused.status_code == 400
    detail = refused.json()['detail']
    assert 'too big for a PNG' in detail and '.drawio' in detail and 'focus mode' in detail
    assert client.get(f'/api/workspaces/{ws}/export?format=drawio').status_code == 200      # the way out works
    assert client.get(f'/api/workspaces/{ws}/export?format=png').status_code == 200         # key columns only: small


# ------------------------------------------------ A6: a new kind of relationship

def test_a_new_kind_only_needs_an_entry_in_rank(client, monkeypatch):
    # Session 3 added 'values' exactly this way. A kind nobody has thought of yet shows it still works.
    assert model_module.RANK == {'names': 1, 'values': 1.5, 'joins': 2, 'declared': 3}
    monkeypatch.setitem(model_module.RANK, 'lineage', 1.2)
    kinds = [k['id'] for k in relationship_kinds()]
    assert kinds == ['declared', 'joins', 'values', 'lineage', 'names']
    assert [k['id'] for k in client.get('/api/meta').json()['relationship_kinds']] == kinds
    lineage = next(k for k in relationship_kinds() if k['id'] == 'lineage')
    assert lineage == {'id': 'lineage', 'rank': 1.2, 'inferred': True, 'label': 'from lineage', 'short': 'Lineage', 'hint': ''}
    values = next(k for k in relationship_kinds() if k['id'] == 'values')
    assert (values['rank'], values['inferred'], values['label'], values['short']) == (1.5, True, 'from values', 'Values')

    m = SchemaModel()
    a, b, c = table(m, 'orders', ['id', 'cust']), table(m, 'customers', ['id']), table(m, 'visits', ['id', 'customer_id'])
    r = m.relate(a, ['cust'], b, ['id'], 'lineage', 0.9, '1-N', evidence=['97% of the values match'])
    assert m.relate(c, ['customer_id'], b, ['id'], NAMES, 0.8, '1-N').source == NAMES
    assert m.relate(c, ['customer_id'], b, ['id'], 'lineage', 0.95, '1-N').source == 'lineage'    # outranks names
    assert m.relate(c, ['customer_id'], b, ['id'], 'values', 0.95, '1-N').source == 'values'      # values outrank it
    assert m.relate(c, ['customer_id'], b, ['id'], JOINS, 0.85, '1-N').source == JOINS            # joins outrank both
    finalize_model(m)
    assert m.stats() == {'tables': 3, 'columns': 5, 'relationships': 2, 'declared': 0, 'joins': 1, 'values': 0,
                         'lineage': 1, 'names': 0}
    assert (r.source, r.confidence, r.seen) == ('lineage', 0.9, ['lineage'])
    assert m.relationships[1].seen == [JOINS, 'values', 'lineage', NAMES]
    assert len(m.view(kinds={'lineage'})[1]) == 1 and len(m.view(kinds={'lineage'}, min_confidence=0.95)[1]) == 0
    again = SchemaModel.from_dict(json.loads(json.dumps(m.to_dict())))                # survives the snapshot
    assert again.stats() == m.stats() and again.relationships[0].source == 'lineage'

    # through the API: the stats, the "kinds" filter of the export and both renderers accept it
    store = client.app.state.workspaces
    ws = client.post('/api/workspaces', json={'name': 'v'}).json()['id']
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('s1', ws, 'sql_files', 'x', '{}', json.dumps(m.to_dict()), '[]', '2026-10-12', '2026-10-12'))
    data = client.get(f'/api/workspaces/{ws}/model').json()
    assert data['stats']['lineage'] == 1 and data['relationships'][0]['source'] == 'lineage'
    only = client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=lineage')
    assert only.status_code == 200 and only.text.count('edge="1"') == 1 and 'dashed=1' in only.text
    assert client.get(f'/api/workspaces/{ws}/export?format=drawio&kinds=joins').text.count('edge="1"') == 1
    assert client.get(f'/api/workspaces/{ws}/export?format=png').status_code == 200
    assert client.get(f'/api/workspaces/{ws}/tables/orders').json()['relationships'][0]['source'] == 'lineage'


def test_the_page_builds_its_kind_checkboxes_from_the_server():
    script = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
    page = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    assert 'relationship_kinds' in script and 'id="kind-toggles"' in page
    assert 'kind-joins' not in page and "['declared', 'joins', 'names']" not in script
    assert 's.joins + s.names' not in script


# ----------------------------------------------------------------- A7: small items

def test_uploads_over_the_limit_are_refused_with_a_clear_message(client, app, monkeypatch, tmp_path):
    assert config.MAX_UPLOAD_BYTES == 500 * 1024 * 1024 and client.get('/api/meta').json()['max_upload_mb'] == 500
    monkeypatch.setattr(config, 'MAX_UPLOAD_BYTES', 4096)
    ws = client.post('/api/workspaces', json={'name': 'u'}).json()['id']
    big = b'id,name\n' + b'1,abcdefghij\n' * 1000                    # about 13 KB
    refused = client.post(f'/api/workspaces/{ws}/sources/files', files=[('files', ('big.csv', big, 'text/csv'))])
    assert refused.status_code == 413
    assert 'larger than the limit' in refused.json()['detail'] and 'file or folder path' in refused.json()['detail']
    assert refused.headers['content-security-policy']                # the usual headers are still there
    assert client.get(f'/api/workspaces/{ws}').json()['sources'] == []
    uploads = tmp_path / 'data' / 'uploads'
    assert not uploads.exists() or not any(p.is_file() for p in uploads.rglob('*'))
    small = client.post(f'/api/workspaces/{ws}/sources/files', files=[('files', ('ok.csv', b'id,name\n1,a\n', 'text/csv'))])
    assert small.status_code == 200 and small.json()['tables'] == 1


def test_the_limit_also_stops_an_upload_that_does_not_declare_its_size(client, monkeypatch):
    monkeypatch.setattr(config, 'MAX_UPLOAD_BYTES', 4096)
    ws = client.post('/api/workspaces', json={'name': 'u'}).json()['id']

    def chunks():                                                    # chunked: no Content-Length header
        yield (b'--x\r\nContent-Disposition: form-data; name="files"; filename="big.csv"\r\n'
               b'Content-Type: text/csv\r\n\r\nid,name\n')
        for _ in range(40):
            yield b'1,abcdefghij\n' * 80
        yield b'\r\n--x--\r\n'
    refused = client.post(f'/api/workspaces/{ws}/sources/files', content=chunks(),
                          headers={'content-type': 'multipart/form-data; boundary=x'})
    assert refused.status_code == 413 and 'limit of 1 MB' in refused.json()['detail']
    assert client.get(f'/api/workspaces/{ws}').json()['sources'] == []


def test_forget_password(client, app, demo_db):
    ws = client.post('/api/workspaces', json={'name': 'p'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={
        'engine': 'sqlite', 'database': str(demo_db), 'password': 'Sup3r-S3cret'}).json()
    assert source['has_password'] is True and app.state.workspaces._passwords == {(ws, source['id']): 'Sup3r-S3cret'}
    forgotten = client.delete(f'/api/workspaces/{ws}/sources/{source["id"]}/password')
    assert forgotten.status_code == 200 and forgotten.json()['has_password'] is False
    assert app.state.workspaces._passwords == {}
    assert client.get(f'/api/workspaces/{ws}').json()['sources'][0]['has_password'] is False
    assert client.delete(f'/api/workspaces/{ws}/sources/{source["id"]}/password').status_code == 200    # twice is fine
    assert client.delete(f'/api/workspaces/{ws}/sources/nope/password').status_code == 404
    assert len(client.get(f'/api/workspaces/{ws}').json()['sources']) == 1                              # the source stays


def test_a_forgotten_password_is_asked_for_again(client, app):
    store = app.state.workspaces
    ws = client.post('/api/workspaces', json={'name': 'p'}).json()['id']
    settings = {'engine': 'postgresql', 'host': '127.0.0.1', 'port': 9, 'database': 'crm', 'username': 'reader',
                'schema_filter': '', 'include_views': False}
    with store._db() as con:
        con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, warnings, '
                    'created_at, refreshed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)',
                    ('src1', ws, 'database', 'crm', json.dumps(settings), '{"tables": [], "relationships": []}', '[]',
                     '2026-10-12', '2026-10-12'))
    store._passwords[(ws, 'src1')] = 'Sup3r-S3cret'
    assert client.get(f'/api/workspaces/{ws}').json()['sources'][0]['needs_password'] is False
    assert client.delete(f'/api/workspaces/{ws}/sources/src1/password').json()['needs_password'] is True
    again = client.post(f'/api/workspaces/{ws}/sources/src1/refresh', json={})
    assert again.status_code == 409 and again.json()['needs_password'] is True


def test_readme_says_what_the_guard_does_and_does_not_see():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    assert 'Every statement passes a guard' not in readme
    assert 'Every statement the app sends' in readme and 'SET NAMES' in readme
    assert 'not seen by the guard' in readme
