"""Session 3, part C: the guard for SQL typed by a person - no database needed.

The same hostile battery runs through the real runner, as an account that can write, on the four
Docker databases: tests/test_live_sql.py.
"""

import json

import pytest

import hostile
from app import config, sqlguard
from app.security import ReadOnlyViolation, check_read_only_sql
from app.sqlguard import FUNCTIONS, FUNCTIONS_FILE, Known, check, load_functions, tokenize, use_functions
from scripts import live_engines as le

ENGINES = list(le.ENGINES)
TELECOM = {'CUSTOMERS': ['CUSTOMER_ID', 'FULL_NAME', 'CITY', 'CREATED_AT'], 'ACCOUNTS': ['ACCOUNT_ID', 'CUSTOMER_ID', 'BALANCE'],
           'SUBSCRIPTIONS': ['SUBSCRIPTION_ID', 'ACCOUNT_ID', 'STATUS'],
           'USAGE_EVENTS': ['EVENT_ID', 'SUBSCRIPTION_ID', 'EVENT_TIME', 'DURATION_S', 'TAGS']}
# what the test databases can call (scripts/live_engines.py), as the catalog read of the source finds it
CALLABLE = ['X_PING', 'STATUS', 'ONE', 'X_PKG', 'X_TOUCH', 'DBMS_RANDOM', 'DBMS_LOCK', 'UTL_HTTP', 'XMLTYPE']
SCHEMAS = ['DX_EXTRA', 'TELECOM', 'OTHER', 'SYS', 'SYSTEM', 'EXPLORER_RO']


def oracle(callable=CALLABLE):
    """The Oracle test source as the guard sees it: upper-case names, and what the database can call."""
    tables = {f'DX_EXTRA.{t.upper()}': [c.upper() for c in columns] for t, (columns, _) in le.extra_rows().items()}
    tables.update({f'TELECOM.{t}': columns for t, columns in TELECOM.items()})
    return Known(tables, callable, SCHEMAS)


ORACLE = oracle()
POSTGRES = Known({**{f'dx_extra.{t}': columns for t, (columns, _) in le.extra_rows().items()},
                  **{f'telecom.{t.lower()}': [c.lower() for c in columns] for t, columns in TELECOM.items()}})


def names(engine):
    return ORACLE if engine == 'oracle' else None


def source_names(engine):
    """Oracle and PostgreSQL judge names against the source: the battery runs with its structure."""
    return {'oracle': ORACLE, 'postgresql': POSTGRES}.get(engine)


def blocked(sql, dialect, known=None):
    with pytest.raises(ReadOnlyViolation) as refusal:
        check_read_only_sql(sql, dialect, strict=True, names=known if known is not None else names(dialect))
    return str(refusal.value)


def passes(sql, dialect, known=None):
    return check_read_only_sql(sql, dialect, strict=True, names=known if known is not None else names(dialect))


def battery(engine):
    return hostile.battery(engine, le.extra_schema(engine), le.ENGINES[engine]['schema'])


HOSTILE = [(engine, group, sql) for engine in ENGINES for group, sql in battery(engine)]
ALLOWED = [(engine, sql) for engine in ENGINES for sql in hostile.allowed(engine, le.extra_schema(engine))]


# ------------------------------------------------------------------ the battery

@pytest.mark.parametrize('engine, group, sql', HOSTILE, ids=[f'{e}-{g}-{i}' for i, (e, g, _) in enumerate(HOSTILE)])
def test_the_hostile_battery_is_refused(engine, group, sql):
    assert blocked(sql, engine, source_names(engine))


@pytest.mark.parametrize('engine, sql', ALLOWED, ids=[f'{e}-{i}' for i, (e, _) in enumerate(ALLOWED)])
def test_reads_that_hold_the_same_words_pass(engine, sql):
    checked = passes(sql, engine, source_names(engine))
    assert checked.sql and not any(t.kind == 'comment' for t in tokenize(checked.sql, engine))      # comments are never sent


def test_the_battery_covers_what_the_brief_asked_for():
    for engine in ENGINES:
        groups = {group for group, _ in hostile.battery(engine, 'x')}
        expected = set(hostile.GROUPS) - ({'dblink', 'sequence'} if engine == 'mysql' else set())
        expected -= set() if engine in hostile.NO_BRACKET_CALLS else {'call without brackets'}
        assert groups == expected, (engine, expected - groups)
        assert len(hostile.battery(engine, 'x')) >= 60
    assert len(HOSTILE) >= 310 and len(ALLOWED) >= 30
    assert len(hostile.ORACLE_CALLS) >= 30 and len(hostile.POSTGRES_CALLS) >= 12


@pytest.mark.parametrize('engine', ENGINES + ['sqlite', 'duckdb', None])
def test_only_select_and_with_start_a_statement(engine):
    for first in ('SHOW TABLES', 'DESCRIBE t', 'PRAGMA table_info(t)', 'SET x = 1', 'EXPLAIN SELECT 1', 'CALL p()',
                  'VALUES (1)', 'TABLE t', 'USE x', 'BEGIN', 'ANALYZE t', '(((DELETE FROM t)))', 'sElEcT 1; DELETE FROM t'):
        assert blocked(first, engine)
    assert passes('  ((SELECT 1 AS a))  ', engine).sql == '((SELECT 1 AS a))'
    assert passes('with a as (select 1 as x) select x from a;;', engine).sql == 'with a as (select 1 as x) select x from a'
    assert blocked('', engine) == 'Empty statement.' and blocked(' ; ', engine) == 'Empty statement.'
    assert blocked('SELECT 1\x00', engine) == 'Statement contains a NUL character.'
    assert 'longer than 20,000 characters' in blocked('SELECT ' + '1,' * 12_000 + '1', engine)


# ----------------------------------------------------------- the known bypasses

def test_oracle_q_quotes_are_read_as_oracle_reads_them():
    """q'[ ... ]' ends at ]' - not at the first quote. Session 2's tokenizer ended it at the quote, so the
    text after it was taken for a string and never looked at."""
    kinds = [(t.kind, t.text) for t in tokenize("SELECT q'[it's]' || x, nq'{a'b}', Q'!c'd!', q'<e'f>', q'(g'h)' FROM dual", 'oracle')]
    strings = [text for kind, text in kinds if kind == 'string']
    assert strings == ["q'[it's]'", "nq'{a'b}'", "Q'!c'd!'", "q'<e'f>'", "q'(g'h)'"]
    assert ('ident', 'x') in kinds and ('ident', 'dual') in kinds
    # the function call sits between two q-quoted strings whose insides hold a quote
    hidden = "SELECT q'[ ' ]' || x_touch() || q'[ ' ]' FROM dual"
    assert 'x_touch() is not on the list' in blocked(hidden, 'oracle')
    assert 'x_touch is the name of something that can be called' in blocked(hidden.replace('()', ''), 'oracle')
    assert 'x_touch is not a column of the tables' in blocked(hidden.replace('()', ''), 'oracle', oracle(callable=[]))
    assert passes("SELECT q'[ ' ; DELETE FROM x; ]' AS note FROM dual", 'oracle').sql.endswith('FROM dual')
    assert blocked("SELECT q'[never closed FROM dual", 'oracle') == "A q'...' string is never closed."
    # a column called q, or a name ending in q, is not the start of such a string
    assert passes("SELECT faq'x' FROM t", 'postgresql') and blocked("SELECT q'[x]' FROM t", None) == "q'...' strings are not allowed."


def test_square_brackets_are_a_quoted_name_only_on_sql_server_and_sqlite():
    sql = "SELECT [select] FROM t"
    for engine in ('mssql', 'sqlite'):
        assert [(t.kind, t.text) for t in tokenize(sql, engine)][1] == ('qident', 'select')
    for engine in ('postgresql', 'oracle', 'mysql', 'duckdb', None):
        assert [(t.kind, t.text) for t in tokenize(sql, engine)][1:4] == [('op', '['), ('ident', 'select'), ('op', ']')]
    # PostgreSQL: an array subscript is code. Session 2 took everything between [ and ] for a name.
    assert 'pg_sleep() is not on the list' in blocked('SELECT (ARRAY[1])[pg_sleep(5)::int]', 'postgresql')
    assert 'NEXTVAL' in blocked("SELECT a[nextval('s')] FROM t", 'postgresql')
    assert 'DELETE is not allowed' in blocked('SELECT a[1] FROM t WHERE b[(SELECT 1)] = 1 OR c[DELETE] = 1', 'postgresql')
    assert passes('SELECT tags[1], (ARRAY[1, 2, 3])[2] FROM t WHERE tags @> ARRAY[1]', 'postgresql')
    assert passes('SELECT [delete], [a b] FROM [my table]', 'mssql')       # there the same brackets quote a name
    assert blocked('SELECT [a FROM t', 'mssql') == 'A [ is never closed.'


def test_backticks_are_a_quoted_name_only_on_mysql_and_sqlite():
    assert passes('SELECT `delete`, `a b` FROM `t`', 'mysql') and passes('SELECT `delete` FROM t', 'sqlite')
    for engine in ('postgresql', 'oracle', 'mssql', 'duckdb', None):
        assert blocked('SELECT `x` FROM t', engine) == 'A backtick (`) is not valid here.'


def test_dblink_and_every_function_with_side_effects_is_refused():
    for engine, sql in [
            ('postgresql', "SELECT dblink('c', 'select 1')"), ('postgresql', "SELECT dblink_exec('c', 'delete from t')"),
            ('postgresql', "SELECT dblink_connect('c')"), ('postgresql', "SELECT dblink_open('c', 'x')"),
            ('postgresql', 'SELECT pg_sleep(1)'), ('postgresql', "SELECT set_config('a', 'b', true)"),
            ('postgresql', "SELECT nextval('s')"), ('postgresql', 'SELECT pg_advisory_lock(1)'), ('postgresql', 'SELECT lo_unlink(1)'),
            ('postgresql', 'SELECT pg_switch_wal()'), ('postgresql', 'SELECT pg_stat_reset()'), ('postgresql', 'SELECT txid_current()'),
            ('mysql', 'SELECT SLEEP(1)'), ('mysql', "SELECT GET_LOCK('a', 1)"), ('mysql', "SELECT LOAD_FILE('x')"),
            ('mysql', 'SELECT BENCHMARK(1, 1)'), ('mysql', 'SELECT MASTER_POS_WAIT(1, 1)'),
            ('mssql', "SELECT * FROM OPENROWSET('a', 'b', 'c')"), ('mssql', "SELECT * FROM OPENQUERY(a, 'b')"),
            ('oracle', 'SELECT sys_guid_and_write() FROM dual'), ('oracle', "SELECT httpuritype('http://x').getclob() FROM dual"),
            ('sqlite', "SELECT load_extension('x')"), ('sqlite', "SELECT writefile('x', 'y')"), ('sqlite', "SELECT readfile('x')"),
            ('duckdb', "SELECT * FROM read_csv('x')"), ('duckdb', "SELECT * FROM read_parquet('x')"),
            ('duckdb', "SELECT * FROM read_json_auto('x')"), ('duckdb', "SELECT * FROM read_text('x')"),
            ('duckdb', "SELECT * FROM glob('*')"), ('duckdb', "SELECT getenv('HOME')"), ('duckdb', "SELECT * FROM query('select 1')")]:
        message = blocked(sql, engine)
        assert 'is not on the list of functions' in message or 'is not allowed' in message, (sql, message)


def test_duckdb_file_and_extension_statements_are_refused():
    for sql in ("COPY t TO 'x.csv'", "COPY (SELECT 1) TO 'x'", "ATTACH 'x.db' AS x", "INSTALL httpfs", "LOAD httpfs",
                "EXPORT DATABASE 'x'", "IMPORT DATABASE 'x'", "PRAGMA database_list", "SET enable_external_access = true",
                "SELECT 1; LOAD httpfs", "SELECT * FROM 'x.csv'", "SELECT * FROM t JOIN 'C:/data/y.parquet' USING (id)",
                "SELECT 1 AS copy FROM t WHERE install = 1"):
        assert blocked(sql, 'duckdb'), sql
    assert blocked("SELECT * FROM 'x.csv'", 'duckdb') == ('A file cannot be read as a table: only the tables of this source '
                                                           'can be queried.')
    assert passes("SELECT 'x.csv' AS file_name, COUNT(*) FROM customers GROUP BY ALL", 'duckdb')


@pytest.mark.parametrize('hint', ['UPDLOCK', 'XLOCK', 'HOLDLOCK', 'TABLOCK', 'TABLOCKX', 'ROWLOCK', 'PAGLOCK', 'SERIALIZABLE',
                                  'REPEATABLEREAD', 'READCOMMITTEDLOCK'])
def test_sql_server_lock_hints_are_refused(hint):
    for sql in (f'SELECT * FROM t WITH ({hint})', f'SELECT * FROM t WITH (NOLOCK, {hint.lower()})', f'SELECT * FROM t a {hint}',
                f'SELECT * FROM t AS a WITH (INDEX(1), {hint}) JOIN u ON u.id = a.id'):
        assert blocked(sql, 'mssql') == f'{hint} is not allowed: lock hints make a read hold locks.'
    assert blocked(f'SELECT * FROM t ({hint})', 'mssql')                 # the old spelling without WITH
    assert passes(f"SELECT '{hint}' AS word, [{hint}] FROM t WITH (NOLOCK)", 'mssql')    # as a string or a quoted name: fine


def test_locking_reads_are_refused_everywhere():
    for engine in ENGINES + ['sqlite', 'duckdb', None]:
        for tail in ('FOR UPDATE', 'FOR SHARE', 'FOR NO KEY UPDATE', 'FOR KEY SHARE', 'FOR UPDATE NOWAIT', 'for update skip locked'):
            assert blocked(f'SELECT * FROM t {tail}', engine) == 'Locking reads (FOR UPDATE / FOR SHARE) are not allowed.'
        assert blocked('SELECT * FROM t LOCK IN SHARE MODE', engine) == 'Locking reads (LOCK IN SHARE MODE) are not allowed.'
    assert passes("SELECT * FROM t WHERE note = 'FOR UPDATE' ORDER BY 1", 'mysql')


def test_sequences_are_refused():
    assert blocked('SELECT s.NEXTVAL FROM dual', 'oracle') == 'NEXTVAL is not allowed: a sequence changes when it is read.'
    assert blocked('SELECT NEXT VALUE FOR s', 'mssql') == 'NEXT VALUE FOR is not allowed: a sequence changes when it is read.'
    assert 'NEXTVAL' in blocked("SELECT nextval('s')", 'postgresql') and 'SETVAL' in blocked("SELECT setval('s', 1)", 'postgresql')


def test_comments_are_removed_and_never_sent():
    checked = passes('SELECT a, -- the key\n b /* and the rest */ FROM t -- done', 'postgresql')
    assert checked.sql == 'SELECT a,  \n b   FROM t' and [t.text for t in checked.tokens] == ['SELECT', 'a', ',', 'b', 'FROM', 't']
    assert passes("SELECT '-- not a comment /* nor this */' AS s FROM t", 'oracle').sql == "SELECT '-- not a comment /* nor this */' AS s FROM t"
    assert blocked('SELECT 1 /* open', 'mssql') == 'A /* comment is never closed.'
    assert blocked('SELECT 1 /* a /* b */ c */', 'postgresql') == 'Comments inside comments are not allowed.'
    for engine in ('mysql', None):
        assert blocked('SELECT 5 --1', engine) == 'Write "-- " with a space to start a comment.'
        assert blocked('SELECT 1 # x', engine) == '"#" is not allowed.'
    assert passes('SELECT 5 --1', 'postgresql').sql == 'SELECT 5'        # there it IS a comment
    for hidden in ('SELECT /*!50000 1 */ 1', 'SELECT /*+ PARALLEL(8) */ 1 FROM t', 'SELECT /*M! 1 */ 1'):
        assert 'Executable comments and optimizer hints' in blocked(hidden, 'mysql')


def test_strings_that_could_be_read_two_ways_are_refused():
    for engine in ('postgresql', 'mysql', 'duckdb', None):
        assert blocked("SELECT 'a\\'b'", engine) == 'Backslashes are not allowed.'
    for engine in ('oracle', 'mssql', 'sqlite'):                           # no escape character there
        assert passes("SELECT 'C:\\data\\x' AS path FROM t", engine if engine != 'oracle' else 'mssql')
    assert blocked('SELECT $$x$$', 'postgresql') == 'Dollar-quoted strings are not allowed.'
    assert blocked('SELECT $tag$x$tag$', 'duckdb') == 'Dollar-quoted strings are not allowed.'
    assert blocked("SELECT 'never closed FROM t", 'mssql') == "A ' is never closed."
    assert blocked('SELECT "never closed FROM t', 'oracle') == 'A " is never closed.'
    assert blocked("SELECT E'x'", 'postgresql') == "E'...' and U&'...' strings (with escapes) are not allowed."
    assert passes("SELECT 'it''s', N'x', \"a \"\"b\"\" c\" FROM t", 'mssql')
    assert blocked('SELECT * FROM t@remote', 'oracle') == '"@" (a database link) is not allowed.'
    assert passes("SELECT 'a@b.com' AS mail FROM dual", 'oracle') and passes('SELECT @@VERSION', 'mssql')
    assert blocked('SELECT @a := 1', 'mysql') == 'Assignments (:=) are not allowed.'


# ------------------------------------------------------------------- functions

def test_only_listed_functions_may_be_called():
    assert passes("SELECT UPPER(city), COUNT(*), COALESCE(MAX(created_at), DATE '2020-01-01') FROM customers GROUP BY UPPER(city)", 'oracle')
    message = blocked('SELECT my_function(city) FROM customers', 'postgresql')
    assert message.startswith('my_function() is not on the list of functions that may be used here.')
    assert 'your own functions are not' in message and 'app/analysis/sql_functions.json' in message
    # a schema or a package in front of it: refused whatever the name is
    for engine, sql, name in [('oracle', 'SELECT dbms_lock.sleep(1) FROM dual', 'dbms_lock.sleep'),
                              ('oracle', 'SELECT telecom.pkg.fn(1) FROM dual', 'telecom.pkg.fn'),
                              ('mssql', 'SELECT dbo.total(1)', 'dbo.total'), ('postgresql', 'SELECT public.upper(city) FROM t', 'public.upper'),
                              ('postgresql', 'SELECT pg_catalog.lower(city) FROM t', 'pg_catalog.lower'),
                              ('mysql', 'SELECT shop.f()', 'shop.f')]:
        assert blocked(sql, engine) == (f'{name}() is not allowed: a function with a schema or package in front of it '
                                        f'cannot be called here.')
    assert blocked('SELECT "upper"(city) FROM t', 'postgresql') == '"upper"() - a function with a quoted name - is not allowed.'


def test_what_looks_like_a_call_but_is_not():
    assert passes('SELECT x FROM t WHERE x IN (1, 2) AND NOT (y = 1) AND EXISTS (SELECT 1 FROM u)', 'mysql')
    assert passes('SELECT CAST(x AS NUMERIC(10, 2)), x::varchar(10) FROM t', 'postgresql')       # types, not functions
    assert passes('SELECT CONVERT(VARCHAR(10), y), CAST(x AS DECIMAL(10, 2)) FROM t', 'mssql')
    assert passes('SELECT * FROM (SELECT 1 AS a, 2 AS b) v(a, b)', 'postgresql')            # an alias with its columns
    assert passes('SELECT * FROM t AS v(a, b)', 'postgresql')
    assert passes('WITH c (a, b) AS (SELECT 1, 2), d(x) AS (SELECT a FROM c) SELECT * FROM d', 'mssql')
    assert passes('SELECT SUM(x) OVER (PARTITION BY y ORDER BY z ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM t', 'oracle',
                  {'tables': {'T': ['X', 'Y', 'Z']}, 'callable': []})
    assert passes('SELECT a.id FROM a, b WHERE a.id = b.id (+)', 'oracle', {'tables': {'A': ['ID'], 'B': ['ID']}, 'callable': []})
    assert passes('SELECT y, COUNT(*) FROM t GROUP BY GROUPING SETS ((y), ()), ROLLUP (y)', 'postgresql')
    assert passes('SELECT TRUNCATE(x, 2) FROM t', 'mysql') and 'TRUNCATE is not allowed' in blocked('SELECT TRUNCATE(x, 2) FROM t', 'mssql')
    # ... and what looks like one of those but is a call
    assert 'evil() is not on the list' in blocked('WITH c AS (SELECT 1) SELECT evil(1) AS (x)', 'postgresql')
    assert 'evil() is not on the list' in blocked('SELECT * FROM evil(1) AS (a int)', 'postgresql')
    assert 'evil() is not on the list' in blocked('SELECT 1, evil(2)', 'mssql')
    assert 'c() is not on the list' in blocked('SELECT c (1) FROM t', 'mysql')


def test_the_function_list_is_a_file_that_can_be_extended(tmp_path, monkeypatch):
    data = json.loads(FUNCTIONS_FILE.read_text(encoding='utf-8'))
    assert FUNCTIONS_FILE.name == 'sql_functions.json' and set(data) == {
        'about', 'all', 'oracle', 'mssql', 'postgresql', 'mysql', 'sqlite', 'duckdb', 'types'}
    for engine in sqlguard.DIALECTS:
        assert {'COUNT', 'UPPER', 'COALESCE', 'ROW_NUMBER'} <= FUNCTIONS[engine]
    assert 'NVL' in FUNCTIONS['oracle'] and 'NVL' not in FUNCTIONS['postgresql'] and 'ISNULL' in FUNCTIONS['mssql']
    # nothing with a side effect is on any list
    listed = set().union(*[FUNCTIONS[e] for e in sqlguard.DIALECTS])
    for bad in ('SLEEP', 'PG_SLEEP', 'BENCHMARK', 'GET_LOCK', 'RELEASE_LOCK', 'LOAD_FILE', 'LOAD_EXTENSION', 'NEXTVAL', 'SETVAL',
                'SET_CONFIG', 'DBLINK', 'DBLINK_EXEC', 'LO_IMPORT', 'LO_EXPORT', 'PG_READ_FILE', 'PG_LS_DIR', 'PG_ADVISORY_LOCK',
                'PG_TERMINATE_BACKEND', 'PG_CANCEL_BACKEND', 'OPENROWSET', 'OPENQUERY', 'OPENDATASOURCE', 'READ_CSV',
                'READ_CSV_AUTO', 'READ_PARQUET', 'READ_JSON', 'READ_TEXT', 'READ_BLOB', 'GETENV', 'QUERY', 'QUERY_TABLE',
                'WRITEFILE', 'READFILE', 'TXID_CURRENT', 'PG_NOTIFY', 'QUERY_TO_XML', 'CURRENT_SETTING', 'LAST_INSERT_ID',
                'XP_CMDSHELL', 'SP_EXECUTESQL', 'UTL_HTTP', 'DBMS_LOCK', 'HTTPURITYPE', 'XMLTYPE'):
        assert bad not in listed, bad
    assert 'GLOB' in FUNCTIONS['sqlite'] and 'GLOB' not in FUNCTIONS['duckdb']       # a string test there, the file system here
    assert not {f for f in FUNCTIONS['duckdb'] if f.startswith('READ_')}

    try:
        assert 'margin() is not on the list' in blocked('SELECT margin(a, b) FROM t', 'oracle', {})
        use_functions(extra='margin, oracle:net_price')                  # DATA_EXPLORER_SQL_FUNCTIONS
        assert passes('SELECT margin(a, b) FROM t', 'postgresql') and passes('SELECT net_price(t.a) FROM t', 'oracle', None)
        assert 'net_price() is not on the list' in blocked('SELECT net_price(a) FROM t', 'mysql')
        assert blocked('SELECT billing.margin(a, b) FROM t', 'postgresql')          # still not with a schema in front
        data['postgresql'].append('my_reviewed_function')                # ... or a copy of the file
        copy = tmp_path / 'functions.json'
        copy.write_text(json.dumps(data), encoding='utf-8')
        use_functions(copy, extra='')
        assert passes('SELECT my_reviewed_function(1)', 'postgresql') and blocked('SELECT margin(1)', 'postgresql')
        with pytest.raises(ValueError, match='is not a function name'):
            load_functions(extra='shop.fn')
        with pytest.raises(ValueError, match='is not a function name'):
            load_functions(extra='db2:f')
    finally:
        use_functions()
    assert 'DATA_EXPLORER_SQL_FUNCTIONS' in (FUNCTIONS_FILE.parent.parent / 'config.py').read_text(encoding='utf-8')
    assert config.SQL_EXTRA_FUNCTIONS == ''


# --------------------------------------------------- Oracle: calls without brackets

KNOWN = Known({'TELECOM.CUSTOMERS': ['CUSTOMER_ID', 'FULL_NAME', 'CITY', 'CREATED_AT', 'STATUS', 'Mixed Case'],
               'TELECOM.ACCOUNTS': ['ACCOUNT_ID', 'CUSTOMER_ID', 'BALANCE']},
              callable=['AUDIT_AND_RETURN', 'MY_PKG', 'STATUS', 'DBMS_RANDOM'], schemas=['TELECOM', 'HR'])
# The list of what can be called is as old as the last refresh of the source: a function made since
# then is not on it. What is refused must be refused without the list too, by where the name stands.
NOT_LISTED = Known(KNOWN.tables, callable=[], schemas=['TELECOM'])


@pytest.mark.parametrize('sql', [
    'SELECT audit_and_return FROM dual',                              # a function that takes no argument
    'SELECT city, audit_and_return FROM telecom.customers',
    'SELECT city FROM telecom.customers WHERE audit_and_return = 1',
    'SELECT city FROM telecom.customers ORDER BY audit_and_return',
    'SELECT COUNT(*) FROM telecom.customers GROUP BY city HAVING audit_and_return > 0',
    'SELECT COALESCE(audit_and_return, 0) FROM dual',
    'SELECT EXTRACT(YEAR FROM audit_and_return) FROM dual',           # FROM inside a function names no table
    'SELECT TRIM(BOTH 0 FROM audit_and_return) FROM dual',
    'SELECT CASE WHEN audit_and_return = 1 THEN 1 END FROM dual',
    'SELECT my_pkg.audit_and_return FROM dual',                       # a package, or a schema, in front of it
    'SELECT telecom.audit_and_return FROM dual',
    'SELECT telecom.my_pkg.audit_and_return FROM dual',
    'SELECT c.city, (SELECT audit_and_return FROM dual) FROM telecom.customers c',
    'SELECT city FROM telecom.customers WHERE customer_id IN (SELECT audit_and_return FROM dual)',
    'SELECT "AUDIT_AND_RETURN" FROM dual',                            # a quoted name is a call just the same
    'SELECT "MY_PKG"."AUDIT_AND_RETURN" FROM dual',
    'SELECT city FROM telecom.customers CONNECT BY LEVEL <= audit_and_return',
    'SELECT city FROM telecom.customers c JOIN telecom.accounts a ON a.customer_id = audit_and_return',
    'SELECT * FROM TABLE(audit_and_return)',
    'SELECT city FROM telecom.customers FETCH FIRST audit_and_return ROWS ONLY',
])
def test_oracle_refuses_names_that_could_be_a_function_call(sql):
    for known in (KNOWN, NOT_LISTED):
        assert 'function' in blocked(sql, 'oracle', known), sql
    passes(sql.replace('CONNECT BY LEVEL <=', 'WHERE 1 <=').replace('TABLE(audit_and_return)', 'audit_and_return')
           .replace('FETCH FIRST audit_and_return ROWS ONLY', 'LIMIT audit_and_return'), 'postgresql')       # elsewhere a call needs its brackets


SCOPE_ALONE = [(what, sql) for what, sql in hostile.calls('oracle', 'DX_EXTRA', 'TELECOM')
               if what.split()[0] not in ('6', '7', '7b')]            # Oracle itself refuses those: the alias wins (ORA-00904)


@pytest.mark.parametrize('what, sql', SCOPE_ALONE, ids=[what.split()[0] for what, _ in SCOPE_ALONE])
def test_oracle_refuses_every_call_without_brackets_by_its_scope_alone(what, sql):
    """scripts/call_proof.py shows Oracle running these. With an empty list of what can be called -
    x_ping made after the last refresh - each is still refused, because no table in that query block
    has such a column."""
    assert 'function' in blocked(sql, 'oracle', oracle(callable=[]))


def test_the_eight_shapes_of_the_audit_and_what_is_said_about_each():
    shapes = hostile.calls('oracle', 'DX_EXTRA', 'TELECOM')
    listed = {what.split()[0]: blocked(sql, 'oracle', ORACLE) for what, sql in shapes}
    alone = {what.split()[0]: blocked(sql, 'oracle', oracle(callable=[])) for what, sql in SCOPE_ALONE}
    # an alias with the name of something callable, or of a schema: "rename the alias"
    assert listed['1'].startswith('x_ping is used as a table alias, but it is also the name of something that can be called')
    assert listed['3'].startswith('x_ping is used as a column alias') and listed['5'].startswith('x_ping is used as a name of a WITH query')
    assert listed['6'].startswith('DX_EXTRA is used as a table alias, but it is also the name of a schema')
    for shape in ('1', '2', '3', '5', '6', '7', '7b', '12', '13', '21', '22', '23'):
        assert 'rename the alias' in listed[shape], shape
    # a name that is callable is refused even where a column has that name: "write it with its table alias"
    assert listed['8'] == ('status is the name of something that can be called (a function, procedure, package, type or a synonym '
                           'for one) in this database. On Oracle a function is called without brackets, so the guard cannot let '
                           'that name stand alone, even where a column is called the same. Write the column with its table '
                           'alias: t.status.')
    assert listed['4'].startswith('x_ping is the name of something that can be called')
    # the same statements without the list: refused by the query block the name stands in
    for shape in ('1', '2', '3', '4', '5', '8', '13', '16', '17', '18', '19', '20', '21', '22', '24', '25', '26', '27', '31', '32'):
        assert 'is not a column of the tables in this part of the query, so it could be a function call' in alone[shape], shape
    assert 'A table alias or the name of a WITH query can only stand in front of a dot (t.column), and a column alias can only ' \
           'be used in the ORDER BY of its own SELECT.' in alone['1']
    for shape in ('10', '11', '30'):
        assert 'is not a table or an alias of this part of the query' in alone[shape], shape
    assert 'x_ping is not a column of the tables' in alone['28']       # the ORDER BY of a UNION: names of its first branch only


@pytest.mark.parametrize('sql', [
    "SELECT city, COUNT(*) AS n FROM telecom.customers WHERE created_at >= DATE '2020-01-01' GROUP BY city ORDER BY n DESC",
    'SELECT c.full_name, a.balance total FROM telecom.customers c JOIN telecom.accounts a ON a.customer_id = c.customer_id',
    'SELECT customers.city, telecom.customers.full_name FROM telecom.customers',
    'SELECT x.town, x.people FROM (SELECT city town, COUNT(*) people FROM customers GROUP BY city) x WHERE x.people > 3 ORDER BY town',
    'WITH big (town, people) AS (SELECT city, COUNT(*) FROM customers GROUP BY city) SELECT town, people FROM big',
    'SELECT t.owner, t.table_name FROM all_tables t WHERE t.owner = USER',          # a table outside the model: by alias
    'SELECT ROWNUM, SYSDATE, LEVEL FROM dual CONNECT BY LEVEL <= 3',
    'SELECT city, ROW_NUMBER() OVER (PARTITION BY city ORDER BY created_at DESC NULLS LAST) rn FROM customers',
    "SELECT CASE WHEN balance > 0 THEN 'credit' ELSE 'debit' END kind, EXTRACT(YEAR FROM created_at) yr FROM accounts, customers",
    'SELECT "Mixed Case", "CITY" FROM customers',                                   # quoted: judged like any other name
    'SELECT * FROM customers SAMPLE (10) WHERE city IS NOT NULL FETCH FIRST 5 ROWS ONLY',
    # what the brief of the fix round names: these must still run
    'SELECT c.city AS town, COUNT(*) AS people FROM telecom.customers c GROUP BY c.city ORDER BY people DESC, town',
    'SELECT c.city town FROM telecom.customers c ORDER BY UPPER(town), LENGTH(town) + 1',
    'SELECT c.customer_id, c.city FROM telecom.customers c WHERE c.city IS NOT NULL',                        # alias.column
    'SELECT COUNT(*) FROM telecom.customers, telecom.accounts',                                              # schema.table in FROM
    'WITH recent AS (SELECT customer_id, city FROM telecom.customers) SELECT recent.city, r.customer_id FROM recent, recent r '
    'WHERE recent.customer_id = r.customer_id',                                                              # a WITH query in FROM and as a qualifier
    'SELECT v.town FROM (SELECT city AS town FROM telecom.customers) v WHERE v.town IS NOT NULL',            # subquery alias.column
    'SELECT LEVEL, ROWNUM, SYSDATE, SYSTIMESTAMP, CURRENT_DATE FROM dual CONNECT BY LEVEL <= 3',
    'SELECT dummy FROM dual',
    # further
    'SELECT c.city FROM telecom.customers c WHERE EXISTS (SELECT 1 FROM telecom.accounts a WHERE a.customer_id = c.customer_id '
    'AND balance > 0 AND city IS NOT NULL)',                                                                 # city: of the block around it
    "SELECT full_name FROM customers WHERE city LIKE 'R!%' ESCAPE '!' ORDER BY created_at NULLS FIRST OFFSET 5 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT TRIM(BOTH ' ' FROM city), CAST(balance AS NUMBER(10, 2)), created_at + INTERVAL '1' DAY FROM customers, accounts",
    'SELECT city, SUM(balance) OVER (ORDER BY created_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) FROM customers, accounts',
    'SELECT c.status FROM telecom.customers c WHERE c.status IS NOT NULL',          # STATUS is a function too: with its alias it is the column
    'SELECT city FROM customers UNION ALL SELECT full_name FROM customers ORDER BY city',
    'SELECT city town FROM customers UNION SELECT full_name FROM customers ORDER BY town',
    'SELECT city, COUNT(*) FROM customers GROUP BY ROLLUP (city)',
    'SELECT c.city, l.balance FROM telecom.customers c, LATERAL (SELECT balance FROM telecom.accounts a WHERE a.customer_id = '
    'c.customer_id) l',
    'SELECT c.city FROM (telecom.customers c JOIN telecom.accounts a ON a.customer_id = c.customer_id) WHERE balance > 0',
    'SELECT c.city FROM telecom.customers PARTITION (p2024) c WHERE created_at < SYSDATE - 30',
    'SELECT customer_id FROM telecom.customers JOIN telecom.accounts USING (customer_id)',
    'SELECT city FROM customers ORDER SIBLINGS BY city',
    "SELECT city FROM customers WHERE created_at > TIMESTAMP '2020-01-01 00:00:00' AT TIME ZONE 'UTC'",
])
def test_oracle_accepts_ordinary_queries(sql):
    assert passes(sql, 'oracle', KNOWN)
    assert passes(sql, 'oracle', NOT_LISTED)


def test_oracle_says_how_to_write_it_instead():
    def refused(sql, known=KNOWN):
        return blocked(sql, 'oracle', known)

    # a column called like something callable: with its table alias, and not alone
    assert 'Write the column with its table alias: t.status.' in refused('SELECT status FROM telecom.customers')
    assert passes('SELECT status FROM telecom.customers', 'oracle', NOT_LISTED)        # nothing of that name: the column
    # an alias called like something callable, or like a schema
    for sql in ('SELECT c.city status FROM telecom.customers c', 'SELECT c.city AS "MY_PKG" FROM telecom.customers c',
                'SELECT hr.city FROM telecom.customers hr', 'SELECT dbms_random.city FROM telecom.customers dbms_random',
                'WITH my_pkg AS (SELECT 1 a FROM dual) SELECT my_pkg.a FROM my_pkg',
                'WITH w (status) AS (SELECT 1 FROM dual) SELECT w.status FROM w'):
        assert 'rename the alias' in refused(sql), sql
    # a table that is called like a schema, without an alias in front of the dot
    known = Known({'APP.HR': ['ID']}, callable=[], schemas=['APP', 'HR'])
    assert 'Give the table an alias (FROM hr t) and write t.id.' in refused('SELECT hr.id FROM app.hr', known)
    assert passes('SELECT h.id FROM app.hr h', 'oracle', known) and passes('SELECT app.hr.id FROM app.hr', 'oracle', known)
    assert 'app is not a table or an alias' in refused('SELECT app.hr.id FROM app.hr h', known)      # the alias hides the name
    # a column alias: in the ORDER BY of its own block, nowhere else
    assert passes('SELECT city town FROM customers ORDER BY town DESC', 'oracle', KNOWN)
    for sql in ('SELECT city town FROM customers WHERE town IS NOT NULL', 'SELECT city town FROM customers GROUP BY town',
                'SELECT COUNT(*) n FROM customers HAVING n > 1', 'SELECT city town, town FROM customers',
                'SELECT city town, ROW_NUMBER() OVER (ORDER BY town) rn FROM customers',
                'SELECT city town FROM customers ORDER BY (SELECT town FROM dual)',
                'SELECT city FROM (SELECT city town FROM customers) ORDER BY 1',
                'SELECT MAX(balance) KEEP (DENSE_RANK FIRST ORDER BY top) top FROM accounts'):
        assert 'a column alias can only be used in the ORDER BY of its own SELECT' in refused(sql), sql
    # a known column, but of no table in that query block
    assert 'balance is not a column of the tables in this part of the query' in refused('SELECT balance FROM customers')
    assert 'balance is not a column of the tables' in refused('SELECT city FROM customers WHERE balance > 0')
    # a table alias or a WITH query as a bare name
    assert 'can only stand in front of a dot (t.column)' in refused('SELECT c FROM telecom.customers c')
    assert 'can only stand in front of a dot' in refused('WITH w AS (SELECT city FROM customers) SELECT w FROM w')
    # an alias of another block, or of another branch
    assert 'c is not a table or an alias of this part of the query' in refused(
        'SELECT a.balance FROM telecom.accounts a UNION ALL SELECT c.city FROM telecom.accounts a2')
    assert 'not in a subquery in FROM' in refused('SELECT v.n FROM telecom.customers c, (SELECT c.city n FROM dual) v')
    assert 'city is not a column of the tables' in refused('SELECT v.n FROM telecom.customers c, (SELECT city n FROM dual) v')
    # a table outside the loaded schemas: its columns by alias
    assert 'write it with its table alias (t.owner)' in refused('SELECT owner FROM all_tables')
    assert passes('SELECT t.owner FROM all_tables t', 'oracle', KNOWN)
    # quoted names are names: the exact spelling of a column of the table, or refused
    assert 'is not a column of the tables' in refused('SELECT "Mixed Case" FROM accounts')
    assert 'is not a column of the tables' in refused('SELECT "city" FROM customers')
    # what the guard does not follow, it says
    assert refused('SELECT * FROM customers PIVOT (COUNT(*) FOR city IN (1))').startswith('PIVOT in a FROM clause is not something')
    assert 'could not follow this FROM clause near "AS"' in refused('SELECT city FROM customers AS OF TIMESTAMP SYSDATE - 1')


@pytest.mark.parametrize('word', ['FIRST', 'LAST', 'YEAR', 'DAY', 'VALUE', 'CASE', 'WHEN', 'END', 'ONE', 'A', 'KEYS', 'TIMESTAMP',
                                  'INTERVAL', 'NEXT', 'ONLY', 'TIES', 'NULLS', 'UNBOUNDED', 'PARTITION', 'RANGE', 'BOTH', 'TRUE',
                                  'DENSE_RANK', 'NOCYCLE', 'SIBLINGS', 'ZONE', 'TIME', 'AT', 'PRECEDING', 'ESCAPE', 'OFFSET', 'PATH'])
def test_a_word_of_the_language_is_a_name_where_the_syntax_does_not_want_the_word(word):
    """Oracle only reserves about a hundred words. A function can be called FIRST, YEAR, VALUE or CASE,
    and SELECT first FROM dual calls it (tried on Oracle 23 for every word of the guard's list)."""
    for sql in (f'SELECT {word} FROM dual', f'SELECT city FROM customers WHERE customer_id = {word}',
                f'SELECT NVL({word}, 0) FROM dual', f'SELECT city FROM customers ORDER BY {word}',
                f'SELECT city, {word} FROM customers', f'SELECT (SELECT {word} FROM dual) FROM customers'):
        assert 'function' in blocked(sql, 'oracle', NOT_LISTED), sql
    # as the column of a table in scope it is a column like any other
    known = Known({'T': ['ID', word]}, callable=[])
    assert passes(f'SELECT {word}, id FROM t WHERE {word} IS NOT NULL ORDER BY {word}', 'oracle', known)
    assert 'the name of something that can be called' in blocked(f'SELECT {word} FROM t', 'oracle', Known({'T': [word]}, callable=[word]))


def test_an_alias_behind_a_word_needs_as():
    known = Known({'T': ['VALUE', 'ID']}, callable=[])
    assert 'If it is meant as an alias, write AS in front of it.' in blocked('SELECT value v FROM t', 'oracle', known)
    assert passes('SELECT value AS v, id i FROM t ORDER BY v, i', 'oracle', known)


def test_oracle_without_the_list_of_what_can_be_called_nothing_passes():
    unread = Known(KNOWN.tables, callable=None, schemas=['TELECOM'])
    for sql in ('SELECT city FROM telecom.customers', 'SELECT 1 FROM dual', 'SELECT c.city FROM telecom.customers c'):
        assert blocked(sql, 'oracle', unread) == ('The names of what this Oracle database can call (functions, packages, types, '
                                                  'schemas) have not been read, so no name can be checked. Refresh the source.')
    assert passes('SELECT city FROM telecom.customers', 'postgresql', unread)           # only Oracle needs the list
    # the guard alone, without a source: there is nothing to judge a name against
    assert check_read_only_sql('SELECT audit_and_return FROM dual', 'oracle', strict=True, names=None)


def test_what_a_source_knows():
    known = Known({'A.T': ['X', 'Y'], 'B.T': ['Y', 'Z'], ('C', 'U'): ['K'], 'PLAIN': ['P']})
    assert known.columns('A', 'T') == {'X', 'Y'} and known.columns('C', 'U') == {'K'} and known.columns(None, 'PLAIN') == {'P'}
    assert known.columns(None, 'T') == {'Y'}            # two tables of that name: only what both have
    assert known.columns('A', 'NOPE') is None and known.columns(None, 'NOPE') is None and known.callable is None
    assert known.schemas == {'A', 'B', 'C'}
    # two tables called T: a bare name must be a column of both, whichever the session means
    listed = Known(known.tables, callable=[])
    assert passes('SELECT y FROM t', 'oracle', listed) and 'x is not a column' in blocked('SELECT x FROM t', 'oracle', listed)
    assert passes('SELECT x FROM a.t', 'oracle', listed)


# ----------------------------------------------- PostgreSQL: t.f is f(t)

PG = Known({'telecom.customers': ['customer_id', 'full_name', 'city', 'Mixed'],
            'telecom.accounts': ['account_id', 'customer_id', 'balance']})


@pytest.mark.parametrize('sql', [
    'SELECT c.audit_and_return FROM telecom.customers c',             # audit_and_return(c): a function of the row
    'SELECT (c).audit_and_return FROM telecom.customers c',
    'SELECT customers.audit_and_return FROM telecom.customers',
    'SELECT telecom.customers.audit_and_return FROM telecom.customers',
    'SELECT (customers).audit_and_return FROM telecom.customers',
    'SELECT (telecom.customers).audit_and_return FROM telecom.customers',
    'SELECT (c.*).audit_and_return FROM telecom.customers c',
    'SELECT c."audit_and_return" FROM telecom.customers c',
    'SELECT c.city FROM telecom.customers c WHERE c.audit_and_return = 1',
    'SELECT c.city FROM telecom.customers c ORDER BY c.audit_and_return',
    'SELECT c.city FROM telecom.customers c JOIN telecom.accounts a ON a.customer_id = c.audit_and_return',
    'SELECT c.balance FROM telecom.customers c, telecom.accounts a',  # a column - of another table of the statement
    'SELECT c.n FROM telecom.customers c, (SELECT 1 AS n) v',         # ... or of a subquery beside it
    'SELECT c."CITY", c.mixed FROM telecom.customers c',              # not the spelling of the column
    'SELECT v.city FROM (SELECT customer_id FROM telecom.customers) v',
    'SELECT v.audit_and_return FROM (SELECT * FROM telecom.customers) v',
    'WITH w AS (SELECT city FROM telecom.customers) SELECT w.full_name FROM w',
    'WITH w (town) AS (SELECT city FROM telecom.customers) SELECT w.city FROM w',
    'SELECT (SELECT c.audit_and_return) FROM telecom.customers c',
    'SELECT t.tablename FROM pg_tables t',                            # a table the source does not know: no columns to check
    'SELECT g.g FROM generate_series(1, 3) AS g',
    'SELECT x.city FROM telecom.customers c',                         # not a table of the query
    'SELECT c.city FROM telecom.customers c2',
    'SELECT v.n FROM telecom.customers c, (SELECT c.city AS n) v',    # not LATERAL: c is not known in there
    'SELECT (ROW(1, 2)).f1',
    'SELECT (c.city).audit_and_return FROM telecom.customers c',
    'SELECT (ARRAY[c])[1].audit_and_return FROM telecom.customers c',
    'SELECT c.city FROM telecom.customers c UNION ALL SELECT c.city FROM telecom.accounts a',
])
def test_postgresql_refuses_what_could_be_a_function_of_the_row(sql):
    message = blocked(sql, 'postgresql', PG)
    assert 'function' in message, message
    assert passes(sql, 'postgresql', None)                              # the guard alone, without a source, cannot judge it


@pytest.mark.parametrize('sql', [
    'SELECT c.city, a.balance FROM telecom.customers c JOIN telecom.accounts a ON a.customer_id = c.customer_id',
    'SELECT customers.city, telecom.customers.full_name FROM telecom.customers',
    'SELECT (c).city, c.*, (c.*).full_name, c."Mixed" FROM telecom.customers c',
    'SELECT v.town, v.n FROM (SELECT city AS town, count(*) AS n FROM telecom.customers GROUP BY city) v WHERE v.n > 1 ORDER BY v.town',
    'WITH w (town) AS (SELECT city FROM telecom.customers) SELECT w.town FROM w',
    'WITH w AS (SELECT c.city, c.customer_id AS id FROM telecom.customers c) SELECT w.city, w.id FROM w',
    'SELECT s.*, s.customer_id FROM (SELECT * FROM telecom.customers) s',
    'SELECT g.n FROM generate_series(1, 3) AS g(n)',
    'SELECT g FROM generate_series(1, 3) AS g',
    'SELECT c.city FROM telecom.customers c WHERE EXISTS (SELECT 1 FROM telecom.accounts a WHERE a.customer_id = c.customer_id)',
    'SELECT c.city::text, c.customer_id::numeric(10, 2) FROM telecom.customers c',
    'SELECT tablename, schemaname FROM pg_tables WHERE schemaname IS DISTINCT FROM tableowner',       # bare names are never calls here
    'SELECT c.city, l.balance FROM telecom.customers c CROSS JOIN LATERAL (SELECT a.balance FROM telecom.accounts a '
    'WHERE a.customer_id = c.customer_id) l',
    'SELECT v.a FROM (VALUES (1), (2)) AS v(a)',
    'SELECT c.city FROM ONLY telecom.customers c TABLESAMPLE SYSTEM (5) REPEATABLE (1) WHERE c.city IS NOT NULL LIMIT 3 OFFSET 1',
    'SELECT DISTINCT ON (c.city) c.city, c.full_name FROM telecom.customers c ORDER BY c.city, c.full_name',
    'SELECT c.city, count(*) FILTER (WHERE c.full_name IS NOT NULL) FROM telecom.customers c GROUP BY c.city',
    'SELECT c.city FROM telecom.customers c UNION ALL SELECT a.balance::text FROM telecom.accounts a ORDER BY 1',
    'SELECT c.city FROM (telecom.customers c JOIN telecom.accounts a USING (customer_id)) WHERE a.balance > 0',
    "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY a.balance) FROM telecom.accounts a",
])
def test_postgresql_accepts_columns_of_its_tables(sql):
    assert passes(sql, 'postgresql', PG)


def test_postgresql_says_how_to_write_it_instead():
    assert blocked('SELECT c.audit_and_return FROM telecom.customers c', 'postgresql', PG) == (
        'c.audit_and_return: audit_and_return is not a column of c as this source knows it. On PostgreSQL t.name calls a '
        'function name(t) when t has no such column, and a function can do anything. (Refresh the source if the table has '
        'changed.)')
    assert 'Write tablename without the table in front of it, or add the schema of that table to the source.' in blocked(
        'SELECT t.tablename FROM pg_tables t', 'postgresql', PG)
    assert 'Only (table).column is accepted.' in blocked('SELECT (ROW(1, 2)).f1', 'postgresql', PG)
    assert 'x is not a table or an alias of this part of the query' in blocked('SELECT x.city FROM telecom.customers c',
                                                                               'postgresql', PG)


# ------------------------------------- each engine's own reserved words (13 Oct 2026)
#
# The reader of a FROM clause took an unquoted word for a table only when it was not one of ORACLE's
# reserved words - on every engine. PostgreSQL's FROM public.customers was refused, and so was a table
# or an alias called session, comment, level, resource or size. Now each engine has its own list.

ORACLE_ONLY = ['public', 'session', 'comment', 'level', 'resource', 'size']        # the words of the brief
OTHER_ENGINES = ['postgresql', 'mssql', 'mysql', 'sqlite', 'duckdb']
PUBLIC = Known({'public.customers': ['customer_id', 'full_name', 'city'], 'public.session': ['id', 'level', 'size', 'comment'],
                'telecom.customers': ['customer_id', 'full_name', 'city', 'Mixed'],
                'telecom.accounts': ['account_id', 'customer_id', 'balance']})


def test_every_engine_has_its_own_list_and_oracles_is_what_it_was():
    assert set(sqlguard.RESERVED) == set(sqlguard.DIALECTS)
    assert sqlguard.RESERVED['oracle'] is sqlguard.ORACLE_NEVER_A_CALL          # Oracle is judged exactly as before
    assert sqlguard.ORACLE_NEVER_A_CALL == sqlguard.ORACLE_RESERVED | sqlguard.ORACLE_BUILT_IN
    assert len(sqlguard.ORACLE_RESERVED) == 109
    for word in ORACLE_ONLY:
        assert word.upper() in sqlguard.RESERVED['oracle'], word
        for engine in OTHER_ENGINES:
            # ... except that SQL Server reserves PUBLIC too
            assert (word.upper() in sqlguard.RESERVED[engine]) is ((engine, word) == ('mssql', 'public')), (engine, word)
    # USER: reserved where the engine gives the name of the account for it
    assert {e for e in sqlguard.DIALECTS if 'USER' in sqlguard.RESERVED[e]} == {'oracle', 'postgresql', 'mssql'}
    # the words every engine keeps for itself are in every list
    always = {'SELECT', 'FROM', 'WHERE', 'GROUP', 'ORDER', 'HAVING', 'UNION', 'INTERSECT', 'ON', 'AS', 'AND', 'OR', 'NOT', 'NULL', 'IN',
              'IS', 'INTO', 'TABLE', 'CREATE', 'DISTINCT', 'ALL', 'ELSE', 'THEN', 'TO'}
    for engine, words in sqlguard.RESERVED.items():
        assert always <= words, (engine, always - words)
        assert all(w == w.upper() and w.replace('_', '').isalnum() for w in words), engine
    # an engine the guard does not know: reserved on one engine, reserved there
    assert sqlguard.RESERVED_ANYWHERE == frozenset().union(*sqlguard.RESERVED.values())
    reader = sqlguard._Names(tokenize('SELECT 1'), None, Known())
    assert reader.reserved is sqlguard.RESERVED_ANYWHERE and {'PUBLIC', 'USE', 'TOP', 'ILIKE', 'INDEXED', 'QUALIFY'} <= reader.reserved
    assert sqlguard.RESERVED_BY_HAND == {'mysql': {'MASTER_BIND', 'MASTER_SSL_VERIFY_SERVER_CERT'},
                                         'duckdb': {'COLUMNS', 'GENERATED', 'MAP', 'STRUCT', 'TRY_CAST'}}


@pytest.mark.parametrize('engine', ['sqlite', 'duckdb'])
def test_the_lists_of_sqlite_and_duckdb_are_what_these_engines_answer(engine):
    """Asked of the engine that runs inside Python, word by word (scripts/reserved_words.py). What matters
    is one direction: a word the guard takes for a name must be a name to the engine. The other way round
    only costs a refusal - another version of the engine may reserve a word less."""
    from scripts import reserved_words
    words = reserved_words.candidates([engine])
    assert len(words) > 1000 and {'PUBLIC', 'SESSION', 'LEVEL', 'SELECT', 'INDEXED', 'QUALIFY'} <= set(words)
    answers = reserved_words.ask(engine, words)
    not_a_name = {w for w, (is_table, is_alias) in answers.items() if not (is_table and is_alias)}
    assert not_a_name <= sqlguard.RESERVED[engine], sorted(not_a_name - sqlguard.RESERVED[engine])
    assert all(answers[w.upper()] == (True, True) for w in ORACLE_ONLY)
    assert len(not_a_name) >= 60


@pytest.mark.parametrize('sql', [
    'SELECT city FROM public.customers',
    'SELECT c.city, c.full_name FROM public.customers c',
    'SELECT customers.city, public.customers.full_name FROM public.customers',
    'SELECT (c).city, c.* FROM public.customers AS c WHERE c.customer_id > 5 ORDER BY c.city LIMIT 3',
    'SELECT p.city, t.full_name FROM public.customers p JOIN telecom.customers t ON t.customer_id = p.customer_id',
    'SELECT s.level, s.size, s.comment FROM public.session s',                   # a table and columns called like Oracle words
    'SELECT session.level, session.id FROM public.session',
    'SELECT public.session.size FROM public.session',
    'SELECT session.city FROM telecom.customers session',                        # ... and aliases
    'SELECT level.city, size.balance FROM telecom.customers AS level JOIN telecom.accounts size ON size.customer_id = level.customer_id',
    'SELECT resource.city FROM (SELECT city FROM telecom.customers) resource',
    'WITH comment AS (SELECT city FROM telecom.customers) SELECT comment.city FROM comment',
    'WITH w (size) AS (SELECT city FROM telecom.customers) SELECT w.size FROM w',
    # a column alias called like one: the subquery gives a column of that name
    'SELECT v.size, v.level FROM (SELECT city AS size, count(*) AS level FROM telecom.customers GROUP BY city) v WHERE v.level > 1',
    'SELECT v.comment FROM (SELECT full_name comment FROM telecom.customers) v',
    'SELECT id, level FROM session',                                             # a table the source does not know: bare names
    'SELECT city FROM public.customers, resource',
])
def test_postgresql_reads_public_and_the_other_names_that_only_oracle_reserves(sql):
    assert passes(sql, 'postgresql', PUBLIC).sql == sql
    assert passes(sql, 'postgresql', None)                              # and the guard alone, as before


@pytest.mark.parametrize('sql', [
    'SELECT c.audit_and_return FROM public.customers c',                # audit_and_return(c): a function of the row
    'SELECT (c).audit_and_return FROM public.customers c',
    'SELECT customers.audit_and_return FROM public.customers',
    'SELECT public.customers.audit_and_return FROM public.customers',
    'SELECT (public.customers).audit_and_return FROM public.customers',
    'SELECT session.audit_and_return FROM telecom.customers session',   # the alias is a name now; what stands behind the dot is judged
    'SELECT (level).audit_and_return FROM telecom.customers AS level',
    'SELECT session.audit_and_return FROM public.session',
    'SELECT size.city FROM telecom.customers c, telecom.accounts size',         # a column - of the other table
    'SELECT resource.full_name FROM (SELECT city FROM telecom.customers) resource',
    'WITH comment AS (SELECT city FROM telecom.customers) SELECT comment.full_name FROM comment',
    'SELECT v.audit_and_return FROM (SELECT city AS size FROM telecom.customers) v',
    'SELECT v.level FROM (SELECT city AS size FROM telecom.customers) v',
    'SELECT s.id FROM resource s',                                      # not a table of the source: no columns to check
    'SELECT p.city FROM public.accounts p',                             # public has no such table: the same
    'SELECT c.city FROM public.customers c2',
])
def test_postgresql_still_refuses_what_could_be_a_function_of_the_row_under_these_names(sql):
    assert 'function' in blocked(sql, 'postgresql', PUBLIC)


def test_a_function_is_not_let_through_by_a_schema_that_is_a_name_now():
    for sql in ('SELECT public.audit_and_return(c) FROM public.customers c', 'SELECT public.audit_and_return()',
                'SELECT * FROM public.audit_and_return()', 'SELECT * FROM public.audit_and_return() AS session',
                'SELECT audit_and_return(c) FROM public.customers c', 'SELECT * FROM session.audit_and_return(1)'):
        for known in (PUBLIC, None):
            assert 'cannot be called here' in blocked(sql, 'postgresql', known) or 'not on the list of functions' in blocked(
                sql, 'postgresql', known), sql
    for write in ('SELECT 1 FROM public.customers; DELETE FROM public.customers', 'SELECT * INTO session FROM public.customers',
                  'SELECT * FROM public.customers FOR UPDATE', "SELECT nextval('public.session')",
                  'WITH session AS (DELETE FROM public.customers RETURNING *) SELECT * FROM session',
                  'SELECT * FROM public.customers session FOR SHARE OF session'):
        assert blocked(write, 'postgresql', PUBLIC), write


@pytest.mark.parametrize('word', ['user', 'end', 'analyse', 'array', 'both', 'do', 'false', 'placing', 'some', 'symmetric', 'verbose',
                                  'window', 'current_user', 'isnull', 'notnull', 'overlaps', 'tablesample', 'cross', 'limit'])
def test_postgresqls_own_reserved_words_are_not_names(word):
    """Where PostgreSQL itself does not take the word for a name, neither does the reader - whatever Oracle thinks of it."""
    assert word.upper() in sqlguard.RESERVED['postgresql']
    tables = [f'SELECT c.city FROM telecom.customers c JOIN {word} ON 1 = 1', f'SELECT 1 FROM {word}.customers']
    # behind a table, a word that starts a clause or a join is read as that (and PostgreSQL then says "syntax error")
    alias = [] if word in ('window', 'limit', 'cross', 'tablesample') else [f'SELECT city FROM telecom.customers {word}']
    for sql in tables + alias:
        message = blocked(sql, 'postgresql', PUBLIC)
        assert 'could not follow this FROM clause' in message or 'in a FROM clause is not something the guard can follow' in message, sql
    # as a column alias it is not one either: the subquery does not give a column of that name
    assert 'function' in blocked(f'SELECT v.{word} FROM (SELECT city AS {word} FROM telecom.customers) v', 'postgresql', PUBLIC)


def test_postgresql_a_word_that_needs_as_is_not_a_label_without_it():
    """x::character varying and x::double precision end with a word that is a name on PostgreSQL but never a
    label without AS. Read as "x::character, called varying", the subquery would seem to give a column
    "varying" - and s.varying, which calls a function varying(s) if the database has one, would pass.
    (Tried on PostgreSQL 16: it answers "column s.varying does not exist".)"""
    needs_as = sqlguard.NEEDS_AS['postgresql']
    assert sqlguard.NEEDS_AS == {'postgresql': needs_as} and not needs_as & sqlguard.RESERVED['postgresql']
    assert needs_as == {'CHAR', 'CHARACTER', 'DAY', 'FILTER', 'HOUR', 'MINUTE', 'MONTH', 'OVER', 'PRECISION', 'SECOND', 'VARYING',
                        'WITHIN', 'WITHOUT', 'YEAR'}
    for sql in ('SELECT s.varying FROM (SELECT full_name::character varying FROM telecom.customers) s',
                'SELECT s.precision FROM (SELECT customer_id::double precision FROM telecom.customers) s',
                'WITH s AS (SELECT full_name::char varying FROM telecom.customers) SELECT s.varying FROM s',
                'SELECT s.year FROM (SELECT customer_id year FROM telecom.customers) s'):
        assert 'function' in blocked(sql, 'postgresql', PUBLIC), sql
    for sql in ('SELECT s.n FROM (SELECT full_name::character varying AS n FROM telecom.customers) s',
                'SELECT s.v FROM (SELECT full_name::character varying v FROM telecom.customers) s',
                'SELECT s.year, s.precision FROM (SELECT customer_id AS year, city AS precision FROM telecom.customers) s',
                'SELECT c.city::character varying, c.customer_id::double precision FROM telecom.customers c'):
        assert passes(sql, 'postgresql', PUBLIC), sql


@pytest.mark.parametrize('word', ORACLE_ONLY + ['date', 'number', 'row', 'rows', 'index', 'mode', 'share', 'file', 'view'])
def test_oracle_is_judged_as_before(word):
    known = oracle()
    for sql in (f'SELECT 1 FROM {word}.customers', f'SELECT city FROM {word}', f'SELECT c.city FROM telecom.customers {word}',
                f'SELECT {word}.city FROM telecom.customers {word}'):
        message = blocked(sql, 'oracle', known)
        assert 'FROM clause' in message, (sql, message)


@pytest.mark.parametrize('engine', ['postgresql', 'oracle'])
def test_the_hostile_battery_is_still_refused_under_the_names_that_are_names_now(engine):
    """Every hostile statement again, with each of its tables and aliases called like each word that
    another engine reserves and this one does not (343 words on PostgreSQL). Not one passes."""
    import re
    released = sorted(sqlguard.RESERVED_ANYWHERE - sqlguard.RESERVED[engine])
    assert len(released) > 300 and (engine == 'oracle' or {'PUBLIC', 'SESSION', 'LEVEL', 'SIZE'} <= set(released))
    known, tried = source_names(engine), 0
    name_of = r"(?<![\w'])(p|q|c|d|u|i|v|a|x_parents|x_audit)(?![\w'])"       # not the q of Oracle's q'[ ... ]'
    for group, sql in battery(engine):
        for name in sorted(set(re.findall(name_of, sql))):
            for word in released:
                variant = re.sub(rf"(?<![\w']){name}(?![\w'])", word.lower(), sql)
                tried += 1
                with pytest.raises(ReadOnlyViolation):
                    check_read_only_sql(variant, engine, strict=True, names=known)
                if engine == 'postgresql' and group != 'call without brackets':
                    # a write, a lock, a function, a second statement: refused by the rules that read no names at all.
                    # The reader of names runs after them and can only refuse more - it cannot let one of these through.
                    with pytest.raises(ReadOnlyViolation):
                        check_read_only_sql(variant, engine, strict=True, names=None)
    assert tried > 10_000


def test_the_reader_of_names_only_ever_adds_a_refusal():
    """Whatever the lists say: a statement the guard refuses without a source is refused with one."""
    for engine, known in (('postgresql', PUBLIC), ('postgresql', POSTGRES), ('oracle', ORACLE)):
        for group, sql in battery(engine) + [('allowed', s) for s in hostile.allowed(engine, le.extra_schema(engine))]:
            try:
                check_read_only_sql(sql, engine, strict=True, names=None)
            except ReadOnlyViolation:
                with pytest.raises(ReadOnlyViolation):
                    check_read_only_sql(sql, engine, strict=True, names=known)
    with open(sqlguard.__file__, encoding='utf-8') as source:
        body = source.read().split('def check(')[1].split('\ndef ')[0]
    # in check(): the reader is the last thing that runs, behind every rule on words, functions and statements
    assert body.index('_Names(toks, dialect, names).run()') > body.index('is not on the list of functions that may be used here')
    assert body.index('_Names(toks, dialect, names).run()') > body.index('if t.up in WRITE_WORDS')


# ------------------------------------------------------- the app's own statements

def test_the_plain_guard_for_the_apps_own_statements_is_unchanged():
    for statement in ('SELECT 1', 'SHOW TABLES', 'PRAGMA table_info(x)', 'SET TRANSACTION READ ONLY', 'DESCRIBE t'):
        assert check_read_only_sql(statement) is None
    for statement in ('DELETE FROM t', 'SELECT 1; DROP TABLE t', 'SELECT * FROM t FOR UPDATE', 'PRAGMA journal_mode = WAL'):
        with pytest.raises(ReadOnlyViolation):
            check_read_only_sql(statement)
    assert check is sqlguard.check and check_read_only_sql('SELECT 1', 'mysql', strict=True).sql == 'SELECT 1'
