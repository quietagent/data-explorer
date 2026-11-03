"""The Scripts tab when the model's tables carry a schema, as a database source gives them (an Oracle owner),
and the scripts leave it out (round 4): the source's default schema goes to the scan, every name is found,
and the note about a source read without its views.

The scripts are tests/fixtures/scripts_without_owner: three small Oracle scripts on tables of the demo model,
written without their owner. The last test reads them against the Docker Oracle (skipped when it is not
running)."""

import time
from pathlib import Path

import pytest

from app.analysis.scripts import Scripts
from app.model import DECLARED, Origin, SchemaModel
from app.workspace import Workspaces
from scripts import live_engines as le

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
WITHOUT_OWNER = FIXTURES / 'scripts_without_owner'
# The tables the fixture scripts name: columns (the first is the primary key) and declared foreign keys.
TABLES = {
    'BRANCHES': (['BRANCH_ID', 'BRANCH_NAME', 'CITY'], []),
    'CUSTOMERS': (['CUSTOMER_ID', 'FULL_NAME', 'BRANCH_ID'], [('BRANCH_ID', 'BRANCHES')]),
    'ACCOUNTS': (['ACCOUNT_ID', 'CUSTOMER_ID', 'ACCOUNT_NUMBER'], [('CUSTOMER_ID', 'CUSTOMERS')]),
    'INVOICES': (['INVOICE_ID', 'ACCOUNT_ID', 'TOTAL_AMOUNT'], [('ACCOUNT_ID', 'ACCOUNTS')]),
    'PAYMENTS': (['PAYMENT_ID', 'INVOICE_ID', 'AMOUNT'], [('INVOICE_ID', 'INVOICES')]),
    'PLANS': (['PLAN_ID', 'PLAN_NAME', 'MONTHLY_FEE'], []),
    'SUBSCRIPTIONS': (['SUBSCRIPTION_ID', 'ACCOUNT_ID', 'PLAN_ID', 'MSISDN', 'STATUS'],
                      [('ACCOUNT_ID', 'ACCOUNTS'), ('PLAN_ID', 'PLANS')]),
}
SOURCE = {'engine': 'oracle', 'host': '127.0.0.1', 'port': 1521, 'database': 'TESTPDB', 'username': 'reader',
          'password': 'not-a-real-password', 'label': 'Owner test'}


def owner_model(default_schema='READER', owner='SALES'):
    """A model as an Oracle source gives it: every table carries its owner."""
    model = SchemaModel()
    for name, (columns, _) in TABLES.items():
        table = model.table(name, owner)
        table.add_origin(Origin('db', 'src', 'Owner test', owner, schema=owner, table=name))
        for column in columns:
            table.add_column(column, 'db', pk=column == columns[0])
    for name, (_, keys) in TABLES.items():
        for column, target in keys:
            model.relate(model.find(f'{owner}.{name}'), [column], model.find(f'{owner}.{target}'),
                         [TABLES[target][0][0]], DECLARED, cardinality='1-N')
    model.default_schema = default_schema
    return model


@pytest.fixture
def owner_source(monkeypatch):
    """Adding or reloading a database source gives owner_model() (no server is asked)."""
    made = {'default_schema': 'READER'}
    monkeypatch.setattr(Workspaces, '_read', staticmethod(
        lambda kind, settings, password, label, source_id: (owner_model(made['default_schema']), [])))
    return made


def wait(client, job):
    assert 'id' in job, job
    for _ in range(3000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def scan(client, ws, folder=WITHOUT_OWNER, dialect='oracle'):
    response = client.post(f'/api/workspaces/{ws}/scripts/scan', json={'folder': str(folder), 'dialect': dialect})
    assert response.status_code == 200, response.text
    job = wait(client, response.json())
    assert job['state'] == 'done', job
    return client.get(f'/api/workspaces/{ws}/scripts').json()


def write(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding='utf-8')
    return folder


# ------------------------------------------------------- names without their owner

def test_names_written_without_their_owner_are_found_in_the_model(client, owner_source):
    ws = client.post('/api/workspaces', json={'name': 'owners'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json=SOURCE)
    assert source.status_code == 200, source.text
    assert source.json()['default_schema'] == 'READER'        # kept with the source when it is read
    tab = scan(client, ws)
    assert tab['scan']['default_schema'] == 'READER'
    assert tab['summary']['unknown_names'] == 0 and tab['unknowns'] == []
    assert tab['summary']['tables'] == 7 and tab['summary']['joins'] == 4
    assert tab['joins_without_key'] == [] and all(row['declared_fk'] for row in tab['catalog'])
    # Every table of the scan is the model's, under its full key, and links to the app's table.
    assert tab['in_model'] == {f'SALES.{name}': f'SALES.{name}' for name in TABLES}
    for table_id in tab['in_model'].values():
        assert client.get(f'/api/workspaces/{ws}/tables/{table_id}').status_code == 200
    assert tab['views_note'] is None                          # every name is in the model


def test_the_default_schema_decides_a_name_that_two_owners_have(client, monkeypatch, tmp_path):
    both = owner_model('HR')
    other = owner_model(owner='HR')
    for table in other.tables:
        both.tables.append(table)
        both.by_key[table.key] = table
    monkeypatch.setattr(Workspaces, '_read', staticmethod(lambda *args: (both, [])))
    ws = client.post('/api/workspaces', json={'name': 'two owners'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json=SOURCE)
    assert source.status_code == 200
    folder = write(tmp_path / 'scripts', {'q.sql': 'SELECT plan_name FROM plans;\n'})
    tab = scan(client, ws, folder)
    assert tab['scan']['default_schema'] == 'HR'
    assert tab['in_model'] == {'HR.PLANS': 'HR.PLANS'} and tab['unknowns'] == []
    both.default_schema = None                                # no default schema: not certain, never guessed
    assert client.post(f'/api/workspaces/{ws}/sources/{source.json()["id"]}/refresh', json={}).status_code == 200
    tab = scan(client, ws, folder)
    assert tab['scan']['default_schema'] is None
    assert [(u['kind'], u['name'], u['reason']) for u in tab['unknowns']] == [
        ('table', 'PLANS', 'in 2 schemas: HR, SALES')]


def test_a_source_saved_before_has_no_default_schema_until_it_is_read_again(client, owner_source):
    owner_source['default_schema'] = None                     # as a snapshot saved before round 4
    ws = client.post('/api/workspaces', json={'name': 'old source'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json=SOURCE).json()
    assert source['default_schema'] is None
    tab = scan(client, ws)                                    # the one-owner rule alone still finds every table
    assert tab['scan']['default_schema'] is None and tab['summary']['unknown_names'] == 0
    owner_source['default_schema'] = 'READER'
    reloaded = client.post(f'/api/workspaces/{ws}/sources/{source["id"]}/refresh', json={})
    assert reloaded.status_code == 200 and reloaded.json()['default_schema'] == 'READER'
    assert scan(client, ws)['scan']['default_schema'] == 'READER'


def test_the_default_schema_is_the_one_database_sources_or_none():
    one = {'sources': [{'kind': 'sql_files'}, {'kind': 'database', 'default_schema': 'APP'}]}
    assert Scripts.default_schema(None, one) == 'APP'
    assert Scripts.default_schema(None, {'sources': [{'kind': 'sql_files'}]}) is None
    two = {'sources': [{'kind': 'database', 'default_schema': 'APP'}, {'kind': 'database', 'default_schema': 'HR'}]}
    assert Scripts.default_schema(None, two) is None
    assert Scripts.default_schema(None, {'sources': [{'kind': 'database'}]}) is None


def test_a_sqlite_source_keeps_no_default_schema(client, tmp_path):
    import sqlite3
    path = tmp_path / 'small.db'
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE t (id INTEGER PRIMARY KEY)')
    con.close()
    ws = client.post('/api/workspaces', json={'name': 'sqlite'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    assert source.status_code == 200 and source.json()['default_schema'] is None


# ------------------------------------------------------------------- the views note

NOTE = ('1 name is not in the workspace model. Owner test was read without views: if some of them are views, '
        'tick Include views on the source, reload it and scan again.')


def test_the_views_note_is_shown_for_missing_names_and_a_source_read_without_views(client, owner_source, tmp_path):
    ws = client.post('/api/workspaces', json={'name': 'views'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/db', json=SOURCE).status_code == 200
    folder = write(tmp_path / 'scripts', {'q.sql': 'SELECT v.msisdn FROM v_active_lines v;\n'
                                                   'SELECT plan_name FROM plans;\n'})
    assert scan(client, ws, folder)['views_note'] == NOTE
    two = write(tmp_path / 'two', {'q.sql': 'SELECT * FROM v_one;\nSELECT * FROM v_two;\n'})
    assert scan(client, ws, two)['views_note'].startswith('2 names are not in the workspace model. Owner test was')


def test_the_views_note_is_not_shown_otherwise(client, owner_source, tmp_path):
    folder = write(tmp_path / 'scripts', {'q.sql': 'SELECT v.msisdn FROM v_active_lines v;\n'})
    # A database source read with its views: the name is simply not in the database.
    ws = client.post('/api/workspaces', json={'name': 'with views'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/db', json={**SOURCE, 'include_views': True}).status_code == 200
    assert scan(client, ws, folder)['views_note'] is None
    # No database source: SQL files only.
    ddl = write(tmp_path / 'ddl', {'model.sql': 'CREATE TABLE plans (plan_id INTEGER PRIMARY KEY, plan_name TEXT);\n'})
    ws = client.post('/api/workspaces', json={'name': 'sql files'}).json()['id']
    assert client.post(f'/api/workspaces/{ws}/sources/sql', data={'path': str(ddl)}).status_code == 200
    assert scan(client, ws, folder)['views_note'] is None
    # No tables at all: no schema, nothing is "not in the model".
    ws = client.post('/api/workspaces', json={'name': 'empty'}).json()['id']
    assert scan(client, ws, folder)['views_note'] is None


def test_the_tab_shows_the_default_schema_and_the_views_note():
    script = (Path(__file__).resolve().parent.parent / 'static' / 'scripts.js').read_text(encoding='utf-8')
    assert 'default schema ${d.scan.default_schema}' in script
    assert "if (d.views_note) out.push(h('div', { class: 'callout', id: 'scripts-views-note', text: d.views_note }))" in script


# ------------------------------------------------------------ the Docker Oracle

@pytest.mark.live
def test_scripts_without_the_owner_find_every_table_on_the_docker_oracle(client):
    if not le.reachable('oracle'):
        pytest.skip('oracle: test container not running or not seeded (scripts\\live_db.ps1 up)')
    owner = le.ENGINES['oracle']['schema']
    ws = client.post('/api/workspaces', json={'name': 'oracle scripts'}).json()['id']
    body = {**le.reader_profile('oracle'), 'password': le.READER_PASSWORD, 'label': 'oracle live'}
    source = client.post(f'/api/workspaces/{ws}/sources/db', json=body)
    assert source.status_code == 200, source.text
    assert source.json()['default_schema'] == le.ENGINES['oracle']['reader']    # the account's own schema
    tab = scan(client, ws)
    assert tab['summary']['unknown_names'] == 0 and tab['unknowns'] == []
    assert sorted(tab['in_model']) == sorted(f'{owner}.{name}' for name in TABLES)
    assert all(row['declared_fk'] for row in tab['catalog']) and tab['joins_without_key'] == []
    assert tab['views_note'] is None
