"""Zephyr Outdoor Supply: a fictional database with an answer key that was checked by hand.

tests/fixtures/zephyr/schema.sql and seed.sql build it (17 tables; the data is made up, so it is in
the repository). The author ran the app on it by hand on 13 Oct 2026 and wrote down what it must say; this
file holds that answer key, so that the app is checked against it at every test run:

  the structure      17 tables, 21 declared relationships
  the row counts     one per table
  the relationships  100 % coverage and no orphans everywhere, the empty keys, 1-1 or 1-N
  the quality tab    no findings
  personal columns   which ones are masked because of their name - now with employee.salary
  the SQL tab        five queries and their answers; and every column masked there exactly when the
                     Profile tab masks it, because a result column is judged by the table it comes from

The database is built from the two files in a temporary folder, once for this file, and read through
the app like any SQLite source: added, profiled, its relationships checked, queried in the SQL tab.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from app.main import create_app
from app.profiling.personal import CATEGORY_TEXT, personal_by_name
from conftest import local_client

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'zephyr'

ROW_COUNTS = {
    'order_item': 33, 'inventory': 29, 'product_supplier': 21, 'employee': 16, 'product': 16, 'payment': 15, 'sales_order': 15,
    'customer_phone': 11, 'customer': 10, 'category': 9, 'employee_dependent': 8, 'shipment': 7, 'department': 6,
    'supplier': 6, 'loyalty_card': 5, 'store': 4, 'warehouse': 3,
}
# child.column -> parent, as declared in schema.sql
RELATIONSHIPS = {
    'category.parent_category_id': 'category', 'customer_phone.customer_id': 'customer', 'department.manager_id': 'employee',
    'employee.department_id': 'department', 'employee.store_id': 'store', 'employee.supervisor_id': 'employee',
    'employee_dependent.employee_id': 'employee', 'inventory.warehouse_id': 'warehouse', 'inventory.product_id': 'product',
    'loyalty_card.customer_id': 'customer', 'order_item.order_id': 'sales_order', 'order_item.product_id': 'product',
    'payment.order_id': 'sales_order', 'product.category_id': 'category', 'product_supplier.product_id': 'product',
    'product_supplier.supplier_id': 'supplier', 'sales_order.customer_id': 'customer', 'sales_order.store_id': 'store',
    'sales_order.employee_id': 'employee', 'shipment.order_id': 'sales_order', 'shipment.warehouse_id': 'warehouse',
}
EMPTY_KEYS = {'employee.store_id': 8, 'sales_order.store_id': 7, 'sales_order.employee_id': 7,
              'category.parent_category_id': 4, 'employee.supervisor_id': 1, 'department.manager_id': 1}
ONE_TO_ONE = {'loyalty_card.customer_id', 'department.manager_id'}          # everything else is 1-N
MASKED_BY_NAME = {
    'customer.first_name': 'name', 'customer.last_name': 'name', 'employee.first_name': 'name', 'employee.last_name': 'name',
    'customer.email': 'e-mail', 'employee.email': 'e-mail', 'supplier.email': 'e-mail',
    'employee.birth_date': 'birth date', 'employee_dependent.birth_date': 'birth date',
    'customer.street': 'address', 'store.street': 'address',
    'customer_phone.phone_number': 'phone number', 'store.phone': 'phone number', 'supplier.phone': 'phone number',
    'loyalty_card.card_number': 'card number',
    'supplier.contact_name': 'name', 'employee_dependent.dependent_name': 'name',
    'employee.salary': 'salary / pay',
}


def wait(client, job):
    assert 'id' in job, job
    for _ in range(6000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    """One app for this file, on its own empty data folder."""
    with local_client(create_app(tmp_path_factory.mktemp('zephyr-data'))) as c:
        yield c


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    """The SQLite file built from schema.sql and seed.sql."""
    path = tmp_path_factory.mktemp('zephyr-db') / 'zephyr.db'
    con = sqlite3.connect(path)
    con.executescript((FIXTURE / 'schema.sql').read_text(encoding='utf-8'))
    con.executescript((FIXTURE / 'seed.sql').read_text(encoding='utf-8'))
    assert con.execute('PRAGMA foreign_key_check').fetchall() == []
    con.commit()
    con.close()
    return path


@pytest.fixture(scope='module')
def zephyr(client, database):
    """The database added to a workspace, profiled and checked. Returns (workspace id, source id)."""
    path = database
    ws = client.post('/api/workspaces', json={'name': 'Zephyr'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    assert source.status_code == 200, source.text
    assert source.json()['warnings'] == []
    done = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    assert (done['state'], done['tables_done'], done['errors']) == ('done', 17, [])
    done = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': 'all'}).json())
    assert (done['state'], done['errors']) == ('done', [])
    return ws, source.json()['id']


def run(client, zephyr, sql):
    ws, source = zephyr
    response = client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': sql})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_files_are_the_ones_that_were_tested_by_hand():
    schema, seed = (FIXTURE / 'schema.sql').read_text(encoding='utf-8'), (FIXTURE / 'seed.sql').read_text(encoding='utf-8')
    assert schema.startswith('-- Zephyr Outdoor Supply Co. (fictional)') and seed.startswith('-- Zephyr Outdoor Supply Co. (fictional)')
    assert schema.count('CREATE TABLE ') == 17 and schema.count('FOREIGN KEY ') == 21


def test_structure_17_tables_and_21_declared_relationships(client, zephyr):
    ws, _ = zephyr
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert model['stats'] == {'tables': 17, 'columns': 97, 'relationships': 21, 'declared': 21, 'joins': 0, 'values': 0, 'names': 0}
    assert {t['id'] for t in model['tables']} == set(ROW_COUNTS)
    declared = {f"{r['from']}.{r['from_columns'][0]}": r['to'] for r in model['relationships']}
    assert declared == RELATIONSHIPS
    assert all(r['source'] == 'declared' and len(r['from_columns']) == 1 for r in model['relationships'])


def test_row_counts(client, zephyr):
    ws, _ = zephyr
    tables = client.get(f'/api/workspaces/{ws}/profile').json()['tables']
    assert {t['id']: t['row_count'] for t in tables} == ROW_COUNTS
    assert all(t['row_count_kind'] == 'exact' and t['status'] == 'profiled' and not t['sampled'] for t in tables)
    assert sum(ROW_COUNTS.values()) == 214


def test_relationship_checks(client, zephyr):
    ws, _ = zephyr
    data = client.get(f'/api/workspaces/{ws}/relations/check').json()
    assert data['summary'] == {'relationships': 21, 'checked': 21, 'not_checked': 0, 'never': 0, 'with_orphans': 0, 'sampled': 0,
                               'not_exact': 0}
    assert data['suggestions'] == []
    seen = {}
    for r in data['relationships']:
        key, c = f"{r['from']}.{r['from_columns'][0]}", r['check']
        assert r['to'] == RELATIONSHIPS[key] and r['kind'] == 'declared', key
        # all 100 % coverage and 0 orphans, measured on every row
        assert (c['status'], c['coverage'], c['orphan_rows'], c['orphan_keys'], c['sampled']) == ('checked', 1.0, 0, 0, False), key
        assert c['rows'] == ROW_COUNTS[r['from']] and c['orphan_examples'] == [], key
        assert c['null_rows'] == EMPTY_KEYS.get(key, 0), key
        assert c['cardinality'] == ('1-1' if key in ONE_TO_ONE else '1-N') and c['cardinality_proven'] is True, key
        seen[key] = c
    assert set(seen) == set(RELATIONSHIPS)
    assert {key for key, c in seen.items() if c['null_rows']} == set(EMPTY_KEYS)


def test_no_quality_findings(client, zephyr):
    ws, _ = zephyr
    quality = client.get(f'/api/workspaces/{ws}/quality').json()
    assert quality['findings'] == [] and quality['summary'] == {'high': 0, 'medium': 0, 'low': 0}
    assert (quality['tables_total'], quality['tables_profiled'], quality['tables_with_data']) == (17, 17, 17)


def test_columns_masked_by_name(client, zephyr):
    ws, _ = zephyr
    masked = {}
    for table in ROW_COUNTS:
        profile = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': 'true'}).json()
        at = {name: i for i, name in enumerate(profile['sample']['columns'])}
        for c in profile['columns']:
            key = f'{table}.{c["name"]}'
            values = [row[at[c['name']]] for row in profile['sample']['rows'] if row[at[c['name']]] is not None]
            if not c['personal']:
                assert c['name'] not in profile['sample']['personal'], key
                assert not c['stats'].get('masked'), key
                continue
            assert c['personal_reason'].startswith('Looks like '), key
            masked[key] = c['personal_reason'][len('Looks like '):]
            assert CATEGORY_TEXT[personal_by_name(c['name'], table)] == masked[key], key       # by its name, whatever its values
            # masked wherever its values would show: minimum, maximum, top values, the sample rows
            assert c['stats']['masked'] is True and 'avg' not in c['stats'] and 'histogram' not in c['stats'], key
            assert '*' in str(c['stats']['min']) and '*' in str(c['stats']['max']), key
            assert all('*' in str(v) for v, _ in c['stats'].get('top') or []), key
            assert values and all('*' in str(v) for v in values), key
    assert masked == MASKED_BY_NAME
    # the things, not people: their names are shown
    names = client.get(f'/api/workspaces/{ws}/tables/store/profile').json()['columns'][1]
    assert (names['name'], names['personal'], names['stats']['min'], names['stats']['max']) == (
        'name', False, 'Zephyr Desert Outpost', 'Zephyr Mountain View')
    # employee.salary: hidden completely
    salary = next(c for c in client.get(f'/api/workspaces/{ws}/tables/employee/profile').json()['columns'] if c['name'] == 'salary')
    assert salary['stats']['min'] == salary['stats']['max'] == '*****' and salary['stats']['top_hidden'] is True
    assert (salary['stats']['rows'], salary['stats']['nulls'], salary['stats']['distinct']) == (16, 0, 16)


def test_sql_orders_by_status(client, zephyr):
    r = run(client, zephyr, 'SELECT status, COUNT(*) AS orders FROM sales_order GROUP BY status ORDER BY orders DESC, status')
    assert dict(map(tuple, r['rows'])) == {'Delivered': 10, 'Shipped': 2, 'Processing': 1, 'Pending': 1, 'Cancelled': 1}
    assert r['rows'][0] == ['Delivered', 10] and r['personal'] == [] and r['more'] is False


REVENUE = """
SELECT COALESCE(s.name, 'Online') AS store, COUNT(DISTINCT o.order_id) AS orders,
       ROUND(SUM(i.quantity * i.unit_price * (1 - i.discount_pct / 100.0)), 2) AS revenue
FROM sales_order o
JOIN order_item i ON i.order_id = o.order_id
LEFT JOIN store s ON s.store_id = o.store_id
WHERE o.status <> 'Cancelled'
GROUP BY COALESCE(s.name, 'Online')
ORDER BY revenue DESC"""
STOCK = """
SELECT w.name AS warehouse, p.name AS product, i.quantity_on_hand, i.reorder_level
FROM inventory i
JOIN warehouse w ON w.warehouse_id = i.warehouse_id
JOIN product p ON p.product_id = i.product_id
WHERE i.quantity_on_hand < i.reorder_level"""


def test_sql_revenue_per_store_without_cancelled_orders(client, zephyr):
    # With "Show personal values" off. The store's name is built from store.name - the name of a shop,
    # which the Profile tab does not mask - so it is shown here too.
    r = run(client, zephyr, REVENUE)
    assert r['rows'] == [['Online', 6, 2740.2], ['Zephyr Lakeside', 3, 1233.35], ['Zephyr Flagship', 3, 1059.0],
                         ['Zephyr Mountain View', 2, 610.0]]
    assert (r['personal'], r['masked'], r['show_personal']) == ([], False, False)
    assert sum(row[1] for row in r['rows']) == 14                               # 15 orders, one cancelled


def test_sql_stock_below_reorder_level(client, zephyr):
    r = run(client, zephyr, STOCK)                                              # warehouse.name and product.name: shown
    assert r['rows'] == [['Southwest Depot', 'Swiftfoot Trail Runners', 6, 10]]
    assert (r['row_count'], r['personal'], r['masked'], r['show_personal']) == (1, [], False, False)


def test_sql_customers_without_a_loyalty_card(client, zephyr):
    r = run(client, zephyr, 'SELECT COUNT(*) AS customers FROM customer c '
                            'WHERE NOT EXISTS (SELECT 1 FROM loyalty_card l WHERE l.customer_id = c.customer_id)')
    assert r['rows'] == [[5]] and r['personal'] == []
    r = run(client, zephyr, 'SELECT COUNT(*) FROM customer c LEFT JOIN loyalty_card l ON l.customer_id = c.customer_id '
                            'WHERE l.card_number IS NULL')
    assert r['rows'] == [[5]]


def test_sql_a_cross_join_stops_at_1000_rows_and_says_more(client, zephyr):
    r = run(client, zephyr, 'SELECT a.order_id, a.line_no, b.order_id AS other_order, b.line_no AS other_line '
                            'FROM order_item a CROSS JOIN order_item b')
    assert (r['row_count'], len(r['rows']), r['more'], r['max_rows']) == (1000, 1000, True, 1000)       # 33 x 33 = 1,089
    assert run(client, zephyr, 'SELECT COUNT(*) FROM order_item a CROSS JOIN order_item b')['rows'] == [[1089]]


def test_sql_pay_is_hidden_and_counts_are_not(client, zephyr):
    r = run(client, zephyr, 'SELECT AVG(salary) FROM employee')
    assert r['rows'] == [['*****']] and r['columns'][0] == {'name': 'AVG(salary)', 'personal': True,
                                                            'reason': 'Built from employee.salary (looks like salary / pay)'}
    r = run(client, zephyr, 'SELECT COUNT(salary), COUNT(*) FROM employee')
    assert r['rows'] == [[16, 16]] and r['personal'] == []
    r = run(client, zephyr, 'SELECT job_title, salary FROM employee ORDER BY employee_id')
    assert r['rows'][0] == ['Chief Executive Officer', '*****'] and {row[1] for row in r['rows']} == {'*****'}


# ------------------------------------------- the SQL tab judges a column by its table

def columns_of(client, ws, table):
    return client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': 'false'}).json()['columns']


def test_every_column_is_masked_in_the_sql_tab_exactly_when_the_profile_tab_masks_it(client, zephyr):
    """All 97 columns, three ways: by its name, under another name, and through SELECT *."""
    ws, _ = zephyr
    walked, masked = 0, set()
    for table in ROW_COUNTS:
        profile = columns_of(client, ws, table)
        star = run(client, zephyr, f'SELECT * FROM {table}')
        assert [c['name'] for c in star['columns']] == [c['name'] for c in profile]
        for i, c in enumerate(profile):
            key = f'{table}.{c["name"]}'
            plain = run(client, zephyr, f'SELECT {c["name"]} FROM {table}')
            alias = run(client, zephyr, f'SELECT t.{c["name"]} AS v FROM {table} t')
            shown = [(plain['columns'][0], [row[0] for row in plain['rows']]), (alias['columns'][0], [row[0] for row in alias['rows']]),
                     (star['columns'][i], [row[i] for row in star['rows']])]
            reason = f'{key}: looks like {MASKED_BY_NAME[key]}' if c['personal'] else None
            for column, values in shown:
                assert (column['personal'], column['reason']) == (c['personal'], reason), (key, column)
                # masked means masked: no value of it without a star
                assert all(('*' in str(v)) is c['personal'] for v in values if v is not None), key
            assert (plain['masked'], alias['masked']) == (c['personal'], c['personal']), key
            masked.update([key] if c['personal'] else [])
            walked += 1
    assert walked == 97 and masked == set(MASKED_BY_NAME) and len(masked) == 18


def test_the_names_of_things_are_shown_in_the_sql_tab(client, zephyr):
    ws, _ = zephyr
    things = [t for t in ROW_COUNTS if any(c['name'] == 'name' and not c['personal'] for c in columns_of(client, ws, t))]
    assert sorted(things) == ['category', 'department', 'product', 'store', 'warehouse']
    for table in things:
        r = run(client, zephyr, f'SELECT name FROM {table} ORDER BY name')
        assert (r['personal'], r['masked'], r['columns'][0]['reason']) == ([], False, None), table
        assert len(r['rows']) == ROW_COUNTS[table] and not any('*' in row[0] for row in r['rows']), table
    assert run(client, zephyr, 'SELECT name FROM store ORDER BY name')['rows'][0] == ['Zephyr Desert Outpost']
    assert run(client, zephyr, 'SELECT p.name, w.name FROM product p JOIN inventory i ON i.product_id = p.product_id '
                               'JOIN warehouse w ON w.warehouse_id = i.warehouse_id '
                               'WHERE i.quantity_on_hand < i.reorder_level')['rows'] == [['Swiftfoot Trail Runners', 'Southwest Depot']]
    # built from the name of a thing: shown as well
    r = run(client, zephyr, "SELECT COALESCE(s.name, 'Online') AS store, UPPER(d.name) AS dept FROM employee e "
                            'JOIN department d ON d.department_id = e.department_id LEFT JOIN store s ON s.store_id = e.store_id '
                            'ORDER BY e.employee_id')
    assert r['personal'] == [] and r['rows'][0] == ['Online', 'EXECUTIVE'] and r['rows'][5] == ['Zephyr Flagship', 'SALES']
    # supplier: the company's name is shown, the contact's is a person's
    r = run(client, zephyr, 'SELECT company_name, contact_name FROM supplier ORDER BY supplier_id')
    assert r['personal'] == ['contact_name'] and '*' not in r['rows'][0][0] and '*' in r['rows'][0][1]


def test_a_personal_column_is_masked_wherever_it_comes_through(client, zephyr):
    name = 'customer.first_name: looks like name'
    r = run(client, zephyr, 'SELECT name FROM (SELECT first_name AS name FROM customer)')          # through a subquery
    assert r['columns'] == [{'name': 'name', 'personal': True, 'reason': name}] and {'*' in row[0] for row in r['rows']} == {True}
    r = run(client, zephyr, 'SELECT name FROM store UNION SELECT first_name FROM customer')         # one branch of a UNION
    assert r['columns'] == [{'name': 'name', 'personal': True, 'reason': name}] and len(r['rows']) == 14
    assert {'*' in row[0] for row in r['rows']} == {True}                                           # the stores' names with it
    r = run(client, zephyr, 'SELECT s.name, e.first_name FROM store s JOIN employee e ON e.store_id = s.store_id '
                            'ORDER BY e.employee_id')                                               # a JOIN: only the person
    assert [(c['name'], c['personal'], c['reason']) for c in r['columns']] == [
        ('name', False, None), ('first_name', True, 'employee.first_name: looks like name')]
    assert r['rows'][0] == ['Zephyr Flagship', 'C***b'] and r['personal'] == ['first_name']
    # a WITH query called like a table of things, and a table of people under the name of one
    r = run(client, zephyr, 'WITH store AS (SELECT last_name AS name FROM employee) SELECT name FROM store')
    assert r['columns'][0]['reason'] == 'employee.last_name: looks like name' and r['masked'] is True
    r = run(client, zephyr, 'SELECT store.first_name AS name, store.salary AS quantity FROM employee store ORDER BY 1 DESC')
    assert [c['reason'] for c in r['columns']] == ['employee.first_name: looks like name', 'employee.salary: looks like salary / pay']
    assert {row[1] for row in r['rows']} == {'*****'} and all('*' in row[0] for row in r['rows'])
    # built from several columns: one personal one is enough, and the one that hides most decides how
    r = run(client, zephyr, "SELECT s.name || ': ' || e.first_name AS who, e.salary + LENGTH(e.last_name) AS n "
                            'FROM store s JOIN employee e ON e.store_id = s.store_id ORDER BY e.employee_id')
    assert [c['reason'] for c in r['columns']] == ['Built from employee.first_name (looks like name)',
                                                   'Built from employee.salary (looks like salary / pay)']
    assert r['rows'][0][1] == '*****' and 'Caleb' not in r['rows'][0][0]


def test_what_cannot_be_resolved_falls_back_to_the_name(client, zephyr):
    """Not resolved for sure: judged by the result column's name, as before. That masks too much, not too little."""
    # a WITH query that reads a later one (SQLite allows it): "a" gives first names, under a harmless name
    r = run(client, zephyr, 'WITH a AS (SELECT name FROM store), store AS (SELECT first_name AS name FROM customer) '
                            'SELECT name FROM a ORDER BY 1')
    assert r['columns'] == [{'name': 'name', 'personal': True, 'reason': 'Looks like name'}] and r['rows'][0] == ['A***n']
    # a join that merges columns: the * is not expanded, every column is judged by its name
    r = run(client, zephyr, 'SELECT * FROM customer NATURAL JOIN loyalty_card')
    assert r['personal'] == ['first_name', 'last_name', 'email', 'street', 'card_number']
    assert {c['reason'] for c in r['columns'] if c['personal']} == {'Looks like name', 'Looks like e-mail', 'Looks like address',
                                                                    'Looks like card number'}
    # the name of a thing that two tables of a merging join both have: masked, as before this round
    r = run(client, zephyr, 'SELECT name FROM store NATURAL JOIN category')
    assert r['columns'] == [{'name': 'name', 'personal': True, 'reason': 'Looks like name'}]
    r = run(client, zephyr, 'SELECT * FROM store NATURAL JOIN (SELECT 1 AS one) x')
    assert r['personal'] == ['name', 'street', 'phone'] and r['rows'][0][1] == 'Ze***********ip'
    # a name that two tables of the FROM clause have is an error on the server; with its table it is resolved
    ws, source = zephyr
    refused = client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': 'SELECT name FROM store, warehouse'})
    assert refused.status_code == 400 and 'ambiguous' in refused.json()['detail']
    assert run(client, zephyr, 'SELECT s.name, w.name FROM store s, warehouse w')['personal'] == []


def test_a_table_under_a_name_that_only_oracle_reserves_is_followed(client, zephyr):
    """session, comment, level, resource, size and public are reserved words of Oracle and plain names
    on SQLite. Until 13 Oct 2026 the reader of a FROM clause went by Oracle's list on every engine: such
    a table or alias was "not followed", its columns were judged by their names alone, and the name of a
    shop was masked."""
    for word in ('session', 'comment', 'level', 'resource', 'size', 'public'):
        r = run(client, zephyr, f'SELECT {word}.name, {word}.city FROM store {word} ORDER BY 1')
        assert (r['personal'], r['masked'], r['rows'][0][0]) == ([], False, 'Zephyr Desert Outpost'), word
        r = run(client, zephyr, f'SELECT {word}.first_name AS shop, s.name FROM employee AS {word} JOIN store s '
                                f'ON s.store_id = {word}.store_id ORDER BY {word}.employee_id')
        assert r['columns'] == [{'name': 'shop', 'personal': True, 'reason': 'employee.first_name: looks like name'},
                                {'name': 'name', 'personal': False, 'reason': None}], word
        assert r['rows'][0] == ['C***b', 'Zephyr Flagship'], word
        r = run(client, zephyr, f'SELECT v.{word} FROM (SELECT salary AS {word} FROM employee) v')
        assert r['columns'][0]['reason'] == 'employee.salary: looks like salary / pay' and {row[0] for row in r['rows']} == {'*****'}, word


def raw_values(database):
    """The values of the 18 masked columns that no other column holds, as text."""
    con = sqlite3.connect(database)
    secret, public = set(), set()
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for column in [row[1] for row in con.execute(f'PRAGMA table_info({table})')]:
            values = {str(int(v) if isinstance(v, float) and v.is_integer() else v)
                      for (v,) in con.execute(f'SELECT {column} FROM {table}') if v is not None}
            (secret if f'{table}.{column}' in MASKED_BY_NAME else public).update(values)
    con.close()
    return secret - public


def leaks(result, secret):
    """The cells that are not masked and show a personal value: whole, or inside a longer text."""
    found = []
    for row in result['rows']:
        for column, value in zip(result['columns'], row):
            cell = str(int(value) if isinstance(value, float) and value.is_integer() else value)
            if value is not None and not (column['personal'] and result['masked']) and (
                    cell in secret or any(len(s) >= 6 and s in cell for s in secret)):
                found.append((column['name'], cell))
    return found


EVERY_WHICH_WAY = [
    'SELECT * FROM employee',
    'SELECT e.*, s.* FROM employee e LEFT JOIN store s ON s.store_id = e.store_id',
    'SELECT * FROM customer c JOIN customer_phone p USING (customer_id)',
    'SELECT * FROM customer NATURAL JOIN loyalty_card',
    'SELECT * FROM supplier',
    'SELECT first_name AS name FROM customer',
    'SELECT c.first_name name, c.last_name AS city FROM customer c',
    'SELECT name FROM (SELECT first_name AS name FROM customer)',
    'SELECT store.name FROM (SELECT last_name AS name FROM employee) store',
    'SELECT * FROM (SELECT d.name, e.first_name AS name FROM department d JOIN employee e ON e.employee_id = d.manager_id)',
    'WITH store AS (SELECT first_name AS name FROM customer) SELECT name FROM store',
    'WITH a AS (SELECT name FROM store), store AS (SELECT first_name AS name FROM customer) SELECT name FROM a',
    'WITH store (name) AS (SELECT email FROM supplier) SELECT s.name FROM store s',
    'WITH x AS (SELECT first_name AS name FROM employee), y AS (SELECT name FROM x) SELECT y.name AS city FROM y',
    'SELECT name FROM store UNION SELECT first_name FROM customer',
    'SELECT first_name FROM customer UNION ALL SELECT name FROM store',
    'SELECT name FROM product UNION ALL SELECT name FROM (SELECT contact_name AS name FROM supplier)',
    "SELECT 'x' AS name WHERE 1 = 0 UNION ALL SELECT last_name FROM employee",
    'SELECT name FROM store EXCEPT SELECT first_name FROM customer UNION SELECT street FROM customer',
    "SELECT name FROM store UNION ALL SELECT 'A' UNION ALL SELECT first_name FROM employee",
    'SELECT name FROM category UNION ALL SELECT dependent_name FROM employee_dependent ORDER BY 1',
    'SELECT s.name, e.first_name FROM store s JOIN employee e ON e.store_id = s.store_id',
    'SELECT d.name, e.first_name AS name FROM department d JOIN employee e ON e.employee_id = d.manager_id',
    'SELECT e.first_name FROM employee e JOIN employee m ON m.employee_id = e.supervisor_id',
    'SELECT first_name FROM employee e JOIN department d ON d.manager_id = e.employee_id',
    'SELECT s.name, (SELECT MAX(e.last_name) FROM employee e WHERE e.store_id = s.store_id) AS name2 FROM store s',
    'SELECT (SELECT c.first_name FROM customer c WHERE c.customer_id = o.customer_id) AS city FROM sales_order o',
    "SELECT COALESCE(NULL, first_name) AS store, first_name || ' ' || last_name AS product FROM employee",
    "SELECT CASE WHEN 1 = 1 THEN email ELSE 'x' END AS status, SUBSTR(last_name, 1, 99) AS part FROM customer",
    'SELECT MAX(first_name) AS n, MIN(street) AS m, GROUP_CONCAT(last_name) AS g FROM customer',
    'SELECT salary, salary + 0 AS quantity, MAX(salary) OVER () AS top FROM employee',
    'SELECT CAST(first_name AS TEXT) AS name, first_name COLLATE NOCASE AS name2 FROM customer',
    'SELECT DISTINCT first_name FROM customer ORDER BY 1',
    'SELECT ALL last_name FROM customer',
    'SELECT "first_name", [last_name], `email` FROM "customer"',
    'SELECT main.customer.first_name FROM main.customer',
    'SELECT store.first_name FROM customer AS store',
    'SELECT store.* FROM employee store',
    'SELECT card_number AS sku, phone_number AS tier FROM loyalty_card l JOIN customer_phone p ON p.customer_id = l.customer_id',
    'SELECT birth_date AS hire_date, street AS city FROM employee e JOIN store s ON s.store_id = e.store_id',
    'SELECT company_name, contact_name, phone AS postal_code FROM supplier',
    'SELECT name FROM store WHERE name IN (SELECT first_name FROM customer) UNION SELECT dependent_name FROM employee_dependent',
    # deeper: subqueries in subqueries, WITH inside an expression, three branches, names in other spellings
    'SELECT c.name FROM (SELECT first_name AS name FROM customer) AS c JOIN store s ON 1 = 1',
    'SELECT (SELECT first_name FROM customer LIMIT 1) AS name FROM store',
    'SELECT name FROM (SELECT * FROM (SELECT first_name AS name FROM customer))',
    'SELECT first_name AS city, city AS first_name FROM customer',
    'SELECT (WITH store AS (SELECT first_name AS name FROM customer) SELECT name FROM store LIMIT 1) AS x FROM department',
    'SELECT name FROM store UNION ALL SELECT x FROM (SELECT first_name x FROM customer)',
    'SELECT t.name FROM store, (SELECT last_name AS name FROM employee) t',
    'SELECT "name" FROM (SELECT first_name AS "name" FROM customer)',
    'SELECT NAME FROM (SELECT FIRST_NAME AS Name FROM CUSTOMER)',
    'SELECT x.* FROM (SELECT first_name FROM customer) x, store',
    'SELECT name, LAG(first_name) OVER (ORDER BY customer_id) AS prev FROM store, customer',
    'SELECT IFNULL(NULL, first_name) name FROM customer',
    "SELECT first_name || '' name FROM customer",
    'SELECT name FROM store UNION SELECT first_name FROM customer ORDER BY 1 LIMIT 3',
    "SELECT customer.first_name AS 'name' FROM customer",
    'SELECT first_name "name" FROM customer',
    'SELECT name FROM store WHERE 1 UNION SELECT first_name FROM customer WHERE 1',
    'SELECT s.name, x.name FROM store s JOIN (SELECT store_id, first_name AS name FROM employee) x ON x.store_id = s.store_id',
    'SELECT name FROM (SELECT name FROM store UNION ALL SELECT last_name FROM customer)',
    'SELECT p.name FROM product p UNION ALL SELECT d.dependent_name FROM employee_dependent d UNION ALL SELECT w.name FROM warehouse w',
    'SELECT MIN(name) FROM (SELECT first_name AS name FROM employee)',
    'SELECT name FROM store WHERE store_id < 0 UNION ALL SELECT * FROM (SELECT email FROM supplier)',
    'WITH RECURSIVE r (id, name) AS (SELECT employee_id, first_name FROM employee WHERE supervisor_id IS NULL UNION ALL '
    'SELECT e.employee_id, e.first_name FROM employee e JOIN r ON e.supervisor_id = r.id) SELECT name FROM r',
    'SELECT s.name FROM store s UNION ALL SELECT c.first_name FROM customer c UNION ALL SELECT p.name FROM product p',
    'SELECT * FROM (SELECT s.name, e.salary AS unit_price FROM store s JOIN employee e ON e.store_id = s.store_id)',
    'SELECT name FROM category WHERE 0 UNION ALL SELECT birth_date FROM employee',
    'SELECT COUNT(*) AS n, MAX(first_name) AS name FROM customer',
    'SELECT name FROM (SELECT phone AS name FROM supplier) UNION ALL SELECT name FROM warehouse',
    # tables, subqueries and columns under names that only Oracle reserves: followed on SQLite since 13 Oct 2026
    'SELECT size.first_name AS name FROM customer size',
    'SELECT session.name, level.name FROM store level, (SELECT first_name AS name FROM customer) session',
    'SELECT comment.name FROM (SELECT email AS name FROM supplier) comment UNION ALL SELECT resource.name FROM warehouse resource',
    'WITH public AS (SELECT last_name AS name FROM employee) SELECT public.name, s.name FROM public, store s',
    'SELECT v.size, v.level FROM (SELECT salary AS size, name AS level FROM employee, store) v',
]


def test_no_personal_value_is_shown_whichever_way_it_is_asked_for(client, zephyr, database):
    """Statements that put personal columns under other names, behind tables of things, through
    subqueries, WITH queries, UNIONs and joins. Whatever of them is resolved and whatever falls
    back to the name: no value of a masked column is in a cell that is not masked."""
    secret = raw_values(database)
    assert len(secret) > 120 and {'Marisol', 'elena.marsh@mail.example', '185000', '555-1001', 'ZL-000101'} <= secret
    assert len(EVERY_WHICH_WAY) == len(set(EVERY_WHICH_WAY)) == 75
    for sql in EVERY_WHICH_WAY:
        assert leaks(run(client, zephyr, sql), secret) == [], sql
    # the same statements with "show personal values" on: each of them does show such values, so each one counted
    client.put('/api/session', json={'show_personal': True})
    try:
        for sql in EVERY_WHICH_WAY:
            assert leaks(run(client, zephyr, sql), secret), sql
    finally:
        client.put('/api/session', json={'show_personal': False})


# ------------------------------------------------------- your own mark, per table

@pytest.fixture
def marked(client, database):
    """A second workspace on the same database (not profiled), for marks: they stay out of the answer key's."""
    ws = client.post('/api/workspaces', json={'name': 'Zephyr marks'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(database)}).json()['id']
    yield ws, source
    client.delete(f'/api/workspaces/{ws}')


def test_a_mark_on_a_table_column_is_obeyed_in_the_sql_tab_and_touches_no_other_table(client, marked):
    ws, _ = marked

    def mark(table, column, personal):
        answer = client.post(f'/api/workspaces/{ws}/personal', json={'table': table, 'column': column, 'personal': personal})
        assert answer.status_code == 200, answer.text

    def shown(sql):
        r = run(client, marked, sql)
        return [(c['name'], c['personal'], c['reason']) for c in r['columns']], r['rows'][0]

    # before: as the names say
    assert shown('SELECT first_name FROM employee ORDER BY employee_id') == (
        [('first_name', True, 'employee.first_name: looks like name')], ['Ma***ol'])
    assert shown('SELECT name FROM store ORDER BY store_id') == ([('name', False, None)], ['Zephyr Flagship'])

    mark('employee', 'first_name', False)                   # "not personal": shown
    mark('store', 'name', True)                             # "personal": masked
    mark('customer', 'email', False)                        # its values look like e-mail addresses all the same
    assert shown('SELECT first_name FROM employee ORDER BY employee_id') == ([('first_name', False, None)], ['Marisol'])
    assert shown('SELECT e.first_name AS who FROM employee e ORDER BY e.employee_id') == ([('who', False, None)], ['Marisol'])
    assert shown('SELECT * FROM employee ORDER BY employee_id')[1][:3] == [1, 'Marisol', 'O****r']       # last_name stays masked
    assert shown('SELECT name FROM store ORDER BY store_id') == (
        [('name', True, 'store.name: marked personal by you')], ['Ze***********ip'])
    assert shown('SELECT email FROM customer ORDER BY customer_id') == ([('email', False, None)], ['elena.marsh@mail.example'])
    # the same-named column of another table is not touched by either mark
    assert shown('SELECT first_name FROM customer ORDER BY customer_id') == (
        [('first_name', True, 'customer.first_name: looks like name')], ['E***a'])
    assert shown('SELECT email FROM employee ORDER BY employee_id')[0] == [('email', True, 'employee.email: looks like e-mail')]
    for table in ('product', 'department', 'category', 'warehouse'):
        assert shown(f'SELECT name FROM {table}')[0] == [('name', False, None)], table
    # in one statement: each mark decides for its own table's column
    assert shown('SELECT s.name, e.first_name, p.name AS product FROM store s JOIN employee e ON e.store_id = s.store_id '
                 'CROSS JOIN product p ORDER BY e.employee_id, p.product_id') == (
        [('name', True, 'store.name: marked personal by you'), ('first_name', False, None), ('product', False, None)],
        ['Ze***********ip', 'Caleb', 'Ridgeline 2-Person Tent'])
    # built from a marked column: masked; a UNION with it: masked
    assert shown("SELECT COALESCE(s.name, 'Online') AS store FROM store s ORDER BY s.store_id")[0] == [
        ('store', True, 'Built from store.name (marked personal by you)')]
    assert shown('SELECT name FROM product UNION ALL SELECT name FROM store')[0] == [('name', True, 'store.name: marked personal by you')]
    # built from the column marked "not personal": no longer personal by its column; its values are looked at like any others'
    assert shown('SELECT UPPER(first_name) AS n FROM employee ORDER BY employee_id') == ([('n', False, None)], ['MARISOL'])
    assert shown('SELECT LOWER(email) AS e FROM customer ORDER BY customer_id')[0] == [('e', True, 'Its values look like e-mail')]
    # where the table is not known for sure, a "personal" mark counts for every column of that name - the safe side
    r = run(client, marked, 'SELECT * FROM product NATURAL JOIN (SELECT 1 AS one) x')
    assert {c['name']: c['reason'] for c in r['columns'] if c['personal']} == {'name': 'Marked personal by you'}

    mark('employee', 'first_name', None)                    # back to the rules
    mark('store', 'name', None)
    mark('customer', 'email', None)
    assert shown('SELECT first_name FROM employee ORDER BY employee_id')[0] == [('first_name', True, 'employee.first_name: looks like name')]
    assert shown('SELECT name FROM store ORDER BY store_id') == ([('name', False, None)], ['Zephyr Flagship'])
    assert shown('SELECT email FROM customer')[0] == [('email', True, 'customer.email: looks like e-mail')]
