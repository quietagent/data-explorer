"""Live database source: read a database's structure with SQLAlchemy's inspector.

Read-only rules, enforced here for every connection the app opens:

  * every statement the app sends goes through check_read_only_sql() first;
    anything that is not a SELECT / catalog read raises ReadOnlyViolation
  * the app never commits (a commit raises) and rolls back after every use
  * the session is put in read-only mode where the engine has one (PostgreSQL,
    Oracle, MySQL, SQLite). If the engine refuses that setting the connection
    is refused (ReadOnlyRefused) - it is never used without it
  * every statement has a timeout
  * connections are not pooled: open, read, roll back, close
  * the password is only ever held in memory
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import time
import warnings as _warnings
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import URL
from sqlalchemy.engine.reflection import ObjectKind
from sqlalchemy.pool import NullPool
from sqlalchemy.types import NullType

from .. import config
from ..model import DECLARED, Origin, SchemaModel, finalize_model
from ..security import ReadOnlyViolation, check_read_only_sql, sanitize_error
from . import SourceError

ENGINES = {
    'oracle': {'label': 'Oracle', 'driver': 'oracle+oracledb', 'module': 'oracledb', 'pip': 'oracledb',
               'port': 1521, 'database_label': 'Service name',
               'hint': 'Thin mode: no Oracle client needed. For a SID write sid:ORCL.'},
    'mssql': {'label': 'SQL Server', 'driver': 'mssql+pymssql', 'module': 'pymssql', 'pip': 'pymssql',
              'port': 1433, 'database_label': 'Database',
              'hint': 'SQL login, or DOMAIN\\user with its password.'},
    'postgresql': {'label': 'PostgreSQL', 'driver': 'postgresql+psycopg', 'module': 'psycopg',
                   'pip': 'psycopg[binary]', 'port': 5432, 'database_label': 'Database', 'hint': ''},
    'mysql': {'label': 'MySQL / MariaDB', 'driver': 'mysql+pymysql', 'module': 'pymysql', 'pip': 'PyMySQL',
              'port': 3306, 'database_label': 'Database', 'hint': ''},
    'sqlite': {'label': 'SQLite', 'driver': 'sqlite', 'module': 'sqlite3', 'pip': '',
               'port': None, 'database_label': 'Database file',
               'hint': 'Full path of the .db / .sqlite file on this PC.'},
}

# Session settings applied when a connection opens, and at the start of each transaction.
# Each one is matched exactly by security.SETUP_STATEMENTS.
# The read-only ones are required: if the engine refuses one, the connection is refused.
# The timeout ones are best effort (the "protections" list of Test connection shows what is active).
SESSION_SETUP = {
    'sqlite': ['PRAGMA query_only = ON'],
    'mysql': ['SET SESSION TRANSACTION READ ONLY'],
}
TRANSACTION_SETUP = {
    'postgresql': ['SET TRANSACTION READ ONLY', 'SET LOCAL statement_timeout = {ms}'],
    'oracle': ['SET TRANSACTION READ ONLY'],
}


# Oracle calls a function that takes no argument without brackets, so the SQL tab's guard has to know
# every name that can be called: one read of the catalog when a source is added or refreshed. What the
# account may call is what ALL_OBJECTS shows it; a synonym counts when it leads to one of those (the
# rows are joined here, not on the server), and a synonym over a database link always does.
ORACLE_CALLABLE_SQL = (
    "SELECT 'O' AS kind, owner, object_name AS name, NULL AS to_owner, NULL AS to_name, NULL AS link "
    "FROM all_objects WHERE object_type IN ('FUNCTION', 'PROCEDURE', 'PACKAGE', 'TYPE', 'OPERATOR') "
    "UNION ALL SELECT 'Y', owner, synonym_name, table_owner, table_name, db_link FROM all_synonyms "
    "UNION ALL SELECT 'U', NULL, username, NULL, NULL, NULL FROM all_users")


def callable_names(rows):
    """{'callable': [...], 'schemas': [...]} from the rows of ORACLE_CALLABLE_SQL."""
    reached = {(owner, name) for kind, owner, name, *_ in rows if kind == 'O'}
    names = {name for kind, _, name, *_ in rows if kind == 'O'}
    synonyms = [row for row in rows if row[0] == 'Y']
    grew = True
    while grew:                                     # a synonym for a synonym for a function ...
        grew = False
        for _, owner, name, to_owner, to_name, link in synonyms:
            if (owner, name) not in reached and (link is not None or (to_owner, to_name) in reached):
                reached.add((owner, name))
                names.add(name)
                grew = True
    return {'callable': sorted(str(n) for n in names if n),
            'schemas': sorted(str(name) for kind, _, name, *_ in rows if kind == 'U' and name)}


class ReadOnlyRefused(SourceError):
    """The engine refused read-only mode, so the connection was closed without being used."""


def _is_read_only_setting(sql):
    return 'READ ONLY' in sql.upper() or sql.upper().startswith('PRAGMA QUERY_ONLY')


def driver_installed(engine):
    try:
        return importlib.util.find_spec(ENGINES[engine]['module']) is not None
    except (ImportError, ValueError):
        return False


def engines_info():
    return [{'id': k, 'label': v['label'], 'port': v['port'], 'database_label': v['database_label'],
             'hint': v['hint'], 'driver': v['module'], 'installed': driver_installed(k)}
            for k, v in ENGINES.items()]


@dataclass
class ConnectionProfile:
    """Everything needed to connect except the password, which is never part of a profile."""
    engine: str
    host: str = ''
    port: int | None = None
    database: str = ''
    username: str = ''
    schema_filter: str = ''
    include_views: bool = False

    @classmethod
    def from_dict(cls, d):
        engine = str(d.get('engine') or '').strip().lower()
        if engine not in ENGINES:
            raise SourceError('Choose a database engine.')
        port = d.get('port')
        try:
            port = int(port) if port not in (None, '') else None
        except (TypeError, ValueError):
            raise SourceError('Port must be a number.') from None
        text = lambda k: str(d.get(k) or '').strip()
        return cls(engine=engine, host=text('host'), port=port, database=text('database'),
                   username=text('username'), schema_filter=text('schema_filter'),
                   include_views=bool(d.get('include_views')))

    def to_dict(self):
        return asdict(self)

    def schemas(self):
        return [s for s in re.split(r'[,;\s]+', self.schema_filter) if s]

    def describe(self):
        label = ENGINES[self.engine]['label']
        if self.engine == 'sqlite':
            return f'{label} · {Path(self.database).name}'
        where = self.host + (f':{self.port}' if self.port else '')
        return f'{label} · {where}/{self.database}' if self.database else f'{label} · {where}'


def build_url(profile, password):
    spec = ENGINES[profile.engine]
    database, query = profile.database or None, {}
    if not profile.host:
        raise SourceError('Host is required.')
    if profile.engine == 'oracle':
        if not database:
            raise SourceError('Service name is required.')
        if database.lower().startswith('sid:'):
            database = database[4:].strip()
        else:
            database, query = None, {'service_name': database}
    return URL.create(spec['driver'], username=profile.username or None, password=password or None,
                      host=profile.host, port=profile.port, database=database, query=query)


def connect_args(profile, timeout_s):
    e = profile.engine
    if e == 'oracle':
        return {'tcp_connect_timeout': config.CONNECT_TIMEOUT_S}
    if e == 'mssql':
        return {'login_timeout': config.CONNECT_TIMEOUT_S, 'timeout': timeout_s}
    if e == 'postgresql':
        return {'connect_timeout': config.CONNECT_TIMEOUT_S, 'application_name': 'data-explorer'}
    if e == 'mysql':
        return {'connect_timeout': config.CONNECT_TIMEOUT_S, 'read_timeout': timeout_s + 5,
                'write_timeout': timeout_s + 5}
    return {}


def type_name(t, dialect=None):
    """A column type as the engine itself writes it (NUMBER(10, 2), VARCHAR2(120 CHAR), DATETIME2 ...).

    Compiled with the connection's dialect: the generic form loses Oracle's precision and scale
    and PostgreSQL's time zone flag. Collation and character-set details are left out.
    """
    if t is None or isinstance(t, NullType):
        return None
    text = None
    if dialect is not None:
        try:
            text = str(t.compile(dialect=dialect))
        except Exception:
            text = None
    if text is None:
        try:
            text = str(t)
        except Exception:
            return type(t).__name__
    text = re.sub(r'\s+(COLLATE|CHARACTER SET)\s+("[^"]*"|\S+)', '', text, flags=re.I)
    return ' '.join(text.split()) or type(t).__name__


def auto_indexes(dialect):
    """Keyword arguments for the inspector's index reads. SQLite keeps a UNIQUE constraint as an index it makes
    itself (sqlite_autoindex_...), and SQLAlchemy leaves those out unless asked - while its own reading of
    the table's SQL for UNIQUE does not see "CODE VARCHAR(10) NOT NULL UNIQUE" (the brackets stop it). With
    them, every column a constraint makes unique is found; a partial index is still told by its WHERE."""
    return {'include_auto_indexes': True} if dialect.name == 'sqlite' else {}


class ReadOnlyDatabase:
    """One database, reachable only through guarded, read-only connections.

    Use `with db.connect() as conn:` for everything; it applies the read-only
    session settings and always rolls back. `db.statements` lists every
    statement this object sent, for audit.
    """

    def __init__(self, profile, password=None, timeout_s=None):
        self.profile = profile
        self.timeout_s = int(timeout_s or config.STATEMENT_TIMEOUT_S)
        self.statements = []            # every statement this object sent, for audit
        self.blocked = []               # every statement the guard refused (never sent)
        self._approved = set()          # statements the strict guard of the SQL tab has passed
        self.protections = []
        self._password = password or ''
        self._deadline = None
        spec = ENGINES[profile.engine]
        if not driver_installed(profile.engine):
            raise SourceError(f"The {spec['label']} driver is not installed. "
                              f"Run:  pip install {spec['pip']}")
        if profile.engine == 'sqlite':
            path = Path(profile.database).expanduser()
            if not profile.database or not path.is_file():
                raise SourceError(f'SQLite file not found: {profile.database or "(no path)"}')
            uri = path.resolve().as_uri() + '?mode=ro'
            self.engine = create_engine(
                'sqlite://', poolclass=NullPool,
                creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False))
        else:
            # psycopg would look up the hstore type inside a SAVEPOINT on every new connection.
            extra = {'use_native_hstore': False} if profile.engine == 'postgresql' else {}
            self.engine = create_engine(build_url(profile, password), poolclass=NullPool,
                                        connect_args=connect_args(profile, self.timeout_s), **extra)
            if profile.engine == 'oracle':
                # SQLAlchemy reads the default isolation level by running a PL/SQL block that starts a
                # transaction (dbms_transaction.local_transaction_id). The app never changes the
                # isolation level, so it does not need to know it: nothing but queries is sent.
                self.engine.dialect.get_default_isolation_level = lambda dbapi_conn: 'READ COMMITTED'
        event.listen(self.engine, 'connect', self._on_connect)
        event.listen(self.engine, 'before_cursor_execute', self._before_execute)
        event.listen(self.engine, 'after_cursor_execute', self._after_execute)
        event.listen(self.engine, 'commit', self._on_commit)

    # ------------------------------------------------------------------ guards
    def approve(self, statement):
        """A statement typed in the SQL tab that security.check_read_only_sql(strict=True) has passed.
        It is sent as it is: the strict guard reads it the way this engine does (q'...' strings,
        array subscripts), which the plain guard for the app's own statements does not."""
        self._approved.add(statement)

    def _guard(self, statement):
        if statement in self._approved:
            return
        try:
            check_read_only_sql(statement, dialect=self.profile.engine)
        except ReadOnlyViolation:
            self.blocked.append(statement)
            raise

    def _before_execute(self, conn, cursor, statement, parameters, context, executemany):
        self._guard(statement)
        self.statements.append(statement)
        self._deadline = time.monotonic() + self.timeout_s

    def _after_execute(self, conn, cursor, statement, parameters, context, executemany):
        # SQLite produces its rows while they are fetched, after execute() has returned: there the
        # deadline stays until the connection is given back, so a slow fetch is stopped too.
        if self.profile.engine != 'sqlite':
            self._deadline = None

    def _on_commit(self, conn):
        raise ReadOnlyViolation('The app never commits on a database connection.')

    def _on_connect(self, dbapi_conn, record):
        e = self.profile.engine
        ms = self.timeout_s * 1000
        protections = []
        if e == 'sqlite':
            protections.append('file opened read-only')
            dbapi_conn.set_progress_handler(self._sqlite_tick, 10000)
            protections.append(f'{self.timeout_s} s statement timeout')
        for sql in SESSION_SETUP.get(e, ()):
            error = self._session_statement(dbapi_conn, sql)
            if error is None:
                protections.append('read-only session')
            elif _is_read_only_setting(sql):
                try:                                 # fail closed: this connection is never used
                    dbapi_conn.close()
                except Exception:
                    pass
                raise self._refused(sql, error)
        if e == 'mysql':
            if (self._session_statement(dbapi_conn, f'SET SESSION MAX_EXECUTION_TIME = {ms}') is None
                    or self._session_statement(dbapi_conn,
                                               f'SET SESSION max_statement_time = {self.timeout_s}') is None):
                protections.append(f'{self.timeout_s} s statement timeout')
        elif e == 'oracle':
            try:
                dbapi_conn.call_timeout = ms
                protections.append(f'{self.timeout_s} s statement timeout')
            except Exception:
                pass
        elif e == 'mssql':
            protections.append(f'{self.timeout_s} s statement timeout')
        self.protections = protections

    def _session_statement(self, dbapi_conn, sql):
        """Run one session setting on a new connection. Returns None, or the error if it failed."""
        self._guard(sql)                                           # must be one of the known settings
        self.statements.append(sql)
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(sql)
            return None
        except Exception as exc:
            return exc
        finally:
            cursor.close()

    def _refused(self, sql, exc):
        return ReadOnlyRefused(
            f"{ENGINES[self.profile.engine]['label']} refused read-only mode, so the connection was closed "
            f"without being used. ({sql} failed: {self.clean_error(exc)})")

    def _sqlite_tick(self):
        return 1 if self._deadline is not None and time.monotonic() > self._deadline else 0

    def _begin_read_only(self, conn, timeout=True):
        """Start the transaction in read-only mode (engines that have one).

        The read-only setting is required: if it fails, ReadOnlyRefused is raised and the
        caller closes the connection. The timeout setting is best effort.
        """
        for template in TRANSACTION_SETUP.get(self.profile.engine, ()):
            sql = template.format(ms=self.timeout_s * 1000)
            required = _is_read_only_setting(sql)
            if not required and not timeout:
                continue
            try:
                conn.exec_driver_sql(sql)
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if required:
                    raise self._refused(sql, exc) from None
                # The failed setting ended the transaction, and its read-only mode with it:
                # start again, this time without the optional setting.
                self._begin_read_only(conn, timeout=False)
                return
            note = 'read-only transaction' if required else f'{self.timeout_s} s statement timeout'
            if note not in self.protections:
                self.protections.append(note)

    # ------------------------------------------------------------- connecting
    @contextmanager
    def connect(self):
        """A guarded connection: read-only settings on entry, rollback and close on exit."""
        try:
            conn = self.engine.connect()
        except (ReadOnlyViolation, ReadOnlyRefused):
            raise
        except Exception as exc:
            raise SourceError(self.clean_error(exc)) from None
        try:
            self._begin_read_only(conn)          # raises ReadOnlyRefused: nothing else runs on it
            yield conn
        finally:
            self._deadline = None
            try:
                conn.rollback()
            finally:
                conn.close()

    def dispose(self):
        self.engine.dispose()

    def clean_error(self, exc, keep=()):
        """A short error message: never the password, and nothing quoted from the data.

        keep: names that may stay in quotes (the table and columns a statement was about, or the
        words of a statement the user typed). The connection's own host, database, user and
        schemas always may.
        """
        orig = getattr(exc, 'orig', None) or exc
        args = getattr(orig, 'args', ())
        if len(args) == 2 and isinstance(args[0], int) and isinstance(args[1], (str, bytes)):
            # MySQL and SQL Server drivers: (error number, message). Not the tuple's repr, which
            # would put the whole message in quotes.
            message = args[1].decode('utf-8', 'replace') if isinstance(args[1], bytes) else args[1]
            text = f'{args[0]}: {message}'
        else:
            text = str(orig)
        text = re.sub(r'\(Background on this error at: [^)]*\)', '', text)
        if self._password:
            text = text.replace(self._password, '***')
        p = self.profile
        known = [p.host, p.database, p.username, *p.schemas(), *keep]
        return sanitize_error(text, known) or type(exc).__name__

    # ----------------------------------------------------------------- reading
    def test(self):
        """Connect and report what is there. Raises SourceError if it cannot."""
        started = time.monotonic()
        try:
            with self.connect() as conn:
                insp = inspect(conn)
                show = self._namer(conn.dialect)
                version = '.'.join(str(x) for x in (conn.dialect.server_version_info or ()))
                default = insp.default_schema_name
                schemas = []
                if self.profile.engine != 'sqlite':
                    try:
                        schemas = [show(s) for s in insp.get_schema_names()]
                    except ReadOnlyViolation:
                        raise
                    except Exception:
                        conn.rollback()
                        self._begin_read_only(conn)
                tables = 0
                for schema in self.profile.schemas() or [None]:
                    tables += len(insp.get_table_names(schema=schema))
        except (SourceError, ReadOnlyViolation):
            raise
        except Exception as exc:
            raise SourceError(self.clean_error(exc)) from None
        return {'ok': True, 'engine': self.profile.engine, 'server_version': version or None,
                'default_schema': None if self.profile.engine == 'sqlite' else show(default) if default else None,
                'schemas': schemas, 'tables': tables, 'protections': list(self.protections),
                'elapsed_ms': int((time.monotonic() - started) * 1000)}

    @staticmethod
    def _namer(dialect):
        """Oracle stores names in upper case; show them that way instead of SQLAlchemy's lower case."""
        if getattr(dialect, 'requires_name_normalize', False):
            return lambda n: dialect.denormalize_name(n) if n else n
        return lambda n: n

    def read_schema(self, label='', source_id=None):
        """Read tables, columns, keys and foreign keys. Returns (SchemaModel, warnings)."""
        notes = []
        try:
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter('always')
                with self.connect() as conn:
                    model = self._read(conn, label, source_id, notes)
                    if self.profile.engine == 'oracle':
                        model.catalog = self.read_callable(conn)
                        if model.catalog.get('error'):
                            notes.append('The SQL tab cannot run queries on this source yet: the names of what the '
                                         f'database can call could not be read ({model.catalog["error"]}).')
                notes += [str(w.message) for w in caught if 'sqlalchemy' in (w.category.__module__ or '')]
        except (SourceError, ReadOnlyViolation):
            raise
        except Exception as exc:
            raise SourceError(self.clean_error(exc)) from None
        finalize_model(model, names=False)
        return model, notes[:50]

    def read_callable(self, conn):
        """Oracle: the names of everything this account can call, and of every schema - one read of the
        catalog, on the guarded connection, under the statement timeout. Never raises: when the read
        fails the answer is {'callable': None, 'error': why}, and the SQL tab refuses to run anything on
        the source (there is no weaker check to fall back to)."""
        started = time.monotonic()
        try:
            rows = [tuple(row) for row in conn.exec_driver_sql(ORACLE_CALLABLE_SQL).fetchall()]
            found = callable_names(rows)
            if not found['schemas']:
                raise SourceError('the catalog listed no schema at all')
            return {**found, 'error': None, 'read_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
        except Exception as exc:
            late = time.monotonic() - started >= self.timeout_s - 0.5
            why = f'no answer within {self.timeout_s} s' if late else self.clean_error(exc)
            return {'callable': None, 'schemas': [], 'error': why, 'read_at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    def _read(self, conn, label, source_id, notes):
        insp = inspect(conn)
        show = self._namer(conn.dialect)
        sqlite_db = self.profile.engine == 'sqlite'
        default = insp.default_schema_name
        kind = ObjectKind.ANY if self.profile.include_views else ObjectKind.TABLE
        model, loaded, foreign = SchemaModel(), {}, []
        model.default_schema = None if sqlite_db or not default else show(default)

        for schema in self.profile.schemas() or [None]:
            shown = None if sqlite_db else show(schema or default)
            cols, pks, fks, views = self._reflect(conn, insp, schema, kind, notes)
            unique = self._unique_columns(conn, insp, schema, kind)
            try:
                comments = insp.get_multi_table_comment(schema=schema, kind=kind)
            except ReadOnlyViolation:
                raise
            except Exception:                        # not every engine has table comments
                comments = {}
                conn.rollback()
                self._begin_read_only(conn)
            for (sch, name), columns in cols.items():
                t = model.table(show(name), shown)
                t.kind = 'view' if name in views else 'table'
                t.add_origin(Origin('db', source_id, label, shown, schema=shown, table=show(name)))
                t.comment = (comments.get((sch, name)) or {}).get('text') or None
                pk = set((pks.get((sch, name)) or {}).get('constrained_columns') or [])
                for c in columns:
                    kind_of = type_name(c.get('type'), conn.dialect)
                    nullable = c.get('nullable')
                    if sqlite_db and pk == {c['name']} and 'INT' in (kind_of or '').upper():
                        nullable = False             # INTEGER PRIMARY KEY is the rowid: never NULL
                    t.add_column(show(c['name']), 'db', type=kind_of, nullable=nullable,
                                 pk=c['name'] in pk, comment=c.get('comment'),
                                 unique=c['name'] in unique.get(name, ()) or pk == {c['name']})
                loaded[(sch, name)] = t
                foreign += [(t, sch, fk) for fk in fks.get((sch, name)) or []]
            if not cols and schema:
                notes.append(f'No tables found in schema {schema}.')

        for t, sch, fk in foreign:
            rname, rschema = fk['referred_table'], fk.get('referred_schema')
            target = loaded.get((rschema, rname)) if rschema else (loaded.get((sch, rname)) or loaded.get((None, rname)))
            if target is None:                       # points outside the schemas that were loaded
                target = model.table(show(rname), show(rschema) if rschema else None)
                if not target.origins:
                    target.add_origin(Origin('ref', source_id, label, 'referenced by a foreign key, not loaded'))
            columns = [show(c) for c in fk.get('constrained_columns') or []]
            referred = [show(c) for c in fk.get('referred_columns') or []]
            if len(referred) != len(columns):
                key = [c.name for c in target.columns if c.pk]
                referred = key if len(key) == len(columns) else columns
            for c in columns:
                if t.column(c) is not None:
                    t.column(c).fk = True
            name = fk.get('name')
            model.relate(t, columns, target, referred, DECLARED, cardinality='1-N', name=name,
                         evidence=[f'foreign key {name} in the database' if name else 'foreign key in the database'])
        return model

    def _unique_columns(self, conn, insp, schema, kind):
        """{table: the columns that are unique by themselves} in one schema: one-column UNIQUE constraints and
        one-column unique indexes (not a partial index, not one on an expression). Two reads of the catalog;
        a catalog that cannot answer one of them gives nothing for it. A link is only inferred from a name
        when the column it would point at is unique (app.model.infer_by_names)."""
        out = {}
        try:
            for (_, table), constraints in insp.get_multi_unique_constraints(schema=schema, kind=kind).items():
                for constraint in constraints or []:
                    columns = [c for c in constraint.get('column_names') or [] if c]
                    if len(columns) == 1:
                        out.setdefault(table, set()).add(columns[0])
        except ReadOnlyViolation:
            raise
        except Exception:
            conn.rollback()
            self._begin_read_only(conn)
        try:
            for (_, table), indexes in insp.get_multi_indexes(schema=schema, kind=kind, **auto_indexes(conn.dialect)).items():
                for index in indexes or []:
                    options = index.get('dialect_options') or {}
                    partial = any(key.endswith('_where') and value is not None for key, value in options.items())
                    columns = index.get('column_names') or []
                    if index.get('unique') and not partial and not index.get('expressions') and len(columns) == 1 and columns[0]:
                        out.setdefault(table, set()).add(columns[0])
        except ReadOnlyViolation:
            raise
        except Exception:
            conn.rollback()
            self._begin_read_only(conn)
        return out

    def _reflect(self, conn, insp, schema, kind, notes):
        """Columns, primary keys and foreign keys of every table in one schema."""
        views = set()
        if self.profile.include_views:
            for getter in (insp.get_view_names, insp.get_materialized_view_names):
                try:
                    views.update(getter(schema=schema))
                except ReadOnlyViolation:
                    raise
                except Exception:
                    conn.rollback()
                    self._begin_read_only(conn)
        try:
            return (insp.get_multi_columns(schema=schema, kind=kind),
                    insp.get_multi_pk_constraint(schema=schema, kind=kind),
                    insp.get_multi_foreign_keys(schema=schema, kind=kind), views)
        except ReadOnlyViolation:
            raise
        except Exception as exc:                     # one odd table must not hide the rest
            notes.append(f'Reading all tables at once failed ({self.clean_error(exc)}); read them one by one.')
        conn.rollback()
        self._begin_read_only(conn)
        cols, pks, fks = {}, {}, {}
        names = list(insp.get_table_names(schema=schema)) + sorted(views)
        for name in names:
            try:
                c = insp.get_columns(name, schema=schema)
                p = insp.get_pk_constraint(name, schema=schema)
                f = insp.get_foreign_keys(name, schema=schema)
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                notes.append(f'{name}: skipped ({self.clean_error(exc)})')
                conn.rollback()
                self._begin_read_only(conn)
                continue
            cols[(schema, name)], pks[(schema, name)], fks[(schema, name)] = c, p, f
        return cols, pks, fks, views
