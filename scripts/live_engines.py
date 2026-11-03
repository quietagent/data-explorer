"""Local test copies of the four server engines, in Docker, for tests/test_live_engines.py.

    scripts\\live_db.ps1 start | seed | status | test | stop      the usual way to use this

    python -m scripts.live_engines wait   [engine ...]    wait until the servers accept logins
    python -m scripts.live_engines seed   [engine ...]    create the schema, the data and the read-only user
    python -m scripts.live_engines extras [engine ...]    (re)create the dx_extra schema only
    python -m scripts.live_engines status                 which engines are reachable

Each engine gets:
  * the demo telecom model (app/demo.py): 10 tables, 13 declared foreign keys, the same rows
  * one big table, usage_events, with 2,000,000 generated rows and no declared foreign key
  * a second schema ("other") with one small table, to test the schema filter
  * a third schema ("dx_extra", see ensure_extras) with what session 3's tests need: parents and
    children with orphans, a two-column key, a table without statistics, large and binary columns,
    call records, a sequence and a function that writes
  * a read-only account, explorer_ro, which is what the app and the tests connect with

The DDL is generated per dialect from app.demo.DDL. Everything here is test tooling: it
writes to throw-away containers that listen on 127.0.0.1 only. The app itself never
connects with the admin account and never writes.

The passwords below are for these local containers only; they protect nothing.
"""

from __future__ import annotations

import html
import random
import re
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import LargeBinary, bindparam, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

from app import demo, er

ADMIN_PASSWORD = 'Dx-Admin-2026-local'
READER_PASSWORD = 'Dx-Reader-2026-local'
READER = 'explorer_ro'
HOST = '127.0.0.1'
BIG_TABLE = 'usage_events'
BIG_ROWS = 2_000_000

# port: the port on this PC (not the engines' usual ones, so a local server is never touched)
ENGINES = {
    'oracle': {'container': 'dx-oracle', 'image': 'gvenzl/oracle-free:slim', 'port': 11521,
               'container_port': 1521, 'env': {'ORACLE_PASSWORD': ADMIN_PASSWORD},
               'admin_user': 'system', 'database': 'FREEPDB1', 'schema': 'TELECOM', 'other': 'OTHER',
               'schema_filter': 'TELECOM', 'reader': 'EXPLORER_RO'},
    'mssql': {'container': 'dx-mssql', 'image': 'mcr.microsoft.com/mssql/server:2022-latest', 'port': 11433,
              'container_port': 1433,
              # ACCEPT_EULA=Y accepts Microsoft's SQL Server licence terms for the free Developer edition.
              'env': {'ACCEPT_EULA': 'Y', 'MSSQL_PID': 'Developer', 'MSSQL_SA_PASSWORD': ADMIN_PASSWORD},
              'admin_user': 'sa', 'database': 'telecom', 'schema': 'dbo', 'other': 'other',
              'schema_filter': '', 'reader': READER},
    'postgresql': {'container': 'dx-postgres', 'image': 'postgres:16', 'port': 15432, 'container_port': 5432,
                   'env': {'POSTGRES_PASSWORD': ADMIN_PASSWORD, 'POSTGRES_DB': 'telecom'},
                   'admin_user': 'postgres', 'database': 'telecom', 'schema': 'telecom', 'other': 'other',
                   'schema_filter': 'telecom', 'reader': READER},
    'mysql': {'container': 'dx-mysql', 'image': 'mysql:8.4', 'port': 13306, 'container_port': 3306,
              'env': {'MYSQL_ROOT_PASSWORD': ADMIN_PASSWORD},
              'admin_user': 'root', 'database': 'telecom', 'schema': 'telecom', 'other': 'other',
              'schema_filter': '', 'reader': READER},
}
DRIVERS = {'oracle': 'oracle+oracledb', 'mssql': 'mssql+pymssql', 'postgresql': 'postgresql+psycopg',
           'mysql': 'mysql+pymysql'}


# ------------------------------------------------------------- connections ----

def reader_profile(engine, **overrides):
    """Connection settings of the read-only account, as the app's ConnectionProfile takes them."""
    spec = ENGINES[engine]
    settings = {'engine': engine, 'host': HOST, 'port': spec['port'], 'database': spec['database'],
                'username': spec['reader'], 'schema_filter': spec['schema_filter'], 'include_views': False}
    settings.update(overrides)
    return settings


def admin_engine(engine, database=None):
    """A normal, writing SQLAlchemy engine for the admin account. Used for seeding and for checking
    afterwards that nothing changed - never by the app."""
    spec = ENGINES[engine]
    database = database or spec['database']
    query = {}
    if engine == 'oracle':
        database, query = None, {'service_name': spec['database']}
    url = URL.create(DRIVERS[engine], username=spec['admin_user'], password=ADMIN_PASSWORD, host=HOST,
                     port=spec['port'], database=database, query=query)
    args = {'login_timeout': 5} if engine == 'mssql' else {'connect_timeout': 5} if engine in ('postgresql', 'mysql') \
        else {'tcp_connect_timeout': 5}
    return create_engine(url, poolclass=NullPool, connect_args=args)


def port_open(engine):
    try:
        with socket.create_connection((HOST, ENGINES[engine]['port']), timeout=1.0):
            return True
    except OSError:
        return False


def reachable(engine, seeded=True):
    """True when the server accepts the admin login (and, with seeded, holds the seeded big table)."""
    if not port_open(engine):
        return False
    spec = ENGINES[engine]
    try:
        eng = admin_engine(engine, 'master' if engine == 'mssql' and not seeded else
                           'mysql' if engine == 'mysql' and not seeded else None)
        try:
            with eng.connect() as conn:
                if not seeded:
                    return True
                schema = spec['schema']
                return conn.execute(text(f'SELECT MAX(event_id) FROM {schema}.{BIG_TABLE}')).scalar() == BIG_ROWS
        finally:
            eng.dispose()
    except Exception:
        return False


def wait(engine, timeout_s=420):
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        if reachable(engine, seeded=False):
            return round(time.monotonic() - started)
        time.sleep(3)
    raise SystemExit(f'{engine}: not accepting logins after {timeout_s} s (docker logs {ENGINES[engine]["container"]})')


# ------------------------------------------------------------ DDL per dialect ----

def demo_model():
    """The demo tables and foreign keys, read from app.demo.DDL with the app's own SQL parser."""
    em = er.parse_sql(demo.DDL)
    tables = {}
    for t in em.tables:
        columns = sorted(t.columns.values(), key=lambda c: c.ddl_order)
        tables[t.name] = [(c.name, c.type, c.nullable, c.pk) for c in columns]
    keys = [(t.name, cols, rt.name, refcols) for t, cols, rt, refcols in em.declared_fks]
    assert list(tables) == demo.TABLES and len(keys) == 13
    return tables, keys


def column_type(base, engine):
    """One column type of the demo DDL in the dialect of `engine`."""
    kind = re.sub(r'\s+', '', base.upper())
    if engine == 'oracle':
        kind = kind.replace('INTEGER', 'NUMBER(10)').replace('VARCHAR(', 'VARCHAR2(').replace('DECIMAL(', 'NUMBER(')
    elif engine == 'mssql':
        kind = kind.replace('INTEGER', 'INT').replace('TIMESTAMP', 'DATETIME2(0)')     # TIMESTAMP is rowversion there
    elif engine == 'mysql':
        kind = kind.replace('INTEGER', 'INT').replace('TIMESTAMP', 'DATETIME')
    return kind


def create_statements(engine):
    """CREATE TABLE for the ten demo tables (keys as named, table-level constraints), the big table,
    and the table of the second schema."""
    spec = ENGINES[engine]
    schema, other = spec['schema'], spec['other']
    tables, keys = demo_model()
    out = []
    for name, columns in tables.items():
        lines = [f'    {c} {column_type(kind, engine)}{"" if nullable else " NOT NULL"}' for c, kind, nullable, _ in columns]
        lines.append(f'    CONSTRAINT pk_{name} PRIMARY KEY ({", ".join(c for c, _, _, pk in columns if pk)})')
        for table, cols, ref, refcols in keys:
            if table == name:
                lines.append(f'    CONSTRAINT fk_{name}_{cols[0]} FOREIGN KEY ({", ".join(cols)}) '
                             f'REFERENCES {schema}.{ref} ({", ".join(refcols)})')
        out.append(f'CREATE TABLE {schema}.{name} (\n' + ',\n'.join(lines) + '\n)')
    big = [('event_id', 'INTEGER', False), ('subscription_id', 'INTEGER', False), ('event_time', 'TIMESTAMP', False),
           ('event_type', 'VARCHAR(10)', False), ('duration_s', 'INTEGER', True), ('data_mb', 'DECIMAL(10,2)', True),
           ('msisdn', 'VARCHAR(12)', False), ('cell_id', 'VARCHAR(8)', False)]
    lines = [f'    {c} {column_type(kind, engine)}{"" if nullable else " NOT NULL"}' for c, kind, nullable in big]
    lines.append(f'    CONSTRAINT pk_{BIG_TABLE} PRIMARY KEY (event_id)')        # subscription_id: no key declared
    out.append(f'CREATE TABLE {schema}.{BIG_TABLE} (\n' + ',\n'.join(lines) + '\n)')
    out.append(f'CREATE TABLE {other}.decoy (\n    decoy_id {column_type("INTEGER", engine)} NOT NULL,\n'
               f'    note {column_type("VARCHAR(20)", engine)},\n    CONSTRAINT pk_decoy PRIMARY KEY (decoy_id)\n)')
    return out


# 2,000,000 generated rows; the same values on every engine (g = 1 .. 2,000,000):
#   subscription_id 1 + g mod 150 · event_time 2025-01-01 + (g * 37 mod 55,123,200) seconds
#   event_type by g mod 10: 0-4 data, 5-7 voice, 8 sms, 9 roaming
#   duration_s only for voice (70% NULL) · data_mb only for data and roaming (40% NULL)
#   msisdn 9665 + 8 digits · cell_id C + (g mod 500)
BIG_INSERT = {
    'postgresql': """
        INSERT INTO {s}.usage_events
        SELECT g, 1 + g % 150, TIMESTAMP '2025-01-01 00:00:00' + ((g * 37) % 55123200) * INTERVAL '1 second',
               CASE WHEN g % 10 < 5 THEN 'data' WHEN g % 10 < 8 THEN 'voice' WHEN g % 10 = 8 THEN 'sms' ELSE 'roaming' END,
               CASE WHEN g % 10 BETWEEN 5 AND 7 THEN 10 + g % 590 END,
               CASE WHEN g % 10 < 5 OR g % 10 = 9 THEN (g % 5000) / 10.0 END,
               '9665' || LPAD(CAST((CAST(g AS BIGINT) * 7919) % 100000000 AS VARCHAR), 8, '0'),
               'C' || CAST(g % 500 AS VARCHAR)
        FROM generate_series(1, {n}) AS g""",
    'mysql': """
        INSERT INTO {s}.usage_events
        WITH RECURSIVE seq (g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM seq WHERE g < {n})
        SELECT g, 1 + g % 150, TIMESTAMPADD(SECOND, (g * 37) % 55123200, '2025-01-01 00:00:00'),
               CASE WHEN g % 10 < 5 THEN 'data' WHEN g % 10 < 8 THEN 'voice' WHEN g % 10 = 8 THEN 'sms' ELSE 'roaming' END,
               CASE WHEN g % 10 BETWEEN 5 AND 7 THEN 10 + g % 590 END,
               CASE WHEN g % 10 < 5 OR g % 10 = 9 THEN (g % 5000) / 10.0 END,
               CONCAT('9665', LPAD((g * 7919) % 100000000, 8, '0')), CONCAT('C', g % 500)
        FROM seq""",
    'mssql': """
        INSERT INTO {s}.usage_events
        SELECT g, 1 + g % 150, DATEADD(SECOND, (g * 37) % 55123200, CAST('2025-01-01' AS DATETIME2(0))),
               CASE WHEN g % 10 < 5 THEN 'data' WHEN g % 10 < 8 THEN 'voice' WHEN g % 10 = 8 THEN 'sms' ELSE 'roaming' END,
               CASE WHEN g % 10 BETWEEN 5 AND 7 THEN 10 + g % 590 END,
               CASE WHEN g % 10 < 5 OR g % 10 = 9 THEN (g % 5000) / 10.0 END,
               '9665' + RIGHT('00000000' + CAST((CAST(g AS BIGINT) * 7919) % 100000000 AS VARCHAR(8)), 8),
               'C' + CAST(g % 500 AS VARCHAR(3))
        FROM (SELECT value AS g FROM GENERATE_SERIES(1, {n})) x""",
    'oracle': """
        INSERT /*+ APPEND */ INTO {s}.usage_events
        SELECT g, 1 + MOD(g, 150), TIMESTAMP '2025-01-01 00:00:00' + NUMTODSINTERVAL(MOD(g * 37, 55123200), 'SECOND'),
               CASE WHEN MOD(g, 10) < 5 THEN 'data' WHEN MOD(g, 10) < 8 THEN 'voice' WHEN MOD(g, 10) = 8 THEN 'sms' ELSE 'roaming' END,
               CASE WHEN MOD(g, 10) BETWEEN 5 AND 7 THEN 10 + MOD(g, 590) END,
               CASE WHEN MOD(g, 10) < 5 OR MOD(g, 10) = 9 THEN MOD(g, 5000) / 10 END,
               '9665' || LPAD(TO_CHAR(MOD(g * 7919, 100000000)), 8, '0'), 'C' || TO_CHAR(MOD(g, 500))
        FROM (SELECT (a.n - 1) * 2000 + b.n AS g
              FROM (SELECT LEVEL AS n FROM dual CONNECT BY LEVEL <= {n} / 2000) a
              CROSS JOIN (SELECT LEVEL AS n FROM dual CONNECT BY LEVEL <= 2000) b)""",
}


# ------------------------------------------------------------------ seeding ----

def _run(conn, statement, **params):
    conn.execute(text(statement), params)


def _prepare(engine):
    """Empty schemas and the read-only account. Safe to run again: everything is dropped first."""
    spec = ENGINES[engine]
    schema, other, reader = spec['schema'], spec['other'], spec['reader']
    if engine == 'postgresql':
        eng = admin_engine(engine)
        with eng.begin() as conn:
            for s in (schema, other):
                _run(conn, f'DROP SCHEMA IF EXISTS {s} CASCADE')
                _run(conn, f'CREATE SCHEMA {s}')
            if not conn.execute(text('SELECT 1 FROM pg_roles WHERE rolname = :r'), {'r': reader}).first():
                _run(conn, f"CREATE ROLE {reader} LOGIN PASSWORD '{READER_PASSWORD}'")
        eng.dispose()
    elif engine == 'mysql':
        eng = admin_engine(engine, 'mysql')
        with eng.begin() as conn:
            for s in (schema, other):
                _run(conn, f'DROP DATABASE IF EXISTS {s}')
                _run(conn, f'CREATE DATABASE {s} CHARACTER SET utf8mb4')
            _run(conn, f"CREATE USER IF NOT EXISTS '{reader}'@'%' IDENTIFIED BY '{READER_PASSWORD}'")
        eng.dispose()
    elif engine == 'mssql':
        eng = admin_engine(engine, 'master')
        with eng.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            if conn.execute(text("SELECT DB_ID('telecom')")).scalar() is not None:
                _run(conn, 'ALTER DATABASE telecom SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
                _run(conn, 'DROP DATABASE telecom')
            _run(conn, 'CREATE DATABASE telecom')
            _run(conn, 'ALTER DATABASE telecom SET RECOVERY SIMPLE')
            if not conn.execute(text('SELECT 1 FROM sys.sql_logins WHERE name = :r'), {'r': reader}).first():
                _run(conn, f"CREATE LOGIN {reader} WITH PASSWORD = '{READER_PASSWORD}', CHECK_POLICY = OFF, "
                           f"DEFAULT_DATABASE = telecom")
        eng.dispose()
        eng = admin_engine(engine)
        with eng.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            _run(conn, f'CREATE SCHEMA {other}')
        eng.dispose()
    elif engine == 'oracle':
        eng = admin_engine(engine)
        with eng.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            for user in (schema, other, reader):
                if conn.execute(text('SELECT 1 FROM all_users WHERE username = :u'), {'u': user}).first():
                    _run(conn, f'DROP USER {user} CASCADE')
            for owner in (schema, other):                   # schema owners: they never log in
                _run(conn, f'CREATE USER {owner} IDENTIFIED BY "{ADMIN_PASSWORD}" ACCOUNT LOCK QUOTA UNLIMITED ON USERS')
            _run(conn, f'CREATE USER {reader} IDENTIFIED BY "{READER_PASSWORD}"')
        eng.dispose()


def _grant_read_only(engine, conn, tables):
    """The read-only account: it can connect and SELECT, nothing else."""
    spec = ENGINES[engine]
    schema, other, reader = spec['schema'], spec['other'], spec['reader']
    if engine == 'postgresql':
        _run(conn, f'GRANT CONNECT ON DATABASE {spec["database"]} TO {reader}')
        _run(conn, f'GRANT USAGE ON SCHEMA {schema}, {other} TO {reader}')
        _run(conn, f'GRANT SELECT ON ALL TABLES IN SCHEMA {schema}, {other} TO {reader}')
    elif engine == 'mysql':
        _run(conn, f"GRANT SELECT, SHOW VIEW ON {schema}.* TO '{reader}'@'%'")
        _run(conn, f"GRANT SELECT, SHOW VIEW ON {other}.* TO '{reader}'@'%'")
    elif engine == 'mssql':
        _run(conn, f'CREATE USER {reader} FOR LOGIN {reader}')
        _run(conn, f'ALTER ROLE db_datareader ADD MEMBER {reader}')
        _run(conn, f'GRANT VIEW DEFINITION TO {reader}')
    elif engine == 'oracle':
        _run(conn, f'GRANT CREATE SESSION TO {reader}')
        for t in tables:
            _run(conn, f'GRANT SELECT ON {schema}.{t} TO {reader}')
        _run(conn, f'GRANT SELECT ON {other}.decoy TO {reader}')


def _statistics(engine, conn, tables):
    """Fresh optimizer statistics, so the catalog's row estimates are filled in."""
    schema = ENGINES[engine]['schema']
    if engine == 'postgresql':
        _run(conn, 'ANALYZE')
    elif engine == 'mysql':
        for t in tables:
            conn.execute(text(f'ANALYZE TABLE {schema}.{t}')).fetchall()
    elif engine == 'mssql':
        _run(conn, 'EXEC sp_updatestats')
    elif engine == 'oracle':
        _run(conn, "BEGIN DBMS_STATS.GATHER_SCHEMA_STATS(:s); END;", s=schema)


def seed(engine, log=print):
    started = time.monotonic()
    spec = ENGINES[engine]
    schema = spec['schema']
    _prepare(engine)
    data = demo._generate(random.Random(20261012))             # the same rows as data/demo
    eng = admin_engine(engine)
    autocommit = engine in ('mssql', 'oracle')                  # DDL commits by itself there anyway
    with eng.connect() as conn:
        if autocommit:
            conn = conn.execution_options(isolation_level='AUTOCOMMIT')
        for statement in create_statements(engine):
            _run(conn, statement)
        for table in demo.TABLES:
            columns, rows = data[table]
            insert = text(f'INSERT INTO {schema}.{table} ({", ".join(columns)}) '
                          f'VALUES ({", ".join(":" + c for c in columns)})')
            conn.execute(insert, [dict(zip(columns, row)) for row in rows])
        _run(conn, f"INSERT INTO {spec['other']}.decoy (decoy_id, note) VALUES (1, 'not telecom')")
        if engine == 'mysql':
            _run(conn, f'SET SESSION cte_max_recursion_depth = {BIG_ROWS + 10}')
        _run(conn, BIG_INSERT[engine].format(s=schema, n=BIG_ROWS))
        if not autocommit:
            conn.commit()
        tables = demo.TABLES + [BIG_TABLE]
        _grant_read_only(engine, conn, tables)
        _statistics(engine, conn, tables)
        if not autocommit:
            conn.commit()
        counted = conn.execute(text(f'SELECT COUNT(*) FROM {schema}.{BIG_TABLE}')).scalar()
    eng.dispose()
    assert counted == BIG_ROWS, counted
    ensure_extras(engine, force=True)
    log(f'{engine}: seeded 10 demo tables + {BIG_TABLE} ({counted:,} rows) in {round(time.monotonic() - started)} s')


# ------------------------------------------------------------ the third schema ----

EXTRA = 'dx_extra'
EXTRAS_VERSION = 8
EXTRA_TABLES = ['x_parents', 'x_children', 'x_profiles', 'x_pairs', 'x_pair_items', 'x_never_counted', 'x_docs',
                'x_cdr', 'x_stores', 'x_staff', 'x_audit', 'x_days', 'x_shifts', 'x_bookings', 'x_slots', 'x_visits']
# Keys that are dates (version 8). A day: DATE everywhere - which on Oracle holds a time of day too. A moment:
MOMENT = {'oracle': 'DATE', 'mssql': 'DATETIME2(0)', 'postgresql': 'TIMESTAMP', 'mysql': 'DATETIME'}
CALL_LOG = 'x_calls'                # one row per call of x_ping, status, x_pkg.ping (Oracle) and x_peek (PostgreSQL)
LONG_TEXT = {'oracle': 'CLOB', 'mssql': 'VARCHAR(MAX)', 'postgresql': 'TEXT', 'mysql': 'LONGTEXT'}
BINARY = {'oracle': 'BLOB', 'mssql': 'VARBINARY(MAX)', 'postgresql': 'BYTEA', 'mysql': 'LONGBLOB'}
BIG_NUMBER = {'oracle': 'NUMBER(19)', 'mssql': 'BIGINT', 'postgresql': 'BIGINT', 'mysql': 'BIGINT'}
# Made up, like everything here. The shops are things, the staff are people: the SQL tab shows the one
# kind of name and masks the other (tests/test_live_sql.py).
STORES = [('Olaya Gallery', 'Riyadh'), ('Corniche Outlet', 'Jeddah'), ('Mountain Kiosk', 'Abha'), ('Airport Shop', 'Riyadh'),
          ('Harbour Store', 'Dammam'), ('Old Town Stall', 'Jeddah')]
STAFF = ['Noura', 'Faisal', 'Layla', 'Khalid', 'Maha', 'Sultan', 'Reem', 'Yousef', 'Hessa', 'Tariq', 'Dana', 'Majed']
# PostgreSQL only: a table in the schema every PostgreSQL database has, "public" - a reserved word of Oracle,
# and until 13 Oct 2026 the guard refused FROM public.<table> for that.
PUBLIC_TABLE = 'x_guests'
GUESTS = [('Salma', 'A-101'), ('Omar', 'B-204'), ('Huda', 'C-310')]


def extra_schema(engine):
    """The third schema as the engine names it (Oracle: upper case; SQL Server: a schema of the database)."""
    return EXTRA.upper() if engine == 'oracle' else EXTRA


def extra_filter(engine):
    """A schema filter that loads the demo schema and the third one together."""
    spec = ENGINES[engine]
    return f'{spec["schema"]}, {extra_schema(engine)}'


def extra_rows():
    """The rows of the third schema: the same on every engine, and small.

    x_parents       200 rows; parent_id is the key, code is unique (a constraint), grp is not
    x_children      1,000 rows; no foreign key is declared. parent_id and parent_code point at
                    x_parents: 5% are empty, and 19 of the 209 values used do not exist there
    x_profiles      150 rows; its key IS its foreign key to x_parents (one to one)
    x_pairs         100 rows; the key is (a, b)
    x_pair_items    500 rows; a declared two-column foreign key that was never validated:
                    the rows with a = 11 have no parent, and 4% have no a at all
    x_never_counted 150 rows the optimizer has never looked at (no statistics)
    x_docs          30 rows with a large text and a binary column
    x_cdr           300 call records: numbers, IMSI, a time in epoch seconds
    x_stores        6 shops: a table of things with a column called just "name"
    x_staff         12 people: first_name and salary (personal), job_title and the shop they work in
    x_audit         empty: only x_touch() writes to it
    x_calls         empty, and not shown to the read-only account: the functions that can be called
                    without brackets write one row per call into it. Oracle: x_ping, status and
                    x_pkg.ping (autonomous transactions; the read-only account may execute them, and
                    each has a public synonym). PostgreSQL: public.x_peek(x_parents), which
                    "SELECT p.x_peek FROM x_parents p" calls.
    public.x_guests PostgreSQL only, and not in the third schema but in "public": 3 people (guest_id,
                    first_name, room). FROM public.x_guests must pass the guard and be masked by its table.
    x_days          30 days of January 2025; the key is the day (a DATE)
    x_shifts        60 rows, a declared foreign key to x_days: every day exists
    x_bookings      50 rows, a foreign key to x_days that was never validated: 5 have no day, and 9 are on
                    3 days that do not exist
    x_slots         10 moments (08:30 to 17:30 on 1 March 2025); the key is the moment (MOMENT)
    x_visits        46 rows, a foreign key to x_slots that was never validated: 4 at 19:30 and 2 at
                    midnight have no slot
    """
    days = [(date(2025, 1, d), f'day {d}') for d in range(1, 31)]
    shifts = [(i, date(2025, 1, 1 + i % 30)) for i in range(1, 61)]
    booked = ([date(2025, 1, 1 + i % 30) for i in range(36)] + [None] * 5 + [date(2025, 2, 1)] * 4 + [date(2025, 2, 2)] * 3
              + [date(2024, 12, 31)] * 2)
    slots = [(datetime(2025, 3, 1, hour, 30), f'room {hour}') for hour in range(8, 18)]
    visited = [datetime(2025, 3, 1, 8 + i % 10, 30) for i in range(40)] + [datetime(2025, 3, 1, 19, 30)] * 4 + [datetime(2025, 3, 1)] * 2
    parents = [(i, f'P{i:04d}', i % 10, f'parent {i}') for i in range(1, 201)]
    children = []
    for i in range(1, 1001):
        ref = None if i % 20 == 0 else 1 + i % 220
        children.append((i, ref, None if ref is None else f'P{ref:04d}', i % 10, f'9665{(i * 7919) % 100000000:08d}'))
    profiles = [(i, f'bio {i}') for i in range(1, 151)]
    pairs = [(a, b, f'{a}-{b}') for a in range(1, 11) for b in range(1, 11)]
    items = [(i, None if i % 25 == 0 else 1 + i % 11, 1 + i % 10, i % 7) for i in range(1, 501)]
    never = [(i, f'v{i % 13}') for i in range(1, 151)]
    docs = [(i, f'doc {i}', None if i % 3 == 0 else f'body of document {i} ' * 20, None if i % 3 else bytes([i]) * 64)
            for i in range(1, 31)]
    cdr = [(i, f'9665{(i * 7919) % 100000000:08d}', f'+9665{(i * 104729) % 100000000:08d}',
            f'42001{(i * 15485863) % 10000000000:010d}', 1_700_000_000 + i * 977, 10 + i % 600,
            None if i % 4 == 0 else f'011{(i * 7717) % 10000000:07d}') for i in range(1, 301)]
    stores = [(i, shop, city) for i, (shop, city) in enumerate(STORES, 1)]
    staff = [(i, person, ['Clerk', 'Cashier', 'Manager'][i % 3], 4000 + i * 1250.5, None if i == 12 else 1 + i % 6)
             for i, person in enumerate(STAFF, 1)]
    return {'x_parents': (['parent_id', 'code', 'grp', 'label'], parents),
            'x_children': (['child_id', 'parent_id', 'parent_code', 'grp', 'msisdn'], children),
            'x_profiles': (['parent_id', 'bio'], profiles),
            'x_pairs': (['a', 'b', 'note'], pairs),
            'x_pair_items': (['item_id', 'a', 'b', 'qty'], items),
            'x_never_counted': (['id', 'v'], never),
            'x_docs': (['doc_id', 'title', 'body', 'photo'], docs),
            'x_cdr': (['call_id', 'a_number', 'b_number', 'imsi', 'call_ts', 'duration_s', 'contact'], cdr),
            'x_stores': (['store_id', 'name', 'city'], stores),
            'x_staff': (['staff_id', 'first_name', 'job_title', 'salary', 'store_id'], staff),
            'x_days': (['the_day', 'label'], days),
            'x_shifts': (['shift_id', 'the_day'], shifts),
            'x_bookings': (['booking_id', 'the_day'], [(i, day) for i, day in enumerate(booked, 1)]),
            'x_slots': (['starts_at', 'room'], slots),
            'x_visits': (['visit_id', 'starts_at'], [(i, moment) for i, moment in enumerate(visited, 1)])}


def extras_ready(engine):
    try:
        eng = admin_engine(engine)
        try:
            with eng.connect() as conn:
                return conn.execute(text(f'SELECT version FROM {extra_schema(engine)}.x_meta')).scalar() == EXTRAS_VERSION
        finally:
            eng.dispose()
    except Exception:
        return False


def ensure_extras(engine, force=False):
    """Create the third schema if it is not there in this version. Returns True if it was (re)built."""
    if not force and extras_ready(engine):
        return False
    spec = ENGINES[engine]
    x, reader = extra_schema(engine), spec['reader']
    integer, text20 = column_type('INTEGER', engine), column_type('VARCHAR(20)', engine)
    eng = admin_engine(engine, 'mysql' if engine == 'mysql' else None)
    with eng.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
        # 1. an empty schema
        if engine == 'postgresql':
            _run(conn, f'DROP SCHEMA IF EXISTS {x} CASCADE')
            _run(conn, f'CREATE SCHEMA {x}')
        elif engine == 'mysql':
            _run(conn, f'DROP DATABASE IF EXISTS {x}')
            _run(conn, f'CREATE DATABASE {x} CHARACTER SET utf8mb4')
            _run(conn, 'SET GLOBAL log_bin_trust_function_creators = 1')
        elif engine == 'mssql':
            rows = conn.execute(text("SELECT o.type, o.name FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
                                     "WHERE s.name = :s AND o.type IN ('F', 'U', 'SO', 'FN') ORDER BY CASE o.type WHEN 'F' "
                                     "THEN 0 ELSE 1 END"), {'s': x}).fetchall()
            for kind, name in rows:
                kind = kind.strip()
                if kind == 'F':
                    parent = conn.execute(text('SELECT OBJECT_NAME(parent_object_id) FROM sys.foreign_keys WHERE name = :n'),
                                          {'n': name}).scalar()
                    _run(conn, f'ALTER TABLE {x}.{parent} DROP CONSTRAINT {name}')
                else:
                    _run(conn, f'DROP {dict(U="TABLE", SO="SEQUENCE", FN="FUNCTION")[kind]} {x}.{name}')
            if conn.execute(text('SELECT 1 FROM sys.schemas WHERE name = :s'), {'s': x}).first() is None:
                _run(conn, f'CREATE SCHEMA {x}')
        elif engine == 'oracle':
            if conn.execute(text('SELECT 1 FROM all_users WHERE username = :u'), {'u': x}).first():
                _run(conn, f'DROP USER {x} CASCADE')
            _run(conn, f'CREATE USER {x} IDENTIFIED BY "{ADMIN_PASSWORD}" ACCOUNT LOCK QUOTA UNLIMITED ON USERS')

        # 2. the tables
        not_valid = {'oracle': ' ENABLE NOVALIDATE', 'postgresql': ' NOT VALID'}.get(engine, '')
        ddl = [
            f'CREATE TABLE {x}.x_parents (parent_id {integer} NOT NULL, code {column_type("VARCHAR(10)", engine)} NOT NULL, '
            f'grp {integer} NOT NULL, label {text20}, CONSTRAINT pk_x_parents PRIMARY KEY (parent_id), '
            f'CONSTRAINT uq_x_parents_code UNIQUE (code))',
            f'CREATE TABLE {x}.x_children (child_id {integer} NOT NULL, parent_id {integer}, '
            f'parent_code {column_type("VARCHAR(10)", engine)}, grp {integer}, msisdn {column_type("VARCHAR(12)", engine)}, '
            f'CONSTRAINT pk_x_children PRIMARY KEY (child_id))',
            f'CREATE TABLE {x}.x_profiles (parent_id {integer} NOT NULL, bio {text20}, '
            f'CONSTRAINT pk_x_profiles PRIMARY KEY (parent_id), '
            f'CONSTRAINT fk_x_profiles_parent FOREIGN KEY (parent_id) REFERENCES {x}.x_parents (parent_id))',
            f'CREATE TABLE {x}.x_pairs (a {integer} NOT NULL, b {integer} NOT NULL, note {text20}, '
            f'CONSTRAINT pk_x_pairs PRIMARY KEY (a, b))',
            f'CREATE TABLE {x}.x_pair_items (item_id {integer} NOT NULL, a {integer}, b {integer}, qty {integer}, '
            f'CONSTRAINT pk_x_pair_items PRIMARY KEY (item_id))',
            f'CREATE TABLE {x}.x_never_counted (id {integer} NOT NULL, v {text20}, CONSTRAINT pk_x_never_counted PRIMARY KEY (id))'
            + (' WITH (autovacuum_enabled = false)' if engine == 'postgresql' else ''),
            f'CREATE TABLE {x}.x_docs (doc_id {integer} NOT NULL, title {text20}, body {LONG_TEXT[engine]}, photo {BINARY[engine]}, '
            f'CONSTRAINT pk_x_docs PRIMARY KEY (doc_id))',
            f'CREATE TABLE {x}.x_cdr (call_id {integer} NOT NULL, a_number {text20}, b_number {text20}, imsi {text20}, '
            f'call_ts {BIG_NUMBER[engine]}, duration_s {integer}, contact {text20}, CONSTRAINT pk_x_cdr PRIMARY KEY (call_id))',
            f'CREATE TABLE {x}.x_stores (store_id {integer} NOT NULL, name {column_type("VARCHAR(40)", engine)} NOT NULL, '
            f'city {text20}, CONSTRAINT pk_x_stores PRIMARY KEY (store_id))',
            f'CREATE TABLE {x}.x_staff (staff_id {integer} NOT NULL, first_name {text20} NOT NULL, job_title {text20}, '
            f'salary {column_type("DECIMAL(10,2)", engine)}, store_id {integer}, CONSTRAINT pk_x_staff PRIMARY KEY (staff_id))',
            f'CREATE TABLE {x}.x_days (the_day DATE NOT NULL, label {text20}, CONSTRAINT pk_x_days PRIMARY KEY (the_day))',
            f'CREATE TABLE {x}.x_shifts (shift_id {integer} NOT NULL, the_day DATE, CONSTRAINT pk_x_shifts PRIMARY KEY (shift_id), '
            f'CONSTRAINT fk_x_shifts_day FOREIGN KEY (the_day) REFERENCES {x}.x_days (the_day))',
            f'CREATE TABLE {x}.x_bookings (booking_id {integer} NOT NULL, the_day DATE, '
            f'CONSTRAINT pk_x_bookings PRIMARY KEY (booking_id))',
            f'CREATE TABLE {x}.x_slots (starts_at {MOMENT[engine]} NOT NULL, room {text20}, '
            f'CONSTRAINT pk_x_slots PRIMARY KEY (starts_at))',
            f'CREATE TABLE {x}.x_visits (visit_id {integer} NOT NULL, starts_at {MOMENT[engine]}, '
            f'CONSTRAINT pk_x_visits PRIMARY KEY (visit_id))',
            f'CREATE TABLE {x}.x_audit (id {integer}, note {column_type("VARCHAR(40)", engine)})',
            f'CREATE TABLE {x}.{CALL_LOG} (fn {column_type("VARCHAR(40)", engine)})',
            f'CREATE TABLE {x}.x_meta (version {integer})',
        ]
        if engine == 'oracle':
            ddl.append(f'CREATE TABLE {x}.x_old_docs (doc_id {integer} NOT NULL, old_text LONG, '
                       f'CONSTRAINT pk_x_old_docs PRIMARY KEY (doc_id))')
        if engine == 'postgresql':
            ddl += [f'CREATE TABLE {x}.x_events_p (event_id INTEGER NOT NULL, day DATE NOT NULL, kind VARCHAR(10)) '
                    f'PARTITION BY RANGE (day)',
                    f"CREATE TABLE {x}.x_events_p_2025_01 PARTITION OF {x}.x_events_p FOR VALUES FROM ('2025-01-01') TO ('2025-02-01') "
                    f'PARTITION BY HASH (event_id)',
                    f'CREATE TABLE {x}.x_events_p_2025_01_a PARTITION OF {x}.x_events_p_2025_01 FOR VALUES WITH (MODULUS 2, REMAINDER 0)',
                    f'CREATE TABLE {x}.x_events_p_2025_01_b PARTITION OF {x}.x_events_p_2025_01 FOR VALUES WITH (MODULUS 2, REMAINDER 1)',
                    f"CREATE TABLE {x}.x_events_p_2025_02 PARTITION OF {x}.x_events_p FOR VALUES FROM ('2025-02-01') TO ('2025-03-01')",
                    f"CREATE TABLE {x}.x_events_p_2025_03 PARTITION OF {x}.x_events_p FOR VALUES FROM ('2025-03-01') TO ('2025-04-01')"]
        for statement in ddl:
            _run(conn, statement)

        # 3. the rows
        if engine == 'mysql':
            _run(conn, 'SET FOREIGN_KEY_CHECKS = 0')
        for table, (columns, rows) in extra_rows().items():
            insert = text(f'INSERT INTO {x}.{table} ({", ".join(columns)}) VALUES ({", ".join(":" + c for c in columns)})')
            if 'photo' in columns:                          # bytes must travel as binary, not as text
                insert = insert.bindparams(bindparam('photo', type_=LargeBinary()))
            conn.execute(insert, [dict(zip(columns, row)) for row in rows])
        if engine == 'oracle':
            _run(conn, f"INSERT INTO {x}.x_old_docs (doc_id, old_text) VALUES (1, 'an old LONG value')")
            _run(conn, f'INSERT INTO {x}.x_old_docs (doc_id) VALUES (2)')
        if engine == 'postgresql':
            _run(conn, f"INSERT INTO {x}.x_events_p SELECT g, DATE '2025-01-01' + (g % 90), 'k' || (g % 5) FROM generate_series(1, 3000) g")
        _run(conn, f'INSERT INTO {x}.x_meta (version) VALUES ({EXTRAS_VERSION})')

        # 4. foreign keys that are declared but were never checked against the rows: one of two columns, and
        #    the two on dates whose children have orphans
        unchecked = [('x_pair_items', f'CONSTRAINT fk_x_pair_items_pair FOREIGN KEY (a, b) REFERENCES {x}.x_pairs (a, b)'),
                     ('x_bookings', f'CONSTRAINT fk_x_bookings_day FOREIGN KEY (the_day) REFERENCES {x}.x_days (the_day)'),
                     ('x_visits', f'CONSTRAINT fk_x_visits_slot FOREIGN KEY (starts_at) REFERENCES {x}.x_slots (starts_at)')]
        for table, key in unchecked:
            if engine == 'mssql':
                _run(conn, f'ALTER TABLE {x}.{table} WITH NOCHECK ADD {key}')
            else:
                _run(conn, f'ALTER TABLE {x}.{table} ADD {key}{not_valid}')
        if engine == 'mysql':
            _run(conn, 'SET FOREIGN_KEY_CHECKS = 1')

        # 5. a sequence, and a function that writes (the SQL runner must never call either)
        if engine == 'oracle':
            _run(conn, f'CREATE SEQUENCE {x}.x_seq START WITH 1')
            _run(conn, f'CREATE OR REPLACE FUNCTION {x}.x_touch RETURN NUMBER IS PRAGMA AUTONOMOUS_TRANSACTION; '
                       f"BEGIN INSERT INTO {x}.x_audit (id, note) VALUES (1, 'written by x_touch'); COMMIT; RETURN 1; END;")
            _run(conn, f'CREATE OR REPLACE PUBLIC SYNONYM x_touch FOR {x}.x_touch')
            # Functions without arguments that the READ-ONLY account may call, each with a public synonym:
            # Oracle calls them without brackets. Each call leaves a row in x_calls, whatever the transaction.
            logs = (f'PRAGMA AUTONOMOUS_TRANSACTION; BEGIN INSERT INTO {x}.{CALL_LOG} (fn) VALUES (\'%s\'); '
                    f'COMMIT; RETURN 1; END;')
            _run(conn, f'CREATE OR REPLACE FUNCTION {x}.x_ping RETURN NUMBER IS ' + logs % 'X_PING')
            _run(conn, f'CREATE OR REPLACE FUNCTION {x}.status RETURN NUMBER IS ' + logs % 'STATUS')     # named like a column
            _run(conn, f'CREATE OR REPLACE FUNCTION {x}.one RETURN NUMBER IS ' + logs % 'ONE')           # ... like a word of SQL
            _run(conn, f'CREATE OR REPLACE PACKAGE {x}.x_pkg AS FUNCTION ping RETURN NUMBER; END;')
            _run(conn, f'CREATE OR REPLACE PACKAGE BODY {x}.x_pkg AS FUNCTION ping RETURN NUMBER IS '
                       + logs % 'X_PKG.PING' + ' END;')
            for name in ('x_ping', 'status', 'one', 'x_pkg'):
                _run(conn, f'GRANT EXECUTE ON {x}.{name} TO {reader}')
                _run(conn, f'CREATE OR REPLACE PUBLIC SYNONYM {name} FOR {x}.{name}')
        elif engine == 'postgresql':
            _run(conn, f'CREATE SEQUENCE {x}.x_seq START 1')
            _run(conn, f'CREATE OR REPLACE FUNCTION {x}.x_touch() RETURNS integer LANGUAGE plpgsql AS $$ '
                       f"BEGIN INSERT INTO {x}.x_audit (id, note) VALUES (1, 'written by x_touch'); RETURN 1; END $$")
            # A function of the table's row type, where every account finds it (public): PostgreSQL calls
            # it for p.x_peek and (p).x_peek. SECURITY DEFINER: it writes whoever calls it.
            _run(conn, f'CREATE OR REPLACE FUNCTION public.x_peek({x}.x_parents) RETURNS integer LANGUAGE plpgsql '
                       f"SECURITY DEFINER AS $$ BEGIN INSERT INTO {x}.{CALL_LOG} (fn) VALUES ('x_peek'); RETURN 1; END $$")
            # a table in the schema "public" (see PUBLIC_TABLE)
            _run(conn, f'DROP TABLE IF EXISTS public.{PUBLIC_TABLE}')
            _run(conn, f'CREATE TABLE public.{PUBLIC_TABLE} (guest_id INTEGER NOT NULL, first_name VARCHAR(20) NOT NULL, '
                       f'room VARCHAR(10), CONSTRAINT pk_{PUBLIC_TABLE} PRIMARY KEY (guest_id))')
            conn.execute(text(f'INSERT INTO public.{PUBLIC_TABLE} (guest_id, first_name, room) VALUES (:i, :n, :r)'),
                         [{'i': i, 'n': name, 'r': room} for i, (name, room) in enumerate(GUESTS, 1)])
            _run(conn, f'GRANT SELECT ON public.{PUBLIC_TABLE} TO {reader}')
            _run(conn, f'ANALYZE public.{PUBLIC_TABLE}')
        elif engine == 'mysql':
            _run(conn, f'CREATE FUNCTION {x}.x_touch() RETURNS INT MODIFIES SQL DATA '
                       f"BEGIN INSERT INTO {x}.x_audit (id, note) VALUES (1, 'written by x_touch'); RETURN 1; END")
        elif engine == 'mssql':
            _run(conn, f'CREATE SEQUENCE {x}.x_seq AS INT START WITH 1')

        # 6. what the read-only account may see, and fresh statistics - except for x_never_counted
        analysed = [t for t in EXTRA_TABLES if t != 'x_never_counted']
        if engine == 'postgresql':
            _run(conn, f'GRANT USAGE ON SCHEMA {x} TO {reader}')
            _run(conn, f'GRANT SELECT ON ALL TABLES IN SCHEMA {x} TO {reader}')
            for t in analysed + ['x_events_p_2025_01_a', 'x_events_p_2025_01_b', 'x_events_p_2025_02', 'x_events_p_2025_03']:
                _run(conn, f'ANALYZE {x}.{t}')                   # the partitions, never the partitioned table itself
        elif engine == 'mysql':
            _run(conn, f"GRANT SELECT, SHOW VIEW ON {x}.* TO '{reader}'@'%'")
            for t in analysed:
                conn.execute(text(f'ANALYZE TABLE {x}.{t}')).fetchall()
        elif engine == 'mssql':
            _run(conn, f'GRANT SELECT ON SCHEMA::{x} TO {reader}')
        elif engine == 'oracle':
            for t in EXTRA_TABLES + ['x_old_docs']:
                _run(conn, f'GRANT SELECT ON {x}.{t} TO {reader}')
            for t in analysed + ['x_old_docs']:
                _run(conn, 'BEGIN DBMS_STATS.GATHER_TABLE_STATS(:s, :t); END;', s=x, t=t.upper())
    eng.dispose()
    return True


def extra_state(engine):
    """Everything in the third schema that a write would change: row counts, the rows of x_parents
    and x_audit, the sequence's position, the list of tables. Read with the admin account."""
    x = extra_schema(engine)
    eng = admin_engine(engine)
    try:
        with eng.connect() as conn:
            state = {t: conn.execute(text(f'SELECT COUNT(*) FROM {x}.{t}')).scalar() for t in EXTRA_TABLES + [CALL_LOG]}
            state['parents'] = [tuple(r) for r in conn.execute(text(f'SELECT parent_id, code, grp, label FROM {x}.x_parents '
                                                                    f'ORDER BY parent_id')).fetchall()]
            if engine == 'oracle':
                state['sequence'] = conn.execute(text('SELECT last_number FROM all_sequences WHERE sequence_owner = :o '
                                                      "AND sequence_name = 'X_SEQ'"), {'o': x}).scalar()
                tables = text('SELECT table_name FROM all_tables WHERE owner = :s')
            elif engine == 'postgresql':
                state['sequence'] = conn.execute(text(f'SELECT last_value, is_called FROM {x}.x_seq')).fetchall()[0]._tuple()
                tables = text('SELECT tablename FROM pg_tables WHERE schemaname = :s')
            elif engine == 'mssql':
                state['sequence'] = str(conn.execute(text("SELECT current_value FROM sys.sequences WHERE name = 'x_seq'")).scalar())
                tables = text('SELECT t.name FROM sys.tables t JOIN sys.schemas c ON c.schema_id = t.schema_id WHERE c.name = :s')
            else:
                tables = text('SELECT table_name FROM information_schema.tables WHERE table_schema = :s')
            state['tables'] = sorted(str(r[0]).lower() for r in conn.execute(tables, {'s': x}).fetchall())
            return state
    finally:
        eng.dispose()


def open_locks(engine):
    """Locks other sessions hold on the test schemas right now, and transactions left open, as the
    server itself reports them. An idle, read-only app leaves none."""
    spec = ENGINES[engine]
    eng = admin_engine(engine, 'mysql' if engine == 'mysql' else None)
    try:
        with eng.connect() as conn:
            if engine == 'postgresql':
                rows = conn.execute(text(
                    "SELECT l.mode, c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname IN ('telecom', 'other', 'dx_extra') "
                    "AND l.pid <> pg_backend_pid()")).fetchall()
            elif engine == 'mysql':
                rows = conn.execute(text(
                    "SELECT lock_mode, object_name FROM performance_schema.data_locks WHERE object_schema IN "
                    "('telecom', 'other', 'dx_extra') UNION ALL SELECT 'open transaction', CAST(trx_id AS CHAR) "
                    "FROM information_schema.innodb_trx")).fetchall()
            elif engine == 'mssql':
                rows = conn.execute(text(
                    "SELECT l.request_mode, l.resource_type FROM sys.dm_tran_locks l "
                    "WHERE l.resource_database_id = DB_ID('telecom') AND l.request_session_id <> @@SPID "
                    "AND l.resource_type <> 'DATABASE'")).fetchall()
            else:
                rows = conn.execute(text(
                    "SELECT o.locked_mode, b.object_name FROM v$locked_object o JOIN all_objects b ON b.object_id = o.object_id "
                    "WHERE b.owner IN ('TELECOM', 'OTHER', 'DX_EXTRA')")).fetchall()
            return [tuple(r) for r in rows]
    finally:
        eng.dispose()


def table_counts(engine):
    """{table: rows} read with the admin account - to check that the app left the data unchanged."""
    spec = ENGINES[engine]
    eng = admin_engine(engine)
    try:
        with eng.connect() as conn:
            return {t: conn.execute(text(f'SELECT COUNT(*) FROM {spec["schema"]}.{t}')).scalar()
                    for t in demo.TABLES + [BIG_TABLE]}
    finally:
        eng.dispose()


# ------------------------------------------- what the server itself received ----

# Statements the drivers and SQLAlchemy send on their own when a connection opens or closes.
# They never pass the app's guard (raw cursors), so the test lists them exactly, per engine.
DRIVER_STATEMENTS = {
    'oracle': [],
    'mssql': [r'(SET [A-Z_]+ (ON|OFF|\d+);?\s*)+', r'use \[telecom\]', r'BEGIN TRAN', r'ROLLBACK TRAN'],
    'postgresql': [r'BEGIN', r'ROLLBACK'],
    'mysql': [r'SET NAMES \w+', r'SET AUTOCOMMIT = 0', r'ROLLBACK', r'<Quit>.*'],
}


def _tidy(statement):
    return ' '.join(str(statement).split()).rstrip(';').strip()


@contextmanager
def server_statements(engine, user=None):
    """Collect every statement the server logs for one account while the block runs: the read-only
    account, or `user` (the hostile battery of the SQL tab runs as the admin account).

    This is the server's own record (general log, statement log, cursor cache, extended events),
    not the app's: it also shows what the driver sends without passing the app's guard.
    Yields a list that is filled in when the block ends.
    """
    spec = ENGINES[engine]
    seen = []
    account = user or spec['reader']

    def admin(database=None):
        return admin_engine(engine, database).connect().execution_options(isolation_level='AUTOCOMMIT')

    if engine == 'mysql':
        with admin('mysql') as c:
            _run(c, "SET GLOBAL log_output = 'TABLE'")
            _run(c, 'TRUNCATE TABLE mysql.general_log')
            _run(c, 'SET GLOBAL general_log = 1')
        try:
            yield seen
        finally:
            time.sleep(1)
            with admin('mysql') as c:
                _run(c, 'SET GLOBAL general_log = 0')
                rows = c.execute(text('SELECT command_type, CONVERT(argument USING utf8mb4) FROM mysql.general_log '
                                      'WHERE user_host LIKE :u ORDER BY event_time'), {'u': account + '%'}).fetchall()
            seen += [_tidy(a) if kind == 'Query' else f'<{kind}> {_tidy(a)}'.strip() for kind, a in rows
                     if kind != 'Connect']
    elif engine == 'postgresql':
        with admin() as c:
            _run(c, "ALTER SYSTEM SET log_statement = 'all'")
            _run(c, "ALTER SYSTEM SET log_line_prefix = '%u|'")
            _run(c, 'SELECT pg_reload_conf()')
        time.sleep(1.5)
        started = time.time()
        try:
            yield seen
        finally:
            time.sleep(1.5)
            with admin() as c:
                _run(c, 'ALTER SYSTEM RESET log_statement')
                _run(c, 'ALTER SYSTEM RESET log_line_prefix')
                _run(c, 'SELECT pg_reload_conf()')
            logs = subprocess.run(['docker', 'logs', '--since', f'{int(time.time() - started) + 5}s', spec['container']],
                                  capture_output=True, text=True, encoding='utf-8', errors='replace')
            current = None
            for line in (logs.stdout + logs.stderr).splitlines():
                match = re.match(re.escape(account) + r'\|(LOG|ERROR):\s+(statement|execute [^:]*):\s?(.*)', line)
                if match:
                    current = [match.group(3)]
                    seen.append(current)
                elif current is not None and line.startswith('\t'):
                    current.append(line)
                else:
                    current = None
            seen[:] = [_tidy(' '.join(parts)) for parts in seen]
    elif engine == 'oracle':
        with admin() as c:
            _run(c, 'ALTER SYSTEM FLUSH SHARED_POOL')
        try:
            yield seen
        finally:
            with admin() as c:
                rows = c.execute(text('SELECT sql_fulltext FROM v$sql WHERE parsing_schema_name = :u '
                                      'ORDER BY first_load_time, sql_id'), {'u': account.upper()}).fetchall()
            seen += [_tidy(r[0]) for r in rows]
    elif engine == 'mssql':
        with admin('master') as c:
            _run(c, "IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = 'dx_audit') "
                    "DROP EVENT SESSION dx_audit ON SERVER")
            _run(c, "CREATE EVENT SESSION dx_audit ON SERVER "
                    "ADD EVENT sqlserver.sql_batch_completed(ACTION(sqlserver.username) "
                    f"WHERE sqlserver.username = N'{account}'), "
                    "ADD EVENT sqlserver.rpc_completed(ACTION(sqlserver.username) "
                    f"WHERE sqlserver.username = N'{account}') "
                    "ADD TARGET package0.ring_buffer(SET max_memory = 32768) WITH (MAX_DISPATCH_LATENCY = 1 SECONDS)")
            _run(c, 'ALTER EVENT SESSION dx_audit ON SERVER STATE = START')
        try:
            yield seen
        finally:
            time.sleep(3)
            with admin('master') as c:
                xml = c.execute(text(
                    'SELECT CAST(t.target_data AS NVARCHAR(MAX)) FROM sys.dm_xe_sessions s '
                    'JOIN sys.dm_xe_session_targets t ON t.event_session_address = s.address '
                    "WHERE s.name = 'dx_audit'")).scalar()
                _run(c, 'DROP EVENT SESSION dx_audit ON SERVER')
            pattern = r'<data name="(?:batch_text|statement)">\s*<type[^>]*>(?:\s*</type>)?\s*<value>(.*?)</value>'
            for match in re.finditer(pattern, xml or '', re.S):
                value = match.group(1)
                if value.startswith('<![CDATA['):
                    value = value[9:-3]
                seen.append(_tidy(html.unescape(value)))


def is_driver_statement(engine, statement):
    return any(re.fullmatch(p, statement, re.I) for p in DRIVER_STATEMENTS[engine])


def main(argv):
    command = argv[0] if argv else 'status'
    engines = [e for e in argv[1:] if e in ENGINES] or list(ENGINES)
    if command == 'status':
        for e in ENGINES:
            state = 'ready (seeded)' if reachable(e) else 'up, not seeded' if reachable(e, seeded=False) else 'not running'
            print(f'{e:<11} 127.0.0.1:{ENGINES[e]["port"]:<6} {state}')
    elif command == 'wait':
        for e in engines:
            print(f'{e}: accepting logins after {wait(e)} s')
    elif command == 'seed':
        for e in engines:
            wait(e)
            seed(e)
    elif command == 'extras':
        for e in engines:
            wait(e)
            print(f'{e}: dx_extra', 'created' if ensure_extras(e, force=True) else 'unchanged')
    elif command == 'config':                               # for live_db.ps1: what to run in Docker
        import json
        print(json.dumps({e: {k: ENGINES[e][k] for k in ('container', 'image', 'port', 'container_port', 'env')}
                          for e in ENGINES}))
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
