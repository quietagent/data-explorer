"""Data-quality findings: one test per rule, on a database and on files."""

import sqlite3
import time

import pytest

from app import config
from app.profiling.quality import RULES, SEVERITIES


def wait(client, job):
    for _ in range(3000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def profile(client, ws):
    job = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    assert job['state'] == 'done' and job['errors'] == [], job
    return client.get(f'/api/workspaces/{ws}/quality').json()


def found(quality, rule=None, table=None):
    return [(f['severity'], f['table'], f['column'], f['rule']) for f in quality['findings']
            if (rule is None or f['rule'] == rule) and (table is None or f['table'] == table)]


def message(quality, table, column, rule):
    return next(f['message'] for f in quality['findings'] if (f['table'], f['column'], f['rule']) == (table, column, rule))


DDL = """
CREATE TABLE empty_table (id INTEGER PRIMARY KEY, label TEXT);
CREATE TABLE facts (
    fact_id INTEGER PRIMARY KEY, never_filled TEXT, always_same TEXT, same_or_empty TEXT,
    mostly_empty TEXT, nearly_empty TEXT, half_empty TEXT, fine TEXT);
CREATE TABLE log_lines (message TEXT, level TEXT);
CREATE TABLE orders (order_id INTEGER, product TEXT);
CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, product TEXT, PRIMARY KEY (order_id, line_no));
CREATE TABLE one_row (id INTEGER PRIMARY KEY, label TEXT);
INSERT INTO one_row VALUES (1, 'only');
"""


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Quality'}).json()['id']


@pytest.fixture
def database(client, ws, tmp_path):
    path = tmp_path / 'quality.db'
    con = sqlite3.connect(path)
    con.executescript(DDL)
    con.executemany('INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [
        (i, None, 'A', 'B' if i % 2 else None, 'x' if i % 5 == 0 else None, 'y' if i % 25 == 0 else None,
         'z' if i % 2 else None, f'value {i}') for i in range(1, 101)])
    con.executemany('INSERT INTO log_lines VALUES (?, ?)', [(f'line {i}', 'info' if i % 3 else 'warn') for i in range(30)])
    con.executemany('INSERT INTO orders VALUES (?, ?)', [(i if i <= 40 else i - 40, f'p{i}') for i in range(1, 51)])
    con.executemany('INSERT INTO order_items VALUES (?, ?, ?)', [(1 + i // 3, i % 3, 'p') for i in range(30)])
    con.commit()
    con.close()
    assert client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)}).status_code == 200
    return ws


@pytest.fixture
def files(client, ws, tmp_path):
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'customers.csv').write_text('customer_id,city\n' + ''.join(
        f'{i if i <= 45 else i - 45},Riyadh{i % 3}\n' for i in range(1, 51)))               # 5 ids repeat
    amounts = [f'{i * 1.5}' for i in range(1, 46)] + ['n/a', 'unknown', 'n/a', '-', 'tbc']   # 90% numbers
    dates = [f'2026-01-{1 + i % 28:02d}' for i in range(44)] + ['soon', 'never', 'soon', '?', '?', 'later']
    (folder / 'payments.csv').write_text('payment_id,amount,paid_on,reference\n' + ''.join(
        f'{i + 1},{amounts[i]},{dates[i]},REF-{i:03d}\n' for i in range(50)))
    (folder / 'notes.csv').write_text('text,tag\n' + ''.join(f'note {i},t{i % 4}\n' for i in range(20)))
    (folder / 'codes.csv').write_text('code_id,padded\n' + ''.join(f'{i},"00{i}"\n' for i in range(1, 21)))
    assert client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)}).status_code == 200
    return ws


# ------------------------------------------------------------------- the rules

def test_findings_have_the_agreed_shape(client, database):
    quality = profile(client, database)
    assert quality['severities'] == list(SEVERITIES) == ['high', 'medium', 'low']
    assert set(quality['rules']) == set(RULES) == {'empty_table', 'all_null', 'constant', 'high_null', 'no_primary_key',
                                                   'no_key_column', 'duplicate_key', 'mixed_types'}
    assert (quality['tables_total'], quality['tables_profiled'], quality['tables_with_data']) == (6, 6, 6)
    for finding in quality['findings']:
        assert set(finding) == {'severity', 'table', 'column', 'rule', 'message', 'sampled', 'sample_method'}
        assert finding['sample_method'] is None                    # nothing here was profiled on a sample
        assert finding['severity'] in SEVERITIES and finding['rule'] in RULES and finding['message'].endswith('.')
    order = [SEVERITIES.index(f['severity']) for f in quality['findings']]
    assert order == sorted(order)                                    # most severe first
    assert quality['summary'] == {s: order.count(i) for i, s in enumerate(SEVERITIES)}
    assert quality['summary']['high'] == 1 and sum(quality['summary'].values()) == len(quality['findings'])


def test_empty_table(client, database):
    quality = profile(client, database)
    assert found(quality, 'empty_table') == [('medium', 'empty_table', None, 'empty_table')]
    assert message(quality, 'empty_table', None, 'empty_table') == 'The table has no rows.'
    assert found(quality, table='empty_table') == [('medium', 'empty_table', None, 'empty_table')]   # nothing per column


def test_all_null_column(client, database):
    quality = profile(client, database)
    assert found(quality, 'all_null') == [('medium', 'facts', 'never_filled', 'all_null')]
    assert message(quality, 'facts', 'never_filled', 'all_null') == 'No row has a value (100 rows checked).'
    assert ('low', 'facts', 'never_filled', 'high_null') not in found(quality)       # said once, not twice


def test_constant_column(client, database):
    quality = profile(client, database)
    assert found(quality, 'constant') == [('low', 'facts', 'always_same', 'constant'),
                                         ('low', 'facts', 'half_empty', 'constant'),
                                         ('low', 'facts', 'mostly_empty', 'constant'),
                                         ('low', 'facts', 'nearly_empty', 'constant'),
                                         ('low', 'facts', 'same_or_empty', 'constant'),
                                         ('low', 'order_items', 'product', 'constant')]
    assert message(quality, 'facts', 'always_same', 'constant') == "Every row holds the same value: 'A'."
    assert message(quality, 'facts', 'same_or_empty', 'constant') == "Every row that has one holds the same value: 'B'."
    assert found(quality, 'constant', 'one_row') == []               # one row is not "constant"


def test_high_null_column_and_its_threshold(client, database, monkeypatch):
    quality = profile(client, database)
    assert found(quality, 'high_null') == [('medium', 'facts', 'nearly_empty', 'high_null'),     # 96%: medium
                                          ('low', 'facts', 'mostly_empty', 'high_null')]         # 80%
    assert message(quality, 'facts', 'mostly_empty', 'high_null') == '80% of the rows are empty (80 of 100).'
    assert quality['settings']['high_null_share'] == config.HIGH_NULL_SHARE == 0.5               # exactly half is not "more than"
    monkeypatch.setattr(config, 'HIGH_NULL_SHARE', 0.9)
    assert found(client.get(f'/api/workspaces/{database}/quality').json(), 'high_null') == [
        ('medium', 'facts', 'nearly_empty', 'high_null')]
    monkeypatch.setattr(config, 'HIGH_NULL_SHARE', 0.4)
    assert len(found(client.get(f'/api/workspaces/{database}/quality').json(), 'high_null')) == 4


def test_table_with_no_primary_key(client, database):
    quality = profile(client, database)
    # log_lines: nothing declared, nothing that looks like a key. orders: order_id is inferred, so it is fine.
    assert found(quality, 'no_primary_key') == [('medium', 'log_lines', None, 'no_primary_key')]
    model = {t['name']: t for t in client.get(f'/api/workspaces/{database}/model').json()['tables']}
    assert set(model) == {'empty_table', 'facts', 'log_lines', 'orders', 'order_items', 'one_row'}
    raw = client.get(f'/api/workspaces/{database}/tables/orders').json()
    assert [(c['name'], c['pk'], c['pk_inferred']) for c in raw['columns']][0] == ('order_id', False, True)


def test_no_primary_key_needs_no_data(client, ws):
    ddl = (b'CREATE TABLE audit_log (message VARCHAR(100), level VARCHAR(10));'
           b'CREATE TABLE customers (customer_id INT PRIMARY KEY, name VARCHAR(50));')
    client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('s.sql', ddl, 'text/plain'))])
    quality = client.get(f'/api/workspaces/{ws}/quality').json()                 # never profiled, SQL files only
    assert found(quality) == [('medium', 'audit_log', None, 'no_primary_key')]
    assert (quality['tables_profiled'], quality['tables_with_data'], quality['tables_total']) == (0, 0, 2)


def test_duplicate_values_in_a_key_like_column(client, database):
    quality = profile(client, database)
    assert found(quality, 'duplicate_key') == [('high', 'orders', 'order_id', 'duplicate_key')]
    assert message(quality, 'orders', 'order_id', 'duplicate_key') == (
        'order_id looks like the key, but 10 of its 50 values repeat an earlier one.')
    # a two-column key cannot be judged from one column's statistics: not flagged
    assert found(quality, 'duplicate_key', 'order_items') == []


def test_duplicate_keys_and_missing_keys_in_files(client, files):
    quality = profile(client, files)
    assert found(quality, 'duplicate_key') == [('high', 'customers', 'customer_id', 'duplicate_key')]
    assert '5 of its 50 values repeat' in message(quality, 'customers', 'customer_id', 'duplicate_key')
    # A file declares no key, so "no primary key is declared" is never said about one. notes.csv has
    # no column named like a key, but its "text" column holds a different value in every row.
    assert found(quality, 'no_primary_key') == [] and found(quality, 'no_key_column') == []


def test_mixed_types_in_a_file_column(client, files):
    quality = profile(client, files)
    assert found(quality, 'mixed_types') == [('medium', 'payments', 'amount', 'mixed_types'),
                                            ('medium', 'payments', 'paid_on', 'mixed_types')]
    assert message(quality, 'payments', 'amount', 'mixed_types') == (
        'Text column, but 90% of its values are numbers; 5 of 50 are not.')
    assert message(quality, 'payments', 'paid_on', 'mixed_types') == (
        'Text column, but 88% of its values are dates; 6 of 50 are not.')
    # ordinary text, and codes with leading zeros, are not "mostly numbers"
    assert found(quality, 'mixed_types', 'codes') == [] and found(quality, 'mixed_types', 'notes') == []
    stats = {c['name']: c['stats'] for c in
             client.get(f'/api/workspaces/{files}/tables/payments/profile?sample=false').json()['columns']}
    assert (stats['amount']['number_like'], stats['paid_on']['date_like'], stats['reference']['number_like']) == (45, 44, 0)


def test_mixed_types_is_only_about_files(client, database):
    con_quality = profile(client, database)
    assert found(con_quality, 'mixed_types') == []                   # a database column has one declared type


def test_findings_on_a_sample_say_so(client, database, monkeypatch):
    monkeypatch.setattr(config, 'SAMPLE_THRESHOLD_ROWS', 20)
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 20)
    quality = profile(client, database)
    sampled = {(f['table'], f['column'], f['rule']): f for f in quality['findings'] if f['sampled']}
    assert ('facts', 'never_filled', 'all_null') in sampled
    assert sampled[('facts', 'never_filled', 'all_null')]['message'] == 'No row has a value in the sample (20 rows checked).'
    assert all(f['table'] in ('facts', 'log_lines', 'orders', 'order_items') for f in sampled.values())


def test_quality_reflects_cleared_statistics(client, database):
    assert len(profile(client, database)['findings']) > 5
    client.delete(f'/api/workspaces/{database}/profile')
    quality = client.get(f'/api/workspaces/{database}/quality').json()
    assert found(quality) == [('medium', 'log_lines', None, 'no_primary_key')] and quality['tables_profiled'] == 0
    assert client.get('/api/workspaces/nope/quality').status_code == 404
