"""The Scripts tab's backend (app/analysis/scripts.py): a scan of a scripts folder with the SQL Lineage
engine, against the workspace model. Not to be confused with tests/test_lineage.py, which is about the
SQL tab's column origins for masking.

The scripts are SQL Lineage's own test scripts (tests/fixtures/zephyr_scripts, copied unchanged), and the
workspace's source is the Zephyr fixture database. The main test is a parity test: the tab must give what
the engine gives when it is run directly with --schema on the same database file."""

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.analysis import scripts as scripts_module
from app.analysis.scripts import DIALECT_OF_ENGINE, Scripts, schema_tables
from app.main import create_app
from conftest import local_client

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SCRIPTS = FIXTURES / 'zephyr_scripts'


def wait(client, job):
    assert 'id' in job, job
    for _ in range(3000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def folder_state(folder):
    """{relative path: sha256} of every file under a folder."""
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).rglob('*')) if p.is_file()}


def as_json(value):
    return json.loads(json.dumps(value))


@pytest.fixture(scope='module')
def zephyr_db(tmp_path_factory):
    path = tmp_path_factory.mktemp('zephyr-db') / 'zephyr.db'
    con = sqlite3.connect(path)
    con.executescript((FIXTURES / 'zephyr' / 'schema.sql').read_text(encoding='utf-8'))
    con.executescript((FIXTURES / 'zephyr' / 'seed.sql').read_text(encoding='utf-8'))
    con.commit()
    con.close()
    return path


def new_workspace(client, zephyr_db, name='Zephyr scripts'):
    ws = client.post('/api/workspaces', json={'name': name}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(zephyr_db)})
    assert source.status_code == 200, source.text
    return ws


def scan(client, ws, folder=SCRIPTS, dialect='sqlite'):
    response = client.post(f'/api/workspaces/{ws}/scripts/scan', json={'folder': str(folder), 'dialect': dialect})
    assert response.status_code == 200, response.text
    return wait(client, response.json())


@pytest.fixture(scope='module')
def scanned(tmp_path_factory, zephyr_db):
    """One app and one workspace with a finished scan of the fixture scripts."""
    data = tmp_path_factory.mktemp('scripts-data')
    with local_client(create_app(data)) as client:
        ws = new_workspace(client, zephyr_db)
        before = folder_state(SCRIPTS)
        job = scan(client, ws)
        yield client, ws, job, data, before


# -------------------------------------------------------------------- parity

def engine_run_directly(zephyr_db, out):
    """What the command line gives: scan --dialect sqlite --schema zephyr.db, then lineage.db read back."""
    from sql_lineage import cli
    from sql_lineage.analysis import analyze
    from sql_lineage.graph import load_graph
    assert cli.main(['scan', str(SCRIPTS), '--dialect', 'sqlite', '--schema', str(zephyr_db), '--out', str(out)]) == 0
    graph = load_graph(out / 'lineage.db')
    return graph, analyze(graph)


def test_the_tab_gives_what_the_engine_gives_with_the_same_database_as_schema(scanned, zephyr_db, tmp_path, capsys):
    client, ws, job, _, _ = scanned
    graph, result = engine_run_directly(zephyr_db, tmp_path / 'out')
    capsys.readouterr()
    tab = client.get(f'/api/workspaces/{ws}/scripts').json()
    for name in ('summary', 'catalog', 'conflicts', 'groups', 'subquery_links', 'joins_without_key', 'unreadable'):
        assert tab[name] == as_json(result[name]), name
    # Every table and column of the scan has the same impact.
    from sql_lineage.analysis import impact
    targets = [t.key for t in graph.tables] + [c.key for c in graph.columns if c.name != '*']
    assert tab['targets'] == sorted(t.key for t in graph.tables) + sorted(c.key for c in graph.columns if c.name != '*')
    for target in targets:
        response = client.get(f'/api/workspaces/{ws}/scripts/impact', params={'name': target})
        assert response.status_code == 200, target
        assert response.json() == as_json(impact(graph, target)), target
    # The numbers themselves, so a change that breaks both sides alike still shows.
    assert tab['summary']['files'] == 11 and tab['summary']['joins_without_key'] is not None
    assert (job['state'], job['done'], job['total']) == ('done', 11, 11)


def test_the_app_runs_its_own_copy_of_the_engine():
    import sql_lineage
    assert Path(sql_lineage.__file__).resolve().parent == Path(scripts_module.__file__).resolve().parent.parent / 'sql_lineage'


# ----------------------------------------------------------------- the scan

def test_the_scan_is_saved_in_the_data_folder_and_the_scripts_are_only_read(scanned):
    client, ws, job, data, before = scanned
    assert folder_state(SCRIPTS) == before
    assert sorted(p.relative_to(data).as_posix() for p in data.rglob('*') if p.is_file()) == [
        'explorer.db', f'lineage/{ws}.db']
    con = sqlite3.connect(data / 'explorer.db')
    con.row_factory = sqlite3.Row
    assert con.execute('PRAGMA user_version').fetchone()[0] == 4
    row = dict(con.execute('SELECT * FROM script_scans WHERE workspace_id = ?', (ws,)).fetchone())
    con.close()
    assert (row['folder'], row['dialect'], row['lineage_file']) == (str(SCRIPTS), 'sqlite', f'lineage/{ws}.db')
    assert json.loads(row['summary'])['statements'] == 31
    tab = client.get(f'/api/workspaces/{ws}/scripts').json()
    assert tab['scan']['folder'] == str(SCRIPTS) and tab['scan']['scanned_at'] == row['scanned_at']
    assert tab['scan']['schema'] == 'the workspace model (17 tables, 21 declared keys)'
    assert tab['job']['state'] == 'done'


def test_tables_of_the_model_are_named_for_the_app(scanned):
    client, ws, _, _, _ = scanned
    in_model = client.get(f'/api/workspaces/{ws}/scripts').json()['in_model']
    assert in_model['ORDER_ITEM'] == 'order_item' and in_model['CUSTOMER'] == 'customer'
    assert 'MART_ORDER_TOTALS' not in in_model          # a table only the scripts make


def test_the_dialect_is_preselected_from_the_one_database_source(scanned, zephyr_db, tmp_path):
    client, ws, _, _, _ = scanned
    assert client.get(f'/api/workspaces/{ws}/scripts').json()['default_dialect'] == 'sqlite'
    empty = client.post('/api/workspaces', json={'name': 'none'}).json()['id']
    assert client.get(f'/api/workspaces/{empty}/scripts').json()['default_dialect'] is None
    two = new_workspace(client, zephyr_db, 'two')
    client.post(f'/api/workspaces/{two}/sources/db', json={'engine': 'sqlite', 'database': str(zephyr_db)})
    assert client.get(f'/api/workspaces/{two}/scripts').json()['default_dialect'] is None
    assert DIALECT_OF_ENGINE == {'oracle': 'oracle', 'mssql': 'tsql', 'postgresql': 'postgres', 'mysql': 'mysql',
                                 'sqlite': 'sqlite'}
    for engine, dialect in DIALECT_OF_ENGINE.items():
        workspace = {'sources': [{'kind': 'sql_files', 'settings': {}}, {'kind': 'database', 'settings': {'engine': engine}}]}
        assert Scripts.default_dialect(None, workspace) == dialect


def test_the_schema_holds_declared_keys_only(client, tmp_path):
    ddl = tmp_path / 'model.sql'
    ddl.write_text('CREATE TABLE customer (customer_id INTEGER PRIMARY KEY, name TEXT);\n'
                   'CREATE TABLE orders (order_id INTEGER PRIMARY KEY, shop_id INTEGER,\n'
                   '  customer_id INTEGER REFERENCES customer (customer_id));\n'
                   'CREATE TABLE shop (shop_id INTEGER PRIMARY KEY);\n'
                   'CREATE TABLE note (note_id INTEGER PRIMARY KEY, order_id INTEGER);\n'
                   'SELECT o.order_id FROM orders o JOIN shop s ON s.shop_id = o.shop_id;\n', encoding='utf-8')
    ws = client.post('/api/workspaces', json={'name': 'keys'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(ddl)}).status_code == 200
    model = client.app.state.workspaces.model(ws)
    model.relate(model.find('note'), ['note_id'], model.find('customer'), ['customer_id'], 'values')   # an accepted value link
    kinds = sorted(r.source for r in model.relationships)
    assert 'declared' in kinds and len(kinds) > 1 and 'values' in kinds
    tables = {t['name']: t for t in schema_tables(model)}
    assert [fk for t in tables.values() for fk in t['foreign_keys']] == [
        {'columns': ['customer_id'], 'ref_table': 'customer', 'ref_schema': '', 'ref_columns': ['customer_id']}]
    assert tables['orders']['primary_key'] == ['order_id']
    assert tables['orders']['columns'] == ['order_id', 'shop_id', 'customer_id']


def test_a_workspace_without_tables_scans_without_a_schema(client):
    ws = client.post('/api/workspaces', json={'name': 'no tables'}).json()['id']
    assert scan(client, ws)['state'] == 'done'
    tab = client.get(f'/api/workspaces/{ws}/scripts').json()
    assert tab['scan']['schema'] is None and tab['joins_without_key'] is None
    assert tab['summary']['joins_without_key'] is None and tab['in_model'] == {}


def test_a_new_scan_replaces_the_last_one(client, zephyr_db, tmp_path):
    ws = new_workspace(client, zephyr_db)
    assert scan(client, ws)['state'] == 'done'
    one = tmp_path / 'one'
    one.mkdir()
    (one / 'q.sql').write_text('SELECT c.name FROM customer c JOIN sales_order o ON o.customer_id = c.customer_id;\n')
    assert scan(client, ws, one)['state'] == 'done'
    tab = client.get(f'/api/workspaces/{ws}/scripts').json()
    assert (tab['scan']['folder'], tab['summary']['files'], tab['summary']['joins']) == (str(one), 1, 1)
    con = sqlite3.connect(client.app.state.workspaces.db_path)
    assert con.execute('SELECT COUNT(*) FROM script_scans').fetchone()[0] == 1
    con.close()


# ------------------------------------------------------- refused and stopped

def test_a_missing_folder_a_file_or_a_wrong_dialect_is_refused(client, tmp_path):
    ws = client.post('/api/workspaces', json={'name': 'refused'}).json()['id']
    (tmp_path / 'a.sql').write_text('SELECT 1;')
    cases = [({'folder': str(tmp_path / 'nowhere'), 'dialect': 'sqlite'}, 'does not exist'),
             ({'folder': str(tmp_path / 'a.sql'), 'dialect': 'sqlite'}, 'is not a folder'),
             ({'folder': '', 'dialect': 'sqlite'}, 'Enter the path of a folder'),
             ({'folder': str(SCRIPTS), 'dialect': 'db2'}, 'Choose the dialect'),
             ({'folder': str(tmp_path), 'dialect': 'sqlite'}, "holds the app's data folder")]
    for body, message in cases:
        response = client.post(f'/api/workspaces/{ws}/scripts/scan', json=body)
        assert response.status_code == 400 and message in response.json()['detail'], (body, response.text)
    assert client.post('/api/workspaces/nope/scripts/scan', json={'folder': str(SCRIPTS), 'dialect': 'sqlite'}).status_code == 404
    assert client.get(f'/api/workspaces/{ws}/scripts').json()['scan'] is None
    assert not (tmp_path / 'data' / 'lineage').exists()


def test_more_scripts_than_the_engine_allows_fail_the_job(client, monkeypatch):
    monkeypatch.setattr(scripts_module.engine_settings, 'MAX_FILES', 3)
    ws = client.post('/api/workspaces', json={'name': 'limit'}).json()['id']
    job = scan(client, ws)
    assert job['state'] == 'failed'
    assert job['error'] == f'{SCRIPTS} holds 11 script files, more than MAX_FILES = 3; scan a smaller folder, or raise MAX_FILES in settings.py'
    assert client.get(f'/api/workspaces/{ws}/scripts').json()['limits']['max_files'] == 3


@pytest.fixture
def held_scan(monkeypatch):
    """Scans wait after their first file until released: time to cancel them or to start another job."""
    started, release = threading.Event(), threading.Event()
    real = scripts_module.scan_folder

    def slow_scan(folder, dialect, schema=None, progress=None, default_schema=None):
        def held(done, total, path):
            started.set()
            release.wait(10)
            progress(done, total, path)
        return real(folder, dialect, schema=schema, progress=held, default_schema=default_schema)

    monkeypatch.setattr(scripts_module, 'scan_folder', slow_scan)
    yield started, release
    release.set()


def test_cancel_stops_the_scan_and_saves_nothing(client, held_scan, tmp_path):
    started, release = held_scan
    ws = client.post('/api/workspaces', json={'name': 'cancel'}).json()['id']
    job = client.post(f'/api/workspaces/{ws}/scripts/scan', json={'folder': str(SCRIPTS), 'dialect': 'sqlite'}).json()
    assert started.wait(10)
    assert client.delete(f'/api/jobs/{job["id"]}').json()['cancel_requested'] is True
    release.set()
    done = wait(client, job)
    assert (done['state'], done['done']) == ('cancelled', 0)
    assert client.get(f'/api/workspaces/{ws}/scripts').json()['scan'] is None
    assert not (tmp_path / 'data' / 'lineage' / f'{ws}.db').exists()


def test_one_job_per_workspace(client, held_scan, zephyr_db):
    started, release = held_scan
    ws = new_workspace(client, zephyr_db, 'busy')
    job = client.post(f'/api/workspaces/{ws}/scripts/scan', json={'folder': str(SCRIPTS), 'dialect': 'sqlite'}).json()
    assert started.wait(10)
    again = client.post(f'/api/workspaces/{ws}/scripts/scan', json={'folder': str(SCRIPTS), 'dialect': 'sqlite'})
    assert again.status_code == 409 and again.json()['detail'] == 'A scan of scripts is already running in this workspace.'
    profile = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'})
    assert profile.status_code == 409 and profile.json()['job']['kind'] == 'scripts'
    forget = client.delete(f'/api/workspaces/{ws}/scripts')
    assert forget.status_code == 409
    release.set()
    assert wait(client, job)['state'] == 'done'


# ------------------------------------------------------- forget and delete

def test_forget_scan_deletes_the_row_and_the_file(client, zephyr_db, tmp_path):
    ws = new_workspace(client, zephyr_db)
    scan(client, ws)
    path = tmp_path / 'data' / 'lineage' / f'{ws}.db'
    assert path.is_file()
    assert client.delete(f'/api/workspaces/{ws}/scripts').json() == {'forgotten': True}
    assert not path.exists()
    assert client.get(f'/api/workspaces/{ws}/scripts').json()['scan'] is None
    for url in (f'/api/workspaces/{ws}/scripts/impact?name=CUSTOMER', f'/api/workspaces/{ws}/scripts/report'):
        response = client.get(url)
        assert response.status_code == 404 and 'no scan of scripts yet' in response.json()['detail']
    assert client.delete(f'/api/workspaces/{ws}/scripts').json() == {'forgotten': False}


def test_deleting_the_workspace_deletes_its_scan(client, zephyr_db, tmp_path):
    ws = new_workspace(client, zephyr_db)
    scan(client, ws)
    path = tmp_path / 'data' / 'lineage' / f'{ws}.db'
    assert path.is_file()
    assert client.delete(f'/api/workspaces/{ws}').status_code == 200
    assert not path.exists()
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    assert con.execute('SELECT COUNT(*) FROM script_scans').fetchone()[0] == 0
    con.close()


def test_an_unknown_impact_name_is_404_with_suggestions(scanned):
    client, ws, _, _, _ = scanned
    response = client.get(f'/api/workspaces/{ws}/scripts/impact', params={'name': 'ORDER'})
    assert response.status_code == 404
    assert response.json()['detail'].startswith('ORDER is not a table or column of this scan. Did you mean: ')
    assert 'ORDER_ITEM' in response.json()['detail']
    assert client.get(f'/api/workspaces/{ws}/scripts/impact', params={'name': 'order_item.order_id'}).json()['target'] == \
        'ORDER_ITEM.ORDER_ID'


# --------------------------------------------------------------- the report

def test_the_report_downloads_as_one_offline_file(scanned):
    client, ws, _, data, _ = scanned
    response = client.get(f'/api/workspaces/{ws}/scripts/report')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'text/html; charset=utf-8'
    assert response.headers['content-disposition'] == 'attachment; filename="Zephyr_scripts-scripts.html"'
    html = response.text
    assert html.startswith('<!doctype html>') and 'id="lineage-data"' in html
    for tag in re.findall(r'<(?:script|link|img|iframe)\b[^>]*>', html, flags=re.I):
        assert not re.search(r'(src|href)\s*=\s*["\']?\s*https?://', tag, flags=re.I), tag
    assert not re.search(r'<script[^>]*\bsrc\s*=', html, flags=re.I) and not re.search(r'<link\b', html, flags=re.I)
    assert not re.search(r'@import|url\(\s*[\'"]?https?:', html, flags=re.I)
    assert sorted(p.name for p in data.rglob('*') if p.is_file()) == sorted(['explorer.db', f'{ws}.db'])
