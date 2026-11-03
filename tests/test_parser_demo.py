"""The parser (app/er) on the demo .sql: every table and every relationship is found."""

import pytest

from app import er
from expected import COLUMN_COUNTS, DECLARED_KEYS, JOIN_ONLY, PRIMARY_KEYS, TABLES, VALUE_MATCH


@pytest.fixture(scope='module')
def model(demo_sql):
    return er.parse_sql(demo_sql.read_text(encoding='utf-8'))


def by_name(model):
    return {t.name: t for t in model.tables}


def test_finds_all_tables(model):
    assert {t.name for t in model.tables} == TABLES
    assert all(t.from_ddl for t in model.tables)


def test_finds_all_columns_in_ddl_order(model):
    tables = by_name(model)
    assert {name: len(t.columns) for name, t in tables.items()} == COLUMN_COUNTS
    assert [c.name for c in tables['branches'].columns.values()] == [
        'branch_id', 'branch_name', 'city', 'region', 'opened_date']
    assert [c.ddl_order for c in tables['branches'].columns.values()] == [0, 1, 2, 3, 4]


def test_reads_types_keys_and_not_null(model):
    accounts = by_name(model)['accounts'].columns
    assert accounts['ACCOUNT_ID'].type == 'INTEGER' and accounts['ACCOUNT_ID'].pk
    assert accounts['CREDIT_LIMIT'].type == 'DECIMAL(10, 2)'
    assert accounts['ACCOUNT_NUMBER'].type == 'VARCHAR(12)'
    assert accounts['ACCOUNT_ID'].nullable is False           # primary key
    assert accounts['CUSTOMER_ID'].nullable is False          # NOT NULL
    assert accounts['PARENT_ACCOUNT_ID'].nullable is True
    assert accounts['CUSTOMER_ID'].fk and accounts['PARENT_ACCOUNT_ID'].fk
    assert not accounts['STATUS'].fk
    for name, key in PRIMARY_KEYS.items():
        assert [c.name for c in by_name(model)[name].columns.values() if c.pk] == [key]


def test_finds_all_declared_foreign_keys(model):
    found = {(t.name, cols[0], rt.name, refcols[0]) for t, cols, rt, refcols in model.declared_fks}
    assert found == DECLARED_KEYS
    declared = [r for r in model.rels.values() if r.declared]
    assert len(declared) == 13
    assert all(r.one is not None for r in declared)


def test_finds_relationships_implied_by_joins(model):
    inferred = [r for r in model.rels.values() if not r.declared]
    assert len(model.rels) == 15 and len(inferred) == 2
    ends = lambda r: frozenset({(r.a[0].name, r.a[0].columns[r.a[1]].name), (r.b[0].name, r.b[0].columns[r.b[1]].name)})
    by_ends = {ends(r): r for r in inferred}

    payments = by_ends[frozenset({(JOIN_ONLY[0], JOIN_ONLY[1]), (JOIN_ONLY[2], JOIN_ONLY[3])})]
    assert payments.kinds == {'join'}
    assert payments.one[0].name == 'accounts'                 # accounts.account_id is the key: 1 - N

    match = by_ends[VALUE_MATCH]
    assert match.one is None                                  # two foreign keys: no clear direction


def test_joins_on_declared_keys_are_not_duplicated(model):
    devices = next(r for r in model.rels.values() if {r.a[0].name, r.b[0].name} == {'devices', 'subscriptions'})
    assert devices.declared and devices.kinds == {'fk', 'in'}  # declared, and used in an IN (sub-query)


def test_records_aliases(model):
    tables = by_name(model)
    assert tables['invoice_lines'].aliases == ['il']
    assert tables['customers'].aliases == ['c']


def test_multi_column_foreign_keys_stay_together():
    model = er.parse_sql("""
        CREATE TABLE orders (order_id INT, line_no INT, PRIMARY KEY (order_id, line_no));
        CREATE TABLE shipments (shipment_id INT PRIMARY KEY, order_id INT NOT NULL, line_no INT NOT NULL,
            CONSTRAINT fk_ship FOREIGN KEY (order_id, line_no) REFERENCES orders (order_id, line_no));
    """)
    (table, cols, ref, refcols), = model.declared_fks
    assert (table.name, cols, ref.name, refcols) == ('shipments', ['order_id', 'line_no'], 'orders', ['order_id', 'line_no'])
    assert len(model.rels) == 2                               # the scripts draw one line per column pair


def test_references_without_columns_use_the_primary_key():
    model = er.parse_sql('CREATE TABLE a (a_id INT PRIMARY KEY); CREATE TABLE b (id INT, a_ref INT REFERENCES a);')
    (_, cols, ref, refcols), = model.declared_fks
    assert (cols, ref.name, refcols) == (['a_ref'], 'a', ['a_id'])
