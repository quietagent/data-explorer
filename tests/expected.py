"""What the demo telecom model contains. See app/demo.py."""

from app.model import DECLARED

COLUMN_COUNTS = {'branches': 5, 'customers': 9, 'accounts': 8, 'plans': 7, 'subscriptions': 7,
                 'devices': 6, 'invoices': 6, 'invoice_lines': 6, 'payments': 7, 'tickets': 9}
TABLES = set(COLUMN_COUNTS)
PRIMARY_KEYS = {'branches': 'branch_id', 'customers': 'customer_id', 'accounts': 'account_id',
                'plans': 'plan_id', 'subscriptions': 'subscription_id', 'devices': 'device_id',
                'invoices': 'invoice_id', 'invoice_lines': 'invoice_line_id', 'payments': 'payment_id',
                'tickets': 'ticket_id'}

# (table, column) -> (referenced table, column): the 13 foreign keys declared in the DDL / database
DECLARED_KEYS = {
    ('customers', 'branch_id', 'branches', 'branch_id'),
    ('accounts', 'customer_id', 'customers', 'customer_id'),
    ('accounts', 'parent_account_id', 'accounts', 'account_id'),
    ('subscriptions', 'account_id', 'accounts', 'account_id'),
    ('subscriptions', 'plan_id', 'plans', 'plan_id'),
    ('devices', 'subscription_id', 'subscriptions', 'subscription_id'),
    ('invoices', 'account_id', 'accounts', 'account_id'),
    ('invoice_lines', 'invoice_id', 'invoices', 'invoice_id'),
    ('invoice_lines', 'subscription_id', 'subscriptions', 'subscription_id'),
    ('payments', 'invoice_id', 'invoices', 'invoice_id'),
    ('payments', 'branch_id', 'branches', 'branch_id'),
    ('tickets', 'customer_id', 'customers', 'customer_id'),
    ('tickets', 'subscription_id', 'subscriptions', 'subscription_id'),
}
# Not declared anywhere: one shows up in a JOIN of telecom.sql, the other only in its name.
JOIN_ONLY = ('payments', 'account_id', 'accounts', 'account_id')
NAME_ONLY = ('tickets', 'branch_id', 'branches', 'branch_id')
# A JOIN on two foreign keys: related by value, neither side is the key.
VALUE_MATCH = frozenset({('accounts', 'customer_id'), ('tickets', 'customer_id')})


def links(model, source=None):
    """Directed single-column relationships as (table, column, referenced table, column)."""
    return {(r.from_table.name, r.from_columns[0], r.to_table.name, r.to_columns[0])
            for r in model.relationships
            if r.cardinality != 'unknown' and (source is None or r.source == source)}


def value_matches(model):
    return {frozenset({(r.from_table.name, r.from_columns[0]), (r.to_table.name, r.to_columns[0])})
            for r in model.relationships if r.cardinality == 'unknown'}


def declared(model):
    return links(model, DECLARED)
