"""The common model: merging sources into one, inference, and filtered views."""

import pytest

from app.model import (DECLARED, JOINS, NAMES, Origin, SchemaModel, finalize_model, infer_by_names, to_er)
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from app.sources.files import collect_data_files, read_files
from app.sources.sqlfiles import collect_sql_files, read_sql_files
from expected import (COLUMN_COUNTS, DECLARED_KEYS, JOIN_ONLY, NAME_ONLY, TABLES, VALUE_MATCH, declared, links,
                      value_matches)


@pytest.fixture(scope='module')
def parts(demo_sql, demo_db, demo_csv):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)))
    out = {'db': db.read_schema('telecom.db', 'a')[0],
           'sql': read_sql_files(collect_sql_files(demo_sql), 'telecom.sql', 'b')[0],
           'csv': read_files(collect_data_files(demo_csv), 'csv', 'c')[0]}
    db.dispose()
    return out


def merged(parts, order):
    model = SchemaModel()
    for key in order:
        model.merge(SchemaModel.from_dict(parts[key].to_dict()))      # as the workspace does, from snapshots
    return finalize_model(model)


def table(model, name, columns, origin='file', pk=()):
    t = model.table(name)
    t.add_origin(Origin(origin))
    for c in columns:
        column, kind = c if isinstance(c, tuple) else (c, 'BIGINT')
        t.add_column(column, origin, type=kind, pk=column in pk)
    return t


# --------------------------------------------------------------- merged demo

def test_three_sources_merge_into_one_model(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    assert {t.name for t in model.tables} == TABLES and len(model.tables) == 10       # merged by name
    assert {t.name: len(t.columns) for t in model.tables} == COLUMN_COUNTS
    assert model.stats() == {'tables': 10, 'columns': 70, 'relationships': 16, 'declared': 13, 'joins': 2, 'values': 0, 'names': 1}
    assert declared(model) == DECLARED_KEYS
    assert links(model, JOINS) == {JOIN_ONLY}
    assert links(model, NAMES) == {NAME_ONLY}
    assert value_matches(model) == {VALUE_MATCH}


def test_merged_relationships_keep_their_best_source_and_all_evidence(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    by = {(r.from_table.name, r.from_columns[0], r.to_table.name): r for r in model.relationships}
    key = by[('invoices', 'account_id', 'accounts')]
    assert key.source == DECLARED and key.confidence is None and key.seen == [DECLARED, JOINS, NAMES]
    join = by[('payments', 'account_id', 'accounts')]
    assert join.source == JOINS and join.confidence == 0.85 and join.seen == [JOINS, NAMES]
    name = by[('tickets', 'branch_id', 'branches')]
    assert name.source == NAMES and name.confidence == 0.85 and name.cardinality == '1-N'
    match = next(r for r in model.relationships if r.cardinality == 'unknown')
    assert match.source == JOINS and match.confidence == 0.5


def test_merged_tables_remember_every_source(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    tickets = model.find('TICKETS')
    assert [(o.kind, o.source_id) for o in tickets.origins] == [('db', 'a'), ('ddl', 'b'), ('file', 'c')]
    assert tickets.aliases == ['t'] and tickets.complete
    branch = tickets.column('branch_id')
    assert branch.origins == ['db', 'ddl', 'file']
    assert branch.types == {'db': 'INTEGER', 'ddl': 'INTEGER', 'file': 'BIGINT'} and branch.type == 'INTEGER'
    assert (branch.fk, branch.fk_inferred) == (False, True)           # not declared, but it points at branches
    customer = tickets.column('customer_id')
    assert (customer.fk, customer.fk_inferred, customer.nullable) == (True, False, False)
    assert (tickets.column('ticket_id').pk, tickets.column('ticket_id').pk_inferred) == (True, False)


@pytest.mark.parametrize('order', [['db', 'sql', 'csv'], ['csv', 'sql', 'db'], ['sql', 'csv', 'db'], ['csv', 'db', 'sql']])
def test_merge_result_does_not_depend_on_source_order(parts, order):
    model = merged(parts, order)
    assert model.stats() == {'tables': 10, 'columns': 70, 'relationships': 16, 'declared': 13, 'joins': 2, 'values': 0, 'names': 1}
    assert declared(model) == DECLARED_KEYS and links(model, JOINS) == {JOIN_ONLY} and links(model, NAMES) == {NAME_ONLY}
    accounts = model.find('accounts')
    assert accounts.column('credit_limit').type == 'DECIMAL(10, 2)'  # the database's type wins over DDL and file
    assert [c.name for c in accounts.columns][:3] == ['account_id', 'customer_id', 'parent_account_id']


def test_csv_alone_then_with_ddl(parts):
    csv_only = merged(parts, ['csv'])
    assert csv_only.stats()['names'] == 15 and csv_only.stats()['declared'] == 0
    with_ddl = merged(parts, ['csv', 'sql'])
    assert with_ddl.stats() == {'tables': 10, 'columns': 70, 'relationships': 16, 'declared': 13, 'joins': 2, 'values': 0, 'names': 1}


# ----------------------------------------------------------------- table()

def test_tables_merge_by_name_case_insensitively_like_the_scripts():
    m = SchemaModel()
    a = m.table('ACCOUNT', 'RDB_OWNER')
    assert m.table('account') is a and m.table('Account', 'rdb_owner') is a      # "ACCOUNT" after "RDB_OWNER.ACCOUNT"
    b = m.table('customer')
    assert m.table('CUSTOMER', 'CRM') is b and b.schema == 'CRM'                  # "CRM.CUSTOMER" after "CUSTOMER"
    other = m.table('ACCOUNT', 'ARCHIVE')
    assert other is not a                                                         # a different schema is a different table
    assert m.table('account') is a                                                # already resolved once: stays that way
    assert [t.id for t in m.tables] == ['RDB_OWNER.ACCOUNT', 'CRM.customer', 'ARCHIVE.ACCOUNT']
    assert m.find('crm.CUSTOMER') is b and m.find('customer') is b and m.find('nope') is None

    n = SchemaModel()
    first, second = n.table('T', 'S1'), n.table('T', 'S2')
    assert n.table('t') not in (first, second)                                    # ambiguous from the start: its own table
    assert [t.id for t in n.tables] == ['S1.T', 'S2.T', 't']


def test_merging_models_with_and_without_schema():
    db = SchemaModel()
    table(db, 'ORDERS', [('ORDER_ID', 'NUMBER(10, 0)'), ('CUSTOMER_ID', 'NUMBER(10, 0)')], 'db', pk=['ORDER_ID']).schema = 'SALES'
    db.by_key = {t.key: t for t in db.tables}
    files = SchemaModel()
    table(files, 'orders', [('order_id', 'BIGINT'), ('customer_id', 'BIGINT'), ('note', 'VARCHAR')])
    model = SchemaModel().merge(db).merge(files)
    (orders,) = model.tables
    assert orders.id == 'SALES.ORDERS'
    assert [c.name for c in orders.columns] == ['ORDER_ID', 'CUSTOMER_ID', 'note']    # new columns are appended
    assert orders.column('order_id').types == {'db': 'NUMBER(10, 0)', 'file': 'BIGINT'}
    assert orders.column('note').origins == ['file']


def test_first_complete_source_sets_the_column_order():
    query = SchemaModel()
    table(query, 't', ['c', 'a'], 'query')
    ddl = SchemaModel()
    table(ddl, 't', ['a', 'b', 'c'], 'ddl')
    model = SchemaModel().merge(query).merge(ddl)
    assert [c.name for c in model.find('t').columns] == ['a', 'b', 'c']
    assert model.find('t').column('c').origins == ['ddl', 'query']


# ---------------------------------------------------------------- relate()

def test_a_declared_key_wins_whatever_the_order():
    for order in ([NAMES, JOINS, DECLARED], [DECLARED, JOINS, NAMES], [JOINS, DECLARED, NAMES]):
        m = SchemaModel()
        child, parent = table(m, 'child', ['id', 'parent_id']), table(m, 'parent', ['id'])
        for source in order:
            if source == JOINS:                    # a join does not know the direction: given the other way round
                m.relate(parent, ['id'], child, ['parent_id'], JOINS, 0.5)
            else:
                m.relate(child, ['parent_id'], parent, ['id'], source, 0.8 if source == NAMES else None, '1-N')
        (r,) = m.relationships
        assert (r.source, r.from_table, r.to_table, r.cardinality, r.confidence) == (DECLARED, child, parent, '1-N', None)
        assert r.seen == [DECLARED, JOINS, NAMES]


def test_relationships_are_matched_by_their_column_pairs():
    m = SchemaModel()
    a, b = table(m, 'a', ['x', 'y', 'z']), table(m, 'b', ['p', 'q'])
    first = m.relate(a, ['x', 'y'], b, ['p', 'q'], DECLARED, cardinality='1-N')
    assert m.relate(a, ['Y', 'X'], b, ['Q', 'P'], DECLARED, cardinality='1-N') is first     # same key, other order
    assert m.relate(b, ['p', 'q'], a, ['x', 'y'], JOINS, 0.5) is first                       # same key, other direction
    assert m.relate(a, ['x'], b, ['p'], JOINS, 0.5) is not first                             # one column is another link
    assert m.relate(a, ['x'], a, ['x'], JOINS, 0.5) is None                                  # a column with itself
    assert m.relate(a, ['x'], a, ['z'], JOINS, 0.5) is not None                              # self-reference is fine
    assert first.from_columns == ['x', 'y'] and len(m.relationships) == 3


def test_round_trip_through_a_snapshot(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    again = finalize_model(SchemaModel.from_dict(model.to_dict()))
    assert again.to_dict() == model.to_dict()


# ------------------------------------------------------------------ inference

def test_names_inference_examples():
    m = SchemaModel()
    table(m, 'customers', ['id', 'name'])
    table(m, 'orders', ['order_id', 'customer_id', 'billing_customer_id', 'parent_order_id', 'status_code'])
    table(m, 'order_lines', ['order_id', 'line_no', 'product_id'])
    table(m, 'branches', ['branch_id'])
    table(m, 'visits', ['visit_id', 'branch_id', ('customer_id', 'VARCHAR')])
    finalize_model(m)
    found = {(r.from_table.name, r.from_columns[0]): (r.to_table.name, r.to_columns[0], r.confidence) for r in m.relationships}
    assert found == {
        ('orders', 'customer_id'): ('customers', 'id', 0.85),                # the example from the brief
        ('orders', 'billing_customer_id'): ('customers', 'id', 0.65),        # prefixed name
        ('orders', 'parent_order_id'): ('orders', 'order_id', 0.65),         # self-reference
        ('order_lines', 'order_id'): ('orders', 'order_id', 0.85),
        ('visits', 'branch_id'): ('branches', 'branch_id', 0.85),            # "-es" plural
        ('visits', 'customer_id'): ('customers', 'id', 0.55),                # text vs number: less sure
    }
    assert all(r.source == NAMES and r.cardinality == '1-N' for r in m.relationships)
    orders = {c.name: (c.pk_inferred, c.fk_inferred) for c in m.find('orders').columns}
    assert orders == {'order_id': (True, False), 'customer_id': (False, True), 'billing_customer_id': (False, True),
                      'parent_order_id': (False, True), 'status_code': (False, False)}


def test_names_inference_does_not_guess_when_unsure():
    m = SchemaModel()
    table(m, 'account', ['account_id'])
    table(m, 'accounts', ['account_id'])
    table(m, 'usage', ['usage_id', 'account_id', 'device_id', 'id'])
    assert infer_by_names(m) == []              # two tables could own account_id; no table owns device_id


def test_names_inference_leaves_declared_keys_alone():
    m = SchemaModel()
    customers = table(m, 'customers', ['customer_id'], 'ddl', pk=['customer_id'])
    legacy = table(m, 'legacy_customers', ['customer_id'], 'ddl', pk=['customer_id'])
    orders = table(m, 'orders', ['order_id', 'customer_id'], 'ddl', pk=['order_id'])
    m.relate(orders, ['customer_id'], legacy, ['customer_id'], DECLARED, cardinality='1-N')
    finalize_model(m)
    assert [(r.from_table, r.to_table, r.source) for r in m.relationships] == [(orders, legacy, DECLARED)]
    assert customers.column('customer_id').pk_inferred is False


def test_part_of_a_composite_key_can_point_elsewhere():
    m = SchemaModel()
    table(m, 'invoices', ['invoice_id'], 'db', pk=['invoice_id'])
    table(m, 'invoice_lines', ['invoice_id', 'line_no'], 'db', pk=['invoice_id', 'line_no'])
    finalize_model(m)
    assert links(m, NAMES) == {('invoice_lines', 'invoice_id', 'invoices', 'invoice_id')}


def test_join_direction_is_resolved_after_merging():
    queries = SchemaModel()
    a, b = table(queries, 'orders', ['customer_ref'], 'query'), table(queries, 'customers', ['customer_id'], 'query')
    queries.relate(a, ['customer_ref'], b, ['customer_id'], JOINS, 0.5)          # direction unknown so far
    ddl = SchemaModel()
    table(ddl, 'customers', ['customer_id', 'name'], 'ddl', pk=['customer_id'])
    model = finalize_model(SchemaModel().merge(queries).merge(ddl))
    (r,) = model.relationships
    assert (r.from_table.name, r.to_table.name, r.cardinality, r.confidence) == ('orders', 'customers', '1-N', 0.85)


# ---------------------------------------------------------------------- views

def test_view_filters(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    tables, rels = model.view()
    assert (len(tables), len(rels)) == (10, 16)
    assert len(model.view(kinds={DECLARED})[1]) == 13
    assert len(model.view(kinds={JOINS, NAMES})[1]) == 3
    assert len(model.view(kinds=set())[1]) == 0
    assert len(model.view(min_confidence=0.6)[1]) == 15          # drops the 50% value match
    assert len(model.view(min_confidence=0.9)[1]) == 13          # declared keys always stay
    assert model.view(schemas=['SALES'])[0] == []
    assert len(model.view(schemas=[''])[0]) == 10                # '' = tables without a schema


def test_focus_view_is_n_hops_around_a_table(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    accounts = model.find('accounts')
    tables, rels = model.view(focus=accounts, hops=1)
    assert {t.name for t in tables} == {'accounts', 'customers', 'invoices', 'payments', 'subscriptions', 'tickets'}
    assert len(rels) == 9                                        # links among those six, as the page shows
    assert len(model.view(focus=accounts, hops=2)[0]) == 10
    plans = model.find('plans')
    assert {t.name for t in model.view(focus=plans, hops=1)[0]} == {'plans', 'subscriptions'}
    assert {t.name for t in model.view(focus=plans, hops=2)[0]} == {
        'plans', 'subscriptions', 'accounts', 'devices', 'invoice_lines', 'tickets'}
    # without the inferred links, tickets no longer reaches branches or accounts directly
    tickets = model.find('tickets')
    assert {t.name for t in model.view(kinds={DECLARED}, focus=tickets, hops=1)[0]} == {'tickets', 'customers', 'subscriptions'}
    assert {t.name for t in model.view(focus=tickets, hops=1)[0]} == {'tickets', 'customers', 'subscriptions', 'branches', 'accounts'}


def test_bridge_to_the_script_model(parts):
    model = merged(parts, ['db', 'sql', 'csv'])
    em, emap = to_er(model, flags=True)
    assert len(em.tables) == 10 and len(em.rels) == 16
    assert sum(r.declared for r in em.rels.values()) == 13
    assert sum(r.one is None for r in em.rels.values()) == 1
    tickets = emap[model.find('tickets')]
    assert tickets.from_ddl and tickets.ddl_pk and tickets.aliases == ['t']
    assert tickets.columns['BRANCH_ID'].show_fk and not tickets.columns['BRANCH_ID'].fk
