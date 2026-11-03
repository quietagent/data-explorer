"""Each adapter on the demo data: SQL files, the live SQLite database, the CSV exports."""

import csv

import pytest
from openpyxl import Workbook

from app.model import DECLARED, JOINS, NAMES
from app.sources import SourceError
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from app.sources.files import collect_data_files, open_duckdb, read_files
from app.sources.sqlfiles import collect_sql_files, read_sql_files
from expected import (COLUMN_COUNTS, DECLARED_KEYS, JOIN_ONLY, NAME_ONLY, PRIMARY_KEYS, VALUE_MATCH, declared,
                      links, value_matches)


def counts(model):
    return {t.name: len(t.columns) for t in model.tables}


# ------------------------------------------------------------------ SQL files

@pytest.fixture(scope='module')
def sql_model(demo_sql):
    model, warnings = read_sql_files(collect_sql_files(demo_sql), 'telecom.sql', 'src1')
    assert warnings == []
    return model


def test_sql_adapter_tables_and_columns(sql_model):
    assert counts(sql_model) == COLUMN_COUNTS
    for t in sql_model.tables:
        assert [o.kind for o in t.origins] == ['ddl'] and t.origins[0].detail == 'telecom.sql'
        assert t.complete and t.schema is None
        assert [c.name for c in t.columns if c.pk] == [PRIMARY_KEYS[t.name]]
    customers = sql_model.find('customers')
    assert [c.name for c in customers.columns][:3] == ['customer_id', 'full_name', 'national_id']
    email = customers.column('EMAIL')
    assert (email.type, email.nullable, email.origins) == ('VARCHAR(120)', True, ['ddl'])
    assert customers.column('full_name').nullable is False
    assert customers.aliases == ['c']


def test_sql_adapter_relationships(sql_model):
    assert declared(sql_model) == DECLARED_KEYS
    assert links(sql_model, JOINS) == {JOIN_ONLY}
    assert value_matches(sql_model) == {VALUE_MATCH}
    assert links(sql_model, NAMES) == set()
    assert len(sql_model.relationships) == 15
    for r in sql_model.relationships:
        if r.source == DECLARED:
            assert r.confidence is None and r.cardinality == '1-N'
        else:
            assert r.source == JOINS and r.evidence
            assert r.confidence == (0.85 if r.cardinality == '1-N' else 0.5)
    seen_in_joins = {r.from_table.name for r in sql_model.relationships if r.source == DECLARED and JOINS in r.seen}
    assert {'devices', 'invoices', 'customers'} <= seen_in_joins


def test_sql_adapter_reads_a_folder_and_other_encodings(tmp_path):
    (tmp_path / 'ddl').mkdir()
    (tmp_path / 'ddl' / 'tables.sql').write_text(
        'CREATE TABLE dbo.orders (order_id INT PRIMARY KEY, customer_id INT NOT NULL);', encoding='utf-16')
    (tmp_path / 'report.sql').write_text(
        'SELECT o.order_id, c.name FROM dbo.orders o JOIN crm.customers c ON c.customer_id = o.customer_id;',
        encoding='utf-8-sig')
    (tmp_path / 'notes.txt').write_text('not sql')
    files = collect_sql_files(tmp_path)
    assert [f.name for f in files] == ['tables.sql', 'report.sql']
    model, _ = read_sql_files(files, 'folder', 'src')
    orders, customers = model.find('dbo.orders'), model.find('crm.customers')
    assert orders.complete and orders.origins[0].kind == 'ddl' and orders.origins[0].detail == 'tables.sql'
    assert not customers.complete and customers.origins[0].kind == 'query'      # only seen in a query
    assert [c.name for c in customers.columns] == ['name', 'customer_id']
    (rel,) = model.relationships
    assert (rel.source, rel.from_table, rel.to_table, rel.cardinality) == (JOINS, orders, customers, '1-N')


def test_sql_adapter_reports_what_it_cannot_use(tmp_path):
    with pytest.raises(SourceError, match='Not found'):
        collect_sql_files(tmp_path / 'missing')
    with pytest.raises(SourceError, match='No .sql files'):
        collect_sql_files(tmp_path)
    empty = tmp_path / 'empty.sql'
    empty.write_text('-- nothing here\n')
    with pytest.raises(SourceError, match='No tables'):
        read_sql_files([empty])


# ------------------------------------------------------------- live database

@pytest.fixture(scope='module')
def db_model(demo_db):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    model, warnings = db.read_schema('telecom.db', 'src2')
    db.dispose()
    assert warnings == []
    return model


def test_db_adapter_tables_and_columns(db_model):
    assert counts(db_model) == COLUMN_COUNTS
    for t in db_model.tables:
        assert [o.kind for o in t.origins] == ['db'] and t.kind == 'table' and t.schema is None
        assert [c.name for c in t.columns if c.pk] == [PRIMARY_KEYS[t.name]]
    accounts = db_model.find('accounts')
    assert [c.name for c in accounts.columns] == ['account_id', 'customer_id', 'parent_account_id', 'account_number',
                                                  'account_type', 'status', 'opened_date', 'credit_limit']
    by = {c.name: c for c in accounts.columns}
    assert by['account_id'].type == 'INTEGER' and by['account_id'].nullable is False
    assert by['credit_limit'].type == 'DECIMAL(10, 2)' and by['credit_limit'].nullable is True
    assert by['opened_date'].type == 'DATE' and by['opened_date'].nullable is False
    assert by['customer_id'].fk and by['parent_account_id'].fk and not by['status'].fk


def test_db_adapter_relationships(db_model):
    assert declared(db_model) == DECLARED_KEYS
    assert len(db_model.relationships) == 13
    assert all(r.source == DECLARED and r.cardinality == '1-N' and r.confidence is None
               for r in db_model.relationships)
    payments = db_model.find('payments')
    assert not payments.column('account_id').fk               # no key declared for it in the database


def test_db_adapter_connection_test(demo_db):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    info = db.test()
    assert info['ok'] and info['tables'] == 10 and info['engine'] == 'sqlite'
    assert 'file opened read-only' in info['protections'] and 'read-only session' in info['protections']


def test_db_adapter_views_and_one_to_one(tmp_path):
    import sqlite3
    path = tmp_path / 'small.db'
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE person (person_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE passport (person_id INTEGER PRIMARY KEY REFERENCES person (person_id), number TEXT);
        CREATE VIEW v_people AS SELECT name FROM person;
    """)
    con.close()
    without, _ = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(path))).read_schema()
    assert {t.name for t in without.tables} == {'person', 'passport'}
    (rel,) = without.relationships
    assert rel.cardinality == '1-1'                           # the foreign key is the whole primary key
    with_views, _ = ReadOnlyDatabase(
        ConnectionProfile(engine='sqlite', database=str(path), include_views=True)).read_schema()
    assert with_views.find('v_people').kind == 'view'


# -------------------------------------------------------------- CSV / Excel

@pytest.fixture(scope='module')
def csv_model(demo_csv):
    model, warnings = read_files(collect_data_files(demo_csv), 'csv exports', 'src3')
    assert warnings == []
    return model


def test_files_adapter_tables_and_types(csv_model):
    assert counts(csv_model) == COLUMN_COUNTS
    for t in csv_model.tables:
        assert [o.kind for o in t.origins] == ['file'] and t.origins[0].detail == f'{t.name}.csv'
        assert all(c.nullable is None and not c.pk and not c.fk for c in t.columns)
    types = {c.name: c.type for c in csv_model.find('tickets').columns}
    assert types == {'ticket_id': 'BIGINT', 'customer_id': 'BIGINT', 'subscription_id': 'BIGINT',
                     'branch_id': 'BIGINT', 'opened_at': 'TIMESTAMP', 'closed_at': 'TIMESTAMP',
                     'category': 'VARCHAR', 'priority': 'VARCHAR', 'status': 'VARCHAR'}
    accounts = {c.name: c.type for c in csv_model.find('accounts').columns}
    assert accounts['opened_date'] == 'DATE' and accounts['credit_limit'] == 'DOUBLE'


def test_files_adapter_infers_relationships_from_names(csv_model):
    # Every real link of the demo is found from the column names alone.
    assert links(csv_model, NAMES) == DECLARED_KEYS | {JOIN_ONLY, NAME_ONLY}
    assert len(csv_model.relationships) == 15
    assert all(r.source == NAMES and r.cardinality == '1-N' for r in csv_model.relationships)
    confidence = {(r.from_table.name, r.from_columns[0]): r.confidence for r in csv_model.relationships}
    assert confidence[('tickets', 'branch_id')] == 0.85               # exact name, same type
    assert confidence[('accounts', 'parent_account_id')] == 0.65      # prefixed name: less sure
    tickets = {c.name: (c.pk_inferred, c.fk_inferred) for c in csv_model.find('tickets').columns}
    assert tickets['ticket_id'] == (True, False) and tickets['branch_id'] == (False, True)
    assert tickets['status'] == (False, False)


def test_files_adapter_orders_customer_id_points_at_customers_id(tmp_path):
    (tmp_path / 'customers.csv').write_text('id,name\n1,Ann\n2,Bob\n')
    (tmp_path / 'orders.csv').write_text('id,customer_id,total\n10,1,5.5\n11,2,7\n')
    model, _ = read_files(collect_data_files(tmp_path))
    assert links(model, NAMES) == {('orders', 'customer_id', 'customers', 'id')}
    assert model.find('orders').column('total').type == 'DOUBLE'


def test_files_adapter_reads_excel_sheets(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = 'customers'
    ws.append(['customer_id', 'name', 'joined', 'score'])
    ws.append([1, 'Ann', __import__('datetime').datetime(2024, 1, 5), 4.5])
    ws.append([2, 'Bob', __import__('datetime').datetime(2024, 2, 9), 3.0])
    orders = wb.create_sheet('orders')
    orders.append([None, None, None])                             # blank row above the header
    orders.append(['order_id', 'customer_id', 'paid'])
    orders.append([10, 1, True])
    wb.create_sheet('empty')
    wb.save(tmp_path / 'export.xlsx')

    single = Workbook()
    single.active.append(['plan_id', 'fee'])
    single.active.append([1, 35])
    single.save(tmp_path / 'plans.xlsx')

    model, warnings = read_files(collect_data_files(tmp_path))
    # several sheets -> "<file>.<sheet>"; one sheet -> the file's name
    assert {t.id for t in model.tables} == {'export.customers', 'export.orders', 'plans'}
    assert {t.name for t in model.tables} == {'customers', 'orders', 'plans'}
    assert warnings == []
    customers = {c.name: c.type for c in model.find('customers').columns}
    assert customers == {'customer_id': 'BIGINT', 'name': 'VARCHAR', 'joined': 'DATE', 'score': 'DOUBLE'}
    assert {c.name: c.type for c in model.find('orders').columns} == {
        'order_id': 'BIGINT', 'customer_id': 'BIGINT', 'paid': 'BOOLEAN'}
    assert model.find('orders').origins[0].detail == 'export.xlsx · sheet orders'
    assert links(model, NAMES) == {('orders', 'customer_id', 'customers', 'customer_id')}


def test_files_adapter_handles_other_encodings_and_bad_files(tmp_path):
    with open(tmp_path / 'arabic.csv', 'w', encoding='cp1256', newline='') as f:
        csv.writer(f).writerows([['id', 'city'], [1, 'الرياض'], [2, 'جدة']])
    (tmp_path / 'tabs.tsv').write_text('a\tb\n1\tx\n', encoding='utf-16')
    (tmp_path / 'broken.xlsx').write_bytes(b'this is not a workbook')
    model, warnings = read_files(collect_data_files(tmp_path))
    assert {c.name: c.type for c in model.find('arabic').columns} == {'id': 'BIGINT', 'city': 'VARCHAR'}
    assert [c.name for c in model.find('tabs').columns] == ['a', 'b']
    assert any('arabic.csv: not UTF-8, read as cp1256' in w for w in warnings)
    assert any(w.startswith('broken.xlsx: could not be read') for w in warnings)
    with pytest.raises(SourceError):
        collect_data_files(tmp_path / 'missing')


def test_duckdb_cannot_download_extensions():
    con = open_duckdb()
    settings = dict(con.execute("SELECT name, value FROM duckdb_settings() WHERE name IN "
                                "('autoinstall_known_extensions', 'autoload_known_extensions')").fetchall())
    con.close()
    assert settings == {'autoinstall_known_extensions': 'false', 'autoload_known_extensions': 'false'}
