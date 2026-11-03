"""The live-database adapter only ever reads: SELECT / catalog statements, no commit, no change."""

import hashlib
import re
import shutil
import sqlite3
import time

import pytest
from sqlalchemy import event

from app.security import ReadOnlyViolation, check_read_only_sql
from app.sources import SourceError
from app.sources.database import (ENGINES, SESSION_SETUP, TRANSACTION_SETUP, ConnectionProfile, ReadOnlyDatabase,
                                  build_url, driver_installed)

WRITE = re.compile(r'\b(INSERT|UPDATE|DELETE|REPLACE|MERGE|CREATE|DROP|ALTER|TRUNCATE|ATTACH|VACUUM|COMMIT|'
                   r'SAVEPOINT|RELEASE|REINDEX|ANALYZE)\b', re.I)


def is_plain_read(statement):
    """An independent check (not the app's own guard): is this a SELECT or a read of the catalog?"""
    text = statement.strip()
    upper = text.upper()
    if WRITE.search(re.sub(r"'[^']*'", "''", upper)):
        return False
    if upper.startswith(('SELECT', 'WITH')):
        return True
    if upper.startswith('PRAGMA'):
        return '=' not in upper or upper == 'PRAGMA QUERY_ONLY = ON'
    return False


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def db_copy(demo_db, tmp_path):
    path = tmp_path / 'telecom.db'
    shutil.copy(demo_db, path)
    return path


@pytest.fixture
def traced(db_copy):
    """A database whose every statement - as SQLite itself sees it - is recorded."""
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(db_copy)))
    seen = []
    event.listen(db.engine, 'connect', lambda dbapi, record: dbapi.set_trace_callback(seen.append))
    yield db, seen
    db.dispose()


def test_reading_a_schema_runs_only_selects_and_catalog_reads(traced, db_copy):
    db, seen = traced
    before, modified = sha(db_copy), db_copy.stat().st_mtime_ns
    info = db.test()
    model, _ = db.read_schema()
    assert info['tables'] == 10 and len(model.tables) == 10 and len(model.relationships) == 13

    assert len(seen) > 20                                        # the trace really saw the work
    not_reads = [s for s in seen if not is_plain_read(s)]
    assert not_reads == []
    assert all(is_plain_read(s) for s in db.statements)          # the adapter's own audit list agrees
    assert {s.split()[0].upper() for s in db.statements} <= {'SELECT', 'PRAGMA'}

    assert sha(db_copy) == before and db_copy.stat().st_mtime_ns == modified
    assert sorted(p.name for p in db_copy.parent.iterdir()) == ['telecom.db']     # no journal / wal files


def test_every_statement_the_adapter_sends_passes_the_guard(traced):
    db, _ = traced
    db.read_schema()
    for statement in db.statements:
        check_read_only_sql(statement, dialect='sqlite')         # does not raise


@pytest.mark.parametrize('statement', [
    "DELETE FROM customers",
    "UPDATE customers SET full_name = 'x'",
    "INSERT INTO plans (plan_id, plan_name, plan_type, monthly_fee) VALUES (99, 'x', 'prepaid', 1)",
    "DROP TABLE tickets",
    "CREATE TABLE hack (a INT)",
    "ALTER TABLE plans ADD COLUMN x INT",
    "PRAGMA writable_schema = ON",
    "PRAGMA journal_mode = WAL",
    "VACUUM",
    "SELECT 1; DELETE FROM customers",
])
def test_the_guard_blocks_writes_before_they_reach_the_database(traced, db_copy, statement):
    db, seen = traced
    before = sha(db_copy)
    with pytest.raises(ReadOnlyViolation):
        with db.connect() as conn:
            conn.exec_driver_sql(statement)
    assert all(is_plain_read(s) for s in seen)                   # SQLite never saw the write
    assert statement not in db.statements                        # refused before it was recorded as sent
    assert sha(db_copy) == before


def test_the_app_never_commits(traced):
    db, seen = traced
    with pytest.raises(ReadOnlyViolation, match='never commits'):
        with db.connect() as conn:
            conn.exec_driver_sql('SELECT COUNT(*) FROM customers').scalar()
            conn.commit()
    assert not any('COMMIT' in s.upper() for s in seen)


def test_the_connection_itself_is_read_only_even_without_the_guard(traced, db_copy):
    db, _ = traced
    before = sha(db_copy)
    with db.connect() as conn:
        raw = conn.connection.dbapi_connection                   # go around SQLAlchemy and the guard
        with pytest.raises(sqlite3.OperationalError, match='readonly|read-only|query_only'):
            raw.execute('DELETE FROM customers')
        with pytest.raises(sqlite3.OperationalError):
            raw.execute('CREATE TABLE hack (a INT)')
        assert raw.execute('SELECT COUNT(*) FROM customers').fetchone()[0] == 80
    assert sha(db_copy) == before


def test_statements_time_out(db_copy):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(db_copy)), timeout_s=1)
    endless = ('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5000000000) '
               'SELECT COUNT(*) FROM n')
    started = time.monotonic()
    with pytest.raises(Exception, match='interrupted'):
        with db.connect() as conn:
            conn.exec_driver_sql(endless).scalar()
    assert time.monotonic() - started < 15
    with db.connect() as conn:                                   # the next query still works
        assert conn.exec_driver_sql('SELECT COUNT(*) FROM plans').scalar() == 10
    db.dispose()


def test_a_missing_sqlite_file_is_an_error_and_is_not_created(tmp_path):
    missing = tmp_path / 'nope.db'
    with pytest.raises(SourceError, match='not found'):
        ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(missing)))
    assert not missing.exists()


# ---------------------------------------------------------------- the guard

ALLOWED = [
    'SELECT 1',
    'select * from customers where full_name = \'DROP TABLE x; DELETE\'',
    'SELECT c.update_rule, c.delete_rule FROM information_schema.referential_constraints c',
    'WITH t AS (SELECT 1 AS a) SELECT a FROM t;',
    '(SELECT 1) UNION ALL (SELECT 2)',
    'SELECT "insert", [update] FROM t -- delete everything',
    "SELECT REPLACE(name, 'a', 'b') FROM t /* drop */",
    'SHOW CREATE TABLE `orders`',
    'SHOW VARIABLES LIKE \'sql_mode\'',
    'DESCRIBE `orders`',
    'PRAGMA main.table_xinfo("customers")',
    'PRAGMA foreign_key_list("customers")',
    'PRAGMA temp.index_list("x")',
    'PRAGMA query_only = ON',
    'SET TRANSACTION READ ONLY',
    'SET SESSION TRANSACTION READ ONLY',
    'SET LOCAL statement_timeout = 30000',
    'SET SESSION MAX_EXECUTION_TIME = 30000',
]
BLOCKED = [
    '', '   ', ';',
    'INSERT INTO t VALUES (1)', 'UPDATE t SET a = 1', 'DELETE FROM t', 'MERGE INTO t USING s ON (1=1)',
    'DROP TABLE t', 'TRUNCATE TABLE t', 'ALTER TABLE t ADD c INT', 'CREATE TABLE t (a INT)',
    'GRANT ALL ON t TO u', 'CALL do_things()', 'EXEC sp_who', 'BEGIN delete_all; END;', 'COMMIT', 'ROLLBACK',
    'SELECT 1; DROP TABLE t', 'SELECT 1;\nDELETE FROM t',
    'SELECT * INTO backup FROM t',
    'WITH x AS (SELECT 1) DELETE FROM t',
    'WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x',
    'SELECT * FROM t FOR UPDATE', 'SELECT * FROM t FOR SHARE', 'SELECT * FROM t LOCK IN SHARE MODE',
    'SELECT 1 /*! INTO OUTFILE \'/tmp/x\' */',
    'PRAGMA writable_schema = ON', 'PRAGMA journal_mode = WAL', 'PRAGMA user_version(5)',
    'PRAGMA writable_schema(table_info)', 'PRAGMA table_info(x) = 1',
    'SET TRANSACTION READ WRITE', 'SET SESSION TRANSACTION READ WRITE', 'SET LOCAL statement_timeout = 0; DROP TABLE t',
    'SET autocommit = 1', 'USE other_db', 'EXPLAIN ANALYZE DELETE FROM t', 'VACUUM', 'ATTACH DATABASE \'x\' AS y',
    'LOAD DATA INFILE \'x\' INTO TABLE t', 'COPY t FROM \'/tmp/x\'',
]


@pytest.mark.parametrize('statement', ALLOWED)
def test_guard_allows_reads_and_known_session_settings(statement):
    check_read_only_sql(statement)


@pytest.mark.parametrize('statement', BLOCKED)
def test_guard_blocks_everything_else(statement):
    with pytest.raises(ReadOnlyViolation):
        check_read_only_sql(statement)


def test_guard_allows_database_switch_only_on_sql_server():
    check_read_only_sql('use [Sales]', dialect='mssql')          # SQLAlchemy does this to read "db.schema"
    for dialect in ('mysql', 'postgresql', 'oracle', 'sqlite', None):
        with pytest.raises(ReadOnlyViolation):
            check_read_only_sql('use Sales', dialect=dialect)
    with pytest.raises(ReadOnlyViolation):
        check_read_only_sql('use [Sales] drop table t', dialect='mssql')


def test_every_session_setting_the_adapter_uses_is_known_to_the_guard():
    used = [s.format(ms=30000) for group in (SESSION_SETUP, TRANSACTION_SETUP) for stmts in group.values()
            for s in stmts]
    used += ['SET SESSION MAX_EXECUTION_TIME = 30000', 'SET SESSION max_statement_time = 30']
    for statement in used:
        check_read_only_sql(statement)
        assert statement.upper().startswith(('SET ', 'PRAGMA QUERY_ONLY'))
        assert not WRITE.search(statement)


STRICT_BLOCKED = [
    "SELECT pg_sleep(100)", "SELECT dbms_lock.sleep(10) FROM dual", "SELECT utl_http.request('http://x') FROM dual",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'DELETE FROM t')", "SELECT nextval('seq')", "SELECT load_file('/etc/passwd')",
    "SELECT load_extension('x')", "SELECT '\\'' INTO OUTFILE '/tmp/x' -- '", "SELECT $$ ' $$; DROP TABLE t; --'",
    "SELECT 5 --1 INTO OUTFILE '/tmp/x'", "SELECT 1 # '\n; DROP TABLE t; -- '",
    'SHOW TABLES', 'PRAGMA table_info(x)', 'SET TRANSACTION READ ONLY', 'DESCRIBE t',
]


@pytest.mark.parametrize('statement', STRICT_BLOCKED)
def test_strict_mode_for_user_sql_is_tighter(statement):
    """strict=True is the hook for the session 3 SQL runner: SELECT / WITH only, and no known tricks."""
    with pytest.raises(ReadOnlyViolation):
        check_read_only_sql(statement, dialect=None, strict=True)


def test_strict_mode_still_allows_ordinary_queries():
    check_read_only_sql('SELECT c.city, COUNT(*) FROM customers c GROUP BY c.city ORDER BY 2 DESC', strict=True)
    check_read_only_sql("WITH a AS (SELECT 1 AS x) SELECT x FROM a -- note", dialect='postgresql', strict=True)
    check_read_only_sql("SELECT * FROM #temp WHERE path LIKE 'C:\\data%'", dialect='mssql', strict=True)


# ------------------------------------------------- profiles, urls, passwords

def test_a_profile_has_no_password_field():
    profile = ConnectionProfile.from_dict({'engine': 'PostgreSQL', 'host': ' db1 ', 'port': '5432', 'database': 'crm',
                                           'username': 'reader', 'password': 'S3cret!', 'schema_filter': 'sales, hr'})
    assert 'password' not in profile.to_dict() and 'S3cret!' not in repr(profile)
    assert profile.to_dict() == {'engine': 'postgresql', 'host': 'db1', 'port': 5432, 'database': 'crm',
                                 'username': 'reader', 'schema_filter': 'sales, hr', 'include_views': False}
    assert profile.schemas() == ['sales', 'hr']
    with pytest.raises(SourceError):
        ConnectionProfile.from_dict({'engine': 'access'})


def test_urls_use_the_expected_driver_for_each_engine():
    def url(**kw):
        return build_url(ConnectionProfile(host='h', username='u', **kw), 'S3cret!')
    oracle = url(engine='oracle', port=1521, database='ORCLPDB1')
    assert oracle.drivername == 'oracle+oracledb' and oracle.query == {'service_name': 'ORCLPDB1'} and oracle.database is None
    assert url(engine='oracle', database='sid:ORCL').database == 'ORCL'
    assert url(engine='mssql', port=1433, database='crm').drivername == 'mssql+pymssql'
    assert url(engine='postgresql', database='crm').drivername == 'postgresql+psycopg'
    assert url(engine='mysql', database='crm').drivername == 'mysql+pymysql'
    assert 'S3cret!' not in str(oracle) and 'S3cret!' not in repr(oracle)      # SQLAlchemy masks it
    with pytest.raises(SourceError, match='Service name'):
        url(engine='oracle')
    with pytest.raises(SourceError, match='Host'):
        build_url(ConnectionProfile(engine='mysql'), None)


def test_all_database_drivers_are_installed():
    assert {e: driver_installed(e) for e in ENGINES} == {e: True for e in ENGINES}


def test_error_messages_never_contain_the_password(demo_db):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(demo_db)), password='S3cret!')
    message = db.clean_error(RuntimeError('login failed for reader:S3cret!@db1 (Background on this error at: https://x)'))
    assert 'S3cret!' not in message and '***' in message and 'Background' not in message
    db.dispose()


def test_cannot_connect_is_reported_quickly_and_cleanly():
    db = ReadOnlyDatabase(ConnectionProfile(engine='postgresql', host='127.0.0.1', port=9, database='x',
                                            username='u'), password='S3cret!')
    started = time.monotonic()
    with pytest.raises(SourceError) as failure:
        db.test()
    assert time.monotonic() - started < 30
    assert 'S3cret!' not in str(failure.value)
    db.dispose()
