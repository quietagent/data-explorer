"""The SQL tab, masking by table: which table columns a result column comes from (app/analysis/lineage.py).

Only the reading of statements is tested here, against a structure made by hand. What the SQL tab
then masks is tested through the app in test_zephyr.py (the answer key) and test_sqlrunner.py.

A result is written down as one text per column:

    store.name                          that column as it is (under its own name or another)
    store.name | customer.first_name    as it is, in the branches of a UNION
    f(employee.salary, store.city)      built from those columns
    f()                                 built from no column: a constant
    count                               COUNT(...) and nothing else
    ?                                   not known: the SQL tab judges it by its name, as before

and None stands for "nothing of the result is known".
"""

import pytest

from app.analysis.lineage import WORDS, _Lineage, only_counts, origins
from app.model import Origin, SchemaModel
from app.sqlguard import ENDS_AN_EXPRESSION, Known, ReadOnlyViolation, _Names, check, tokenize

TABLES = {
    'store': ['store_id', 'name', 'city', 'phone'],
    'employee': ['employee_id', 'first_name', 'last_name', 'salary', 'store_id', 'email'],
    'customer': ['customer_id', 'first_name', 'email', 'store_id'],
    'product': ['product_id', 'name', 'price'],
}


def structure(tables=None, schema=None, upper=False):
    """A source's structure: {'[schema.]table': [columns]}."""
    model = SchemaModel()
    for name, columns in (TABLES if tables is None else tables).items():
        own, _, name = name.rpartition('.')
        own = own or schema
        if upper:
            name, own, columns = name.upper(), own.upper() if own else own, [c.upper() for c in columns]
        table = model.table(name, own)
        table.add_origin(Origin('db', 'src', 'x', schema=own, table=name))
        for column in columns:
            table.add_column(column, 'db')
    return model


def text(origin):
    if origin is None:
        return '?'
    if origin.count:
        return 'count'
    names = [f'{table.id}.{column}' for table, column in origin.columns]
    return ' | '.join(names) if origin.plain else f'f({", ".join(names)})'


def read(sql, dialect='sqlite', model=None, names=None):
    """What origins() says about a statement the guard has passed."""
    model = model if model is not None else structure(upper=dialect == 'oracle', schema={'mssql': 'dbo', 'postgresql': 'shop'}.get(dialect))
    found = origins(check(sql, dialect).tokens, dialect, model, names)
    return None if found is None else [text(o) for o in found]


# ------------------------------------------------------------- what is followed

@pytest.mark.parametrize('sql, expected', [
    ('SELECT name FROM store', ['store.name']),
    ('SELECT s.name AS shop, e.first_name FROM store s JOIN employee e ON e.store_id = s.store_id', ['store.name', 'employee.first_name']),
    # without a table in front of it: the one table of the FROM clause that has it
    ('SELECT name, first_name, city FROM store s JOIN employee e ON e.store_id = s.store_id',
     ['store.name', 'employee.first_name', 'store.city']),
    ('SELECT store.name, store.city FROM store', ['store.name', 'store.city']),
    ('SELECT "name", [city], `phone` FROM "store"', ['store.name', 'store.city', 'store.phone']),
    ('SELECT NAME FROM STORE', ['store.name']),
    ('SELECT main.store.name, x.city FROM main.store, main.store x', ['store.name', 'store.city']),
    ('SELECT DISTINCT name FROM store', ['store.name']),
    ('SELECT ALL first_name FROM employee', ['employee.first_name']),
    # a subquery in FROM, with and without a name of its own
    ('SELECT name FROM (SELECT first_name AS name FROM customer)', ['customer.first_name']),
    ('SELECT x.name FROM (SELECT first_name AS name FROM customer) x', ['customer.first_name']),
    ('SELECT x FROM (SELECT y AS x FROM (SELECT phone AS y FROM store))', ['store.phone']),
    # WITH: its name hides a table of that name
    ('WITH store AS (SELECT first_name AS name FROM customer) SELECT name FROM store', ['customer.first_name']),
    ('WITH p (a, b) AS (SELECT first_name, store_id FROM customer) SELECT b, a FROM p', ['customer.store_id', 'customer.first_name']),
    ('WITH x AS (SELECT name FROM store), y AS (SELECT name FROM x) SELECT name FROM y', ['store.name']),
    ('SELECT name FROM (WITH store AS (SELECT first_name AS name FROM customer) SELECT name FROM store)', ['customer.first_name']),
    # every branch of a UNION
    ('SELECT name FROM store UNION SELECT first_name FROM customer', ['store.name | customer.first_name']),
    ('SELECT name FROM store UNION ALL SELECT name FROM product ORDER BY 1', ['store.name | product.name']),
    ('(SELECT name FROM store) UNION ALL (SELECT first_name FROM customer) ORDER BY 1', ['store.name | customer.first_name']),
    ('SELECT name FROM store EXCEPT SELECT first_name FROM customer', ['store.name | customer.first_name']),
    ('SELECT name FROM store UNION SELECT UPPER(first_name) FROM customer', ['f(store.name, customer.first_name)']),
    # * and t.*: from the structure, in its order
    ('SELECT * FROM store', ['store.store_id', 'store.name', 'store.city', 'store.phone']),
    ('SELECT p.*, s.name FROM store s, product p', ['product.product_id', 'product.name', 'product.price', 'store.name']),
    ('SELECT * FROM product p JOIN store s ON 1 = 1',
     ['product.product_id', 'product.name', 'product.price', 'store.store_id', 'store.name', 'store.city', 'store.phone']),
    ('SELECT * FROM (SELECT first_name AS name, 1 AS one, COUNT(email) AS n FROM customer) x', ['customer.first_name', 'f()', 'count']),
    ('SELECT e.* FROM store s JOIN employee e USING (store_id)',
     [f'employee.{c}' for c in TABLES['employee']]),
])
def test_a_result_column_is_followed_to_its_table(sql, expected):
    assert read(sql) == expected


@pytest.mark.parametrize('sql, expected', [
    # functions, CASE, arithmetic, COALESCE, aggregates: built from their columns
    ("SELECT COALESCE(s.name, 'Online') AS store, ROUND(SUM(e.salary * 12 * (1 - 0.1)), 2) AS pay "
     'FROM store s LEFT JOIN employee e ON e.store_id = s.store_id GROUP BY 1', ['f(store.name)', 'f(employee.salary)']),
    ("SELECT CASE WHEN salary > 60000 THEN 'high' ELSE 'low' END AS band, UPPER(city) "
     'FROM employee e JOIN store s ON s.store_id = e.store_id', ['f(employee.salary)', 'f(store.city)']),
    ("SELECT name || ' ' || city AS label, price * 2 AS twice FROM store s, product p WHERE 1 = 1",
     ['?', 'f(product.price)']),                                    # name: store and product both have one
    ('SELECT CAST(salary AS INTEGER) AS s, CAST(price AS TEXT) FROM employee, product', ['f(employee.salary)', 'f(product.price)']),
    ("SELECT first_name IS NULL, salary BETWEEN 1 AND 2, email LIKE 'a%' ESCAPE 'x' FROM employee",
     ['f(employee.first_name)', 'f(employee.salary)', 'f(employee.email)']),
    ('SELECT SUM(salary) OVER (PARTITION BY store_id ORDER BY employee_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) '
     'FROM employee', ['f(employee.salary, employee.store_id, employee.employee_id)']),
    ('SELECT name, COUNT(*) FILTER (WHERE price > 5) FROM product GROUP BY name', ['product.name', 'f(product.price)']),
    ("SELECT 1 AS one, 'x' AS phone, NULL AS first_name, CURRENT_DATE AS today", ['f()', 'f()', 'f()', 'f()']),
    # a count, and nothing but a count
    ('SELECT COUNT(salary), COUNT(DISTINCT email), COUNT(*), COUNT(salary) + 1, MAX(first_name) FROM employee',
     ['count', 'count', 'count', 'f(employee.salary)', 'f(employee.first_name)']),
    ('SELECT n, n + 1 FROM (SELECT COUNT(email) AS n FROM customer)', ['count', 'f()']),
    ('SELECT COUNT(salary) FROM employee UNION ALL SELECT COUNT(email) FROM employee', ['count']),
    ('SELECT COUNT(*) AS n FROM employee UNION ALL SELECT salary FROM employee', ['f(employee.salary)']),
    # a subquery inside an expression: everything it reads, its conditions too
    ('SELECT (SELECT MAX(e.salary) FROM employee e WHERE e.store_id = s.store_id) AS top FROM store s',
     ['f(employee.salary, employee.store_id, store.store_id)']),
    ("SELECT (SELECT 'high' FROM employee e WHERE e.salary > 100000 AND e.store_id = s.store_id) AS band FROM store s",
     ['f(employee.salary, employee.store_id, store.store_id)']),
    ('SELECT (SELECT COUNT(*) FROM employee e WHERE e.store_id = s.store_id) AS staff FROM store s',
     ['f(employee.store_id, store.store_id)']),
    ('SELECT (SELECT COUNT(e.email) FROM employee e WHERE e.store_id = s.store_id) AS staff FROM store s',
     ['f(employee.store_id, store.store_id)']),                    # a count there too: not built from the e-mail
    ('SELECT (SELECT COUNT(e.email) + MAX(e.salary) FROM employee e) AS n FROM store s', ['f(employee.email, employee.salary)']),
    # EXISTS: whether there are rows, not what they hold - but the conditions that pick them count
    ('SELECT CASE WHEN EXISTS (SELECT * FROM employee e WHERE e.store_id = s.store_id) THEN 1 ELSE 0 END AS staffed FROM store s',
     ['f(employee.store_id, store.store_id)']),
    ("SELECT EXISTS (SELECT first_name FROM employee e WHERE e.salary > 100000 AND e.store_id = s.store_id) FROM store s",
     ['f(employee.salary, employee.store_id, store.store_id)']),
    ('SELECT store_id IN (SELECT e.store_id FROM employee e WHERE e.first_name IS NOT NULL) FROM store s',
     ['f(store.store_id, employee.store_id, employee.first_name)']),
    ('SELECT (SELECT s.name FROM store s WHERE s.store_id = e.store_id) AS store FROM employee e',
     ['f(store.name, store.store_id, employee.store_id)']),
    ('SELECT (SELECT name FROM customer c LIMIT 1) FROM store', ['f(store.name)']),      # customer has no name: the store's
    ('WITH s AS (SELECT first_name AS name FROM customer) SELECT (SELECT name FROM s LIMIT 1) AS x FROM store',
     ['f(customer.first_name)']),
    ('SELECT first_name AS name, name FROM customer, store', ['customer.first_name', 'store.name']),    # an alias is not a column
])
def test_what_an_expression_is_built_from(sql, expected):
    assert read(sql) == expected


# ------------------------------------------------- names, as each engine keeps them

def test_oracle_keeps_names_in_upper_case_and_quoted_names_as_written():
    assert read('SELECT name, s.city FROM store s', 'oracle') == ['STORE.NAME', 'STORE.CITY']
    assert read('SELECT "NAME" FROM "STORE"', 'oracle') == ['STORE.NAME']
    assert read('SELECT "name" FROM store', 'oracle') == ['?']                   # another name than NAME
    assert read('SELECT name FROM "store"', 'oracle') == ['?']
    assert read('SELECT e.first_name FROM employee e, store s WHERE e.store_id = s.store_id (+)', 'oracle') == ['EMPLOYEE.FIRST_NAME']
    assert read("SELECT NVL(s.name, 'x') shop, s.name || first_name FROM store s, employee", 'oracle') == [
        'f(STORE.NAME)', 'f(STORE.NAME, EMPLOYEE.FIRST_NAME)']
    assert read('SELECT sysdate, 1 FROM dual', 'oracle') == ['f()', 'f()']
    assert read('SELECT name FROM store MINUS SELECT first_name FROM customer', 'oracle') == ['STORE.NAME | CUSTOMER.FIRST_NAME']
    assert read('SELECT LISTAGG(first_name, \',\') WITHIN GROUP (ORDER BY employee_id), MAX(salary) KEEP (DENSE_RANK FIRST '
                'ORDER BY employee_id) FROM employee', 'oracle') == [
        'f(EMPLOYEE.FIRST_NAME, EMPLOYEE.EMPLOYEE_ID)', 'f(EMPLOYEE.SALARY, EMPLOYEE.EMPLOYEE_ID)']
    assert read('SELECT s.name FROM store s FETCH FIRST 5 ROWS ONLY', 'oracle') == ['STORE.NAME']
    # with the owner in front, as the catalog has it
    owned = structure({'telecom.customers': ['customer_id', 'full_name'], 'telecom.branches': ['branch_id', 'name']}, upper=True)
    assert read('SELECT c.full_name, b.name FROM telecom.customers c, TELECOM.BRANCHES b', 'oracle', owned) == [
        'TELECOM.CUSTOMERS.FULL_NAME', 'TELECOM.BRANCHES.NAME']
    assert read('SELECT telecom.branches.name, name FROM telecom.branches', 'oracle', owned) == ['TELECOM.BRANCHES.NAME'] * 2
    assert read('SELECT name FROM other.branches', 'oracle', owned) == ['?']        # another owner's table


def test_postgresql_keeps_names_in_lower_case_and_quoted_names_as_written():
    assert read('SELECT Name, S.City FROM Store S', 'postgresql') == ['shop.store.name', 'shop.store.city']
    assert read('SELECT "name" FROM "store"', 'postgresql') == ['shop.store.name']
    assert read('SELECT "Name" FROM store', 'postgresql') == ['?']
    assert read('SELECT name FROM "Store"', 'postgresql') == ['?']
    assert read('SELECT shop.store.name FROM shop.store', 'postgresql') == ['shop.store.name']
    assert read('SELECT name::text, salary::numeric(10, 2) FROM store, employee', 'postgresql') == [
        'f(shop.store.name)', 'f(shop.employee.salary)']
    assert read('SELECT DISTINCT ON (store_id) first_name FROM employee', 'postgresql') == ['shop.employee.first_name']
    assert read("SELECT extract(year FROM now()), trim(both ' ' FROM name) FROM store", 'postgresql') == ['f()', 'f(shop.store.name)']
    assert read('SELECT e.first_name FROM store s, LATERAL (SELECT first_name FROM employee e2 WHERE e2.store_id = s.store_id) e',
                'postgresql') == ['shop.employee.first_name']
    assert read('SELECT name FROM ONLY store TABLESAMPLE SYSTEM (5)', 'postgresql') == ['shop.store.name']
    mixed = structure({'shop.Staff': ['Id', 'FullName']})
    assert read('SELECT "FullName" FROM "Staff"', 'postgresql', mixed) == ['shop.Staff.FullName']
    assert read('SELECT fullname FROM staff', 'postgresql', mixed) == ['?']


def test_a_table_used_as_a_value_is_every_column_of_it():
    """PostgreSQL and DuckDB give the whole row for the name of a table: SELECT c FROM customers c."""
    everything = ', '.join(f'shop.customer.{c}' for c in TABLES['customer'])
    assert read('SELECT c FROM customer c', 'postgresql') == [f'f({everything})']
    assert read('SELECT c::text, c.store_id FROM customer c', 'postgresql') == [f'f({everything})', 'shop.customer.store_id']
    assert read('SELECT employee FROM employee', 'duckdb') == ['f(' + ', '.join(f'employee.{c}' for c in TABLES['employee']) + ')']
    assert read('SELECT first_name.x FROM employee', 'duckdb') == ['f(employee.first_name)']          # a part of a column
    assert read('SELECT nobody.name, level.name FROM store', 'duckdb') == ['?', '?']                  # not a table, not a column


def test_sql_server_top_brackets_schemas_and_table_hints():
    assert read('SELECT TOP 5 name FROM store', 'mssql') == ['dbo.store.name']
    assert read('SELECT TOP (5) PERCENT WITH TIES first_name FROM employee ORDER BY 1', 'mssql') == ['dbo.employee.first_name']
    assert read('SELECT DISTINCT TOP 5 s.name, e.first_name FROM store s JOIN employee e ON e.store_id = s.store_id', 'mssql') == [
        'dbo.store.name', 'dbo.employee.first_name']
    assert read('SELECT TOP 5 * FROM product', 'mssql') == ['dbo.product.product_id', 'dbo.product.name', 'dbo.product.price']
    assert read('SELECT [name], [dbo].[store].[city] FROM [dbo].[store]', 'mssql') == ['dbo.store.name', 'dbo.store.city']
    assert read('SELECT telecom.dbo.store.name FROM telecom.dbo.store', 'mssql') == ['dbo.store.name']
    assert read('SELECT s.name, e.first_name FROM store AS s WITH (NOLOCK) JOIN employee e WITH (NOLOCK) ON e.store_id = s.store_id',
                'mssql') == ['dbo.store.name', 'dbo.employee.first_name']
    assert read('SELECT s.name, x.first_name FROM store s CROSS APPLY (SELECT TOP 1 first_name FROM employee e '
                'WHERE e.store_id = s.store_id) x', 'mssql') == ['dbo.store.name', 'dbo.employee.first_name']
    assert read('SELECT CONVERT(VARCHAR(10), salary) FROM employee', 'mssql') == ['f(dbo.employee.salary)']
    assert read('SELECT name FROM other.store', 'mssql') == ['?']
    # what is not followed: "alias = column", a type as an argument
    assert read('SELECT who = first_name FROM employee', 'mssql') == ['?']
    assert read('SELECT CONVERT(INT, salary) FROM employee', 'mssql') == ['?']


def test_mysql_backticks_and_a_database_in_front():
    shop = structure(schema='shop')
    assert read('SELECT `name`, s.`city` FROM `store` s LIMIT 5', 'mysql', shop) == ['shop.store.name', 'shop.store.city']
    assert read('SELECT shop.store.name FROM shop.store', 'mysql', shop) == ['shop.store.name']
    assert read('SELECT Name FROM Store', 'mysql', shop) == ['shop.store.name']


def test_a_table_that_two_schemas_have_is_only_known_with_its_schema():
    two = structure({'a.t': ['name'], 'b.t': ['name'], 'b.u': ['name']})
    assert read('SELECT name FROM t', 'mysql', two) == ['?']
    assert read('SELECT name FROM b.t', 'mysql', two) == ['b.t.name']
    assert read('SELECT t.name, x.name FROM a.t, b.t x', 'mysql', two) == ['a.t.name', 'b.t.name']
    assert read('SELECT name FROM u', 'mysql', two) == ['b.u.name']


# ------------------------------------------------------- what is not resolved

@pytest.mark.parametrize('sql, expected', [
    ('SELECT name FROM nowhere', ['?']),                                             # a table the source does not know
    ('SELECT s.name, n.thing FROM store s JOIN nowhere n ON 1 = 1', ['store.name', '?']),
    ('SELECT name FROM store s JOIN nowhere n ON 1 = 1', ['?']),                     # it may be the unknown table's
    ('SELECT name FROM store, product', ['?']),                                      # two tables have it
    ('SELECT first_name FROM employee e JOIN employee m ON m.employee_id = e.employee_id', ['?']),
    ('SELECT store_id FROM store s JOIN employee e USING (store_id)', ['?']),
    ('SELECT nope FROM store', ['?']),
    ('SELECT s.nope FROM store s', ['?']),
    ('SELECT x.name FROM store s', ['?']),
    ('SELECT s.name FROM store s, (SELECT first_name AS name FROM customer) s', ['?']),      # two tables under one name
    ('SELECT x.name FROM (SELECT s.name, c.first_name AS name FROM store s, customer c) x', ['?']),    # two columns of that name
    ('SELECT name COLLATE nocase FROM store', ['?']),
    ('SELECT name AS n, n || \'!\' AS m FROM store', ['store.name', '?']),           # a column alias used as a value
    ('SELECT SUM(price) OVER w FROM product WINDOW w AS (ORDER BY name)', ['?']),
    ('SELECT "hello" FROM store', ['?']),
    ('SELECT rowid, name FROM store', ['?', 'store.name']),
    ('SELECT name FROM store UNION SELECT name FROM nowhere', ['?']),
    # WITH: a query that reads itself, and one that stands further down the list (SQLite reads it there, and then
    # it hides the table of that name: "a" below gives the customers' first names)
    ('WITH RECURSIVE n (x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT x FROM n', ['?']),
    ('WITH store AS (SELECT name FROM store) SELECT name FROM store', ['?']),
    ('WITH a AS (SELECT name FROM store), store AS (SELECT first_name AS name FROM customer) SELECT name FROM a', ['?']),
    # a * whose columns are not all known, or that the engine merges
    ('SELECT * FROM store s JOIN nowhere n ON 1 = 1', None),
    ('SELECT * FROM store s JOIN employee e USING (store_id)', None),
    ('SELECT * FROM store NATURAL JOIN employee', None),
    ('SELECT * FROM (VALUES (1, \'a\')) AS v (id, name)', None),
    ('SELECT x.* FROM store s', None),
    ('SELECT name FROM store UNION SELECT * FROM nowhere', None),
])
def test_what_cannot_be_resolved_for_sure_is_not_guessed(sql, expected):
    assert read(sql) == expected


def test_nothing_is_known_without_a_structure():
    assert read('SELECT name, 1 AS one FROM store', model=SchemaModel()) == ['?', 'f()']
    assert read('SELECT * FROM store', model=SchemaModel()) is None
    # a table that is only known as the target of a foreign key has no columns that were read
    referred = SchemaModel()
    referred.table('store').add_origin(Origin('ref', 'src', 'x'))
    referred.table('store').add_column('name', 'ref')
    assert read('SELECT name FROM store', model=referred) == ['?']


@pytest.mark.parametrize('dialect, sql', [
    # a word in front of a select list that this engine does not have: never read as "<word> <number>, called first_name"
    ('mysql', 'SELECT TOP 10 first_name FROM employee'),
    ('sqlite', 'SELECT TOP 10 first_name FROM employee'),
    ('postgresql', 'SELECT TOP 10 first_name FROM employee'),
    ('mysql', 'SELECT SQL_NO_CACHE first_name FROM employee'),
    ('mysql', 'SELECT DISTINCTROW first_name FROM employee'),
    ('mysql', 'SELECT STRAIGHT_JOIN e.first_name FROM employee e'),
    ('mysql', 'SELECT HIGH_PRIORITY first_name FROM employee'),
    # operators written as words
    ('mysql', 'SELECT 1000 DIV salary FROM employee'),
    ('mysql', 'SELECT BINARY first_name FROM employee'),
    ('mysql', 'SELECT 5 MOD salary FROM employee'),
    ('oracle', 'SELECT CONNECT_BY_ROOT first_name FROM employee CONNECT BY PRIOR employee_id = store_id'),
    ('mysql', "SELECT GROUP_CONCAT(first_name SEPARATOR ',') FROM employee"),
])
def test_a_word_that_is_not_understood_is_never_skipped(dialect, sql):
    """Each of these shows a personal column. Read wrongly, its name would become an alias of a constant."""
    assert read(sql, dialect) == ['?']


def test_the_words_that_may_be_skipped_never_turn_a_column_into_an_alias():
    """A name behind one of WORDS is not an alias to the reader of the select list - except behind a
    word that is a complete value (NULL, TRUE, SYSDATE ...), where it really is one."""
    values = {'END', 'NULL', 'TRUE', 'FALSE', 'SYSDATE', 'SYSTIMESTAMP', 'CURRENT_DATE', 'CURRENT_TIMESTAMP', 'LOCALTIMESTAMP',
              'USER', 'ROWNUM', 'LEVEL'}
    reader = _Names(tokenize('SELECT 1', 'oracle'), 'oracle', Known())
    for word in sorted(WORDS):
        (tok,) = tokenize(word, 'oracle')
        assert reader._word(tok), word                              # a word of the language to the guard's reader
        assert reader._ends(tok) is (word in values), word          # only a value ends an expression
    assert WORDS & ENDS_AN_EXPRESSION == values - {'END'} | {'END'} and 'CONNECT_BY_ROOT' not in WORDS
    for never in ('TOP', 'DISTINCTROW', 'DIV', 'MOD', 'BINARY', 'COLLATE', 'XOR', 'PERCENT', 'WITH', 'AS'):
        assert never not in WORDS, never
    # ... and with one of them in front, the column behind it is read as a column
    assert read('SELECT NOT first_name, NULL first_name FROM employee') == ['f(employee.first_name)', 'f()']
    assert read('SELECT PRIOR first_name FROM employee CONNECT BY PRIOR employee_id = store_id', 'oracle') == ['f(EMPLOYEE.FIRST_NAME)']


@pytest.mark.parametrize('dialect, sql', [
    ('duckdb', 'SELECT name FROM store POSITIONAL JOIN product'),
    ('duckdb', 'SELECT * FROM store s SEMI JOIN employee e ON e.store_id = s.store_id'),
    ('duckdb', 'SELECT name FROM store QUALIFY row_number() OVER () = 1'),
    ('mysql', 'SELECT name FROM store USE INDEX (x)'),
    ('mysql', 'SELECT name FROM store STRAIGHT_JOIN product'),
    ('sqlite', 'SELECT name FROM store INDEXED BY x'),
    ('postgresql', 'SELECT g FROM generate_series(1, 3) g'),
    ('oracle', 'SELECT name FROM store PIVOT (MAX(city) FOR phone IN (1))'),
    # a word the engine itself reserves is not taken for a table or an alias there
    ('oracle', 'SELECT name FROM public.store'),
    ('oracle', 'SELECT session.name FROM store session'),
    ('mssql', 'SELECT name FROM public.store'),
    ('mssql', 'SELECT s.name FROM store s JOIN employee key ON key.store_id = s.store_id'),
    ('mysql', 'SELECT s.name FROM store s JOIN employee key ON key.store_id = s.store_id'),
    ('mysql', 'SELECT rows.name FROM store rows'),
    ('postgresql', 'SELECT s.name FROM store s JOIN employee analyse ON analyse.store_id = s.store_id'),
    ('postgresql', 'SELECT user.name FROM store user'),
    ('sqlite', 'SELECT s.name FROM store s JOIN employee indexed ON 1 = 1'),
    ('duckdb', 'SELECT s.name FROM store s JOIN employee qualify ON 1 = 1'),
])
def test_a_from_clause_that_is_not_followed_gives_nothing(dialect, sql):
    found = read(sql, dialect)
    assert found is None or set(found) == {'?'}


# The words of the brief of 13 Oct 2026: reserved on Oracle, plain names on the other engines (SQL Server
# reserves PUBLIC too). Until then Oracle's list was the reader's list on every engine.
ORACLE_ONLY = ['public', 'session', 'comment', 'level', 'resource', 'size']
PREFIX = {'postgresql': 'shop.', 'mssql': 'dbo.', 'mysql': '', 'sqlite': '', 'duckdb': ''}


def test_postgresql_follows_a_table_of_the_public_schema():
    public = structure({'public.store': TABLES['store'], 'public.customer': TABLES['customer'],
                        'public.session': ['id', 'level', 'size', 'comment', 'first_name'], 'other.store': ['store_id', 'name']})
    assert read('SELECT name FROM public.store', 'postgresql', public) == ['public.store.name']
    assert read('SELECT s.name, c.first_name AS who FROM public.store s JOIN public.customer c ON c.store_id = s.store_id',
                'postgresql', public) == ['public.store.name', 'public.customer.first_name']
    assert read('SELECT public.store.name, store.city FROM public.store', 'postgresql', public) == ['public.store.name', 'public.store.city']
    assert read('SELECT * FROM public.customer', 'postgresql', public) == [f'public.customer.{c}' for c in TABLES['customer']]
    assert read('SELECT UPPER(c.first_name) FROM public.customer c', 'postgresql', public) == ['f(public.customer.first_name)']
    assert read('SELECT name FROM public.store UNION ALL SELECT first_name FROM public.customer', 'postgresql', public) == [
        'public.store.name | public.customer.first_name']
    # a table, columns and aliases called like words of Oracle
    assert read('SELECT level, size, comment, first_name FROM public.session', 'postgresql', public) == [
        f'public.session.{c}' for c in ('level', 'size', 'comment', 'first_name')]
    assert read('SELECT session.first_name, s.size FROM session, public.session s', 'postgresql', public) == [
        'public.session.first_name', 'public.session.size']
    assert read('SELECT size s, level l, first_name comment FROM session', 'postgresql', public) == [
        'public.session.size', 'public.session.level', 'public.session.first_name']
    # which schema is meant still counts: two tables called store, and a schema the source does not have
    assert read('SELECT name FROM store', 'postgresql', public) == ['?']
    assert read('SELECT name FROM other.store', 'postgresql', public) == ['other.store.name']
    assert read('SELECT first_name FROM telecom.customer', 'postgresql', public) == ['?']


@pytest.mark.parametrize('dialect', ['postgresql', 'mssql', 'mysql', 'sqlite', 'duckdb'])
def test_each_engine_reads_a_from_clause_with_its_own_reserved_words(dialect):
    p = PREFIX[dialect]
    for word in ORACLE_ONLY:
        if (dialect, word) == ('mssql', 'public'):
            continue                                    # SQL Server reserves it as well: tested above
        # an alias of a table, bare and behind AS; of a subquery; of a WITH query; of a column
        assert read(f'SELECT {word}.name, {word}.city FROM store {word}', dialect) == [f'{p}store.name', f'{p}store.city'], word
        assert read(f'SELECT s.name, {word}.first_name FROM store s JOIN employee AS {word} ON {word}.store_id = s.store_id',
                    dialect) == [f'{p}store.name', f'{p}employee.first_name'], word
        assert read(f'SELECT {word}.who FROM (SELECT first_name AS who FROM customer) {word}', dialect) == [f'{p}customer.first_name'], word
        assert read(f'WITH {word} AS (SELECT first_name AS who FROM customer) SELECT who FROM {word}', dialect) == [
            f'{p}customer.first_name'], word
        assert read(f'SELECT q.{word} FROM (SELECT first_name AS {word}, name AS thing FROM customer, store) q', dialect) == [
            f'{p}customer.first_name'], word
        # a table of that name which the source does not have: not known, and nothing else is disturbed
        assert read(f'SELECT s.name, t.x FROM store s, {word} t', dialect) == [f'{p}store.name', '?'], word
        assert read(f'SELECT s.name FROM {word}.store s', dialect) == ['?'], word
    # the same statements on Oracle, where these words are not names: nothing is followed
    for word in ORACLE_ONLY:
        found = read(f'SELECT {word}.name, {word}.city FROM store {word}', 'oracle')
        assert found is None or set(found) == {'?'}, word


def test_a_table_and_a_schema_called_like_a_word_of_oracle():
    for dialect in ('mysql', 'sqlite', 'duckdb', 'mssql'):
        own = 'dbo.' if dialect == 'mssql' else ''
        model = structure({f'{own}session': ['id', 'first_name', 'level'], f'{own}resource': ['id', 'name'], 'size.store': ['name', 'city']})
        assert read('SELECT first_name, level FROM session', dialect, model) == [f'{own}session.first_name', f'{own}session.level']
        assert read('SELECT r.name, s.first_name FROM resource r JOIN session s ON s.id = r.id', dialect, model) == [
            f'{own}resource.name', f'{own}session.first_name']
        assert read('SELECT name FROM size.store', dialect, model) == ['size.store.name']
        assert read('SELECT * FROM session', dialect, model) == [f'{own}session.{c}' for c in ('id', 'first_name', 'level')]


def test_lateral_and_only_are_names_where_the_engine_has_no_such_word():
    """FROM only store: on PostgreSQL the table store; on SQLite, MySQL and SQL Server a table called "only" under
    the name store. Read the first way there, employee's names would be taken for a shop's."""
    model = structure({'only': ['name', 'first_name'], 'lateral': ['name', 'first_name'], 'store': ['name', 'city']})
    for dialect in ('sqlite', 'mssql', 'mysql'):
        own = structure({f'dbo.{t}': c for t, c in (('only', ['name']), ('lateral', ['name']), ('store', ['name', 'city']))}) \
            if dialect == 'mssql' else model
        p = 'dbo.' if dialect == 'mssql' else ''
        assert read('SELECT store.name FROM only store', dialect, own) == [f'{p}only.name']
        assert read('SELECT name FROM only', dialect, own) == [f'{p}only.name']
        if dialect != 'mysql':                          # MySQL has LATERAL
            assert read('SELECT store.name FROM lateral store', dialect, own) == [f'{p}lateral.name']
        assert read('SELECT store.name FROM only store', dialect) in (['?'], None)      # no such table in this source
    # where the engine has the word, it is the word
    assert read('SELECT name FROM ONLY store', 'postgresql') == ['shop.store.name']
    assert read('SELECT name FROM ONLY store', 'duckdb') == ['store.name']
    assert read('SELECT s.name, e.first_name FROM store s, LATERAL (SELECT first_name FROM employee e2 WHERE e2.store_id = s.store_id) e',
                'mysql') == ['store.name', 'employee.first_name']


def test_a_star_that_is_not_plain_hides_the_columns():
    def lineage(sql, dialect='duckdb'):          # these do not pass the guard today; the reader must not trust them either
        found = _Lineage(tokenize(sql, dialect), dialect, structure()).run()
        return None if found is None else [text(o if o.columns is not None else None) for o in found]
    assert lineage('SELECT * EXCLUDE (first_name) FROM employee') is None
    assert lineage('SELECT e.* EXCLUDE (first_name) FROM employee e') is None
    assert lineage('SELECT SQL_CALC_FOUND_ROWS * FROM employee', 'mysql') is None
    assert lineage('SELECT (e).* FROM employee e', 'postgresql') is None
    assert lineage('SELECT price * 2, 2 * price, COUNT(*), COUNT(p.*) FROM product p') == ['f(product.price)', 'f(product.price)',
                                                                                         'count', 'count']
    assert lineage('SELECT MAX(*) FROM product') == ['?']
    assert lineage('SELECT COLUMNS(*) FROM product') == ['?']


# ---------------------------------------------------- checked against the result

def test_the_names_of_the_result_have_to_match():
    sql = 'SELECT s.name, e.first_name AS who, UPPER(e.email), COUNT(*) FROM store s, employee e'
    assert read(sql, names=['name', 'who', 'UPPER(e.email)', 'COUNT(*)']) == [
        'store.name', 'employee.first_name', 'f(employee.email)', 'count']
    assert read(sql, names=['NAME', 'WHO', '', '']) == ['store.name', 'employee.first_name', 'f(employee.email)', 'count']
    assert read(sql, names=['name', 'who', 'x']) is None                            # another number of columns
    assert read(sql, names=['who', 'name', 'x', 'y']) is None                       # another order
    # a table that changed since the source was read: the * no longer lines up
    assert read('SELECT * FROM store', names=['store_id', 'name', 'city', 'phone']) is not None
    assert read('SELECT * FROM store', names=['store_id', 'owner_name', 'name', 'city', 'phone']) is None
    assert read('SELECT * FROM store', names=['store_id', 'owner_name', 'city', 'phone']) is None


def test_whatever_the_statement_the_answer_is_a_list_or_nothing():
    """origins() never raises: what it cannot read is "not known"."""
    model = structure()
    for dialect in ('oracle', 'mssql', 'postgresql', 'mysql', 'sqlite', 'duckdb'):
        for sql in ('SELECT', 'SELECT FROM', 'SELECT ) FROM store', 'SELECT ( FROM store', 'SELECT name FROM', 'SELECT name FROM (',
                    'WITH', 'WITH x AS SELECT 1', 'SELECT * FROM store UNION', 'SELECT name FROM store JOIN', 'SELECT , FROM store',
                    'SELECT name.. FROM store', 'SELECT s..name FROM store s', 'SELECT TOP FROM store', '', ')', 'SELECT 1 UNION ALL'):
            try:
                tokens = tokenize(sql, dialect)
            except ReadOnlyViolation:
                continue
            found = origins(tokens, dialect, model)
            assert found is None or isinstance(found, list), (dialect, sql)
    assert origins(tokenize('SELECT name FROM store', 'sqlite'), 'sqlite', None) is None        # no structure at all


def test_count_and_nothing_else():
    def only(sql):
        return only_counts(tokenize(sql, 'sqlite'))
    assert only('COUNT(*)') and only('count(DISTINCT a)') and only('COUNT_BIG(a)') and only('COUNT((SELECT 1))')
    assert not only('COUNT(a) + 1') and not only('COUNT(*) OVER (PARTITION BY a)') and not only('MAX(a)') and not only('(COUNT(a))')
