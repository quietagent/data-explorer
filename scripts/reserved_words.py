"""Which words an engine does not take for a name - asked of the engine itself.

    python -m scripts.reserved_words                    every engine that can be reached, held against app/sqlguard.py
    python -m scripts.reserved_words postgresql --list  its words, as they stand in sqlguard.py

The guard's reader of a statement (sqlguard._Names, and lineage.py with it) takes an unquoted word of
a FROM clause for a table, or for the alias of one, only when the engine does. Which words an engine
keeps for itself differs: PUBLIC, SESSION and LEVEL are reserved on Oracle and plain names on
PostgreSQL; USE and KEY are reserved on MySQL only. So each engine is asked, word by word:

    a table?    SELECT 1 FROM <word>                        "there is no such table" = a name;
                                                            a syntax error = not a name
    an alias?   SELECT <word>.<column> FROM <a table> <word>    it runs = an alias

(A word that gives rows as a table is a name when it can be an alias too - there happens to be a
table called that - and the engine's own when it cannot: USER on PostgreSQL, DUAL on MySQL.)

A word that fails either question is reserved for the reader (sqlguard.RESERVED). The words that
are asked: every word list of sqlguard.py, the engines' own lists of key words where they have one
(pg_get_keywords(), INFORMATION_SCHEMA.KEYWORDS, duckdb_keywords()) and the lists at the end of
this file. Every statement sent is a SELECT.

SQLite and DuckDB run inside Python; the others need the test containers (scripts\\live_db.ps1 start).
Nothing here is used by the app.
"""

from __future__ import annotations

import re
import sqlite3
import sys

from app import sqlguard
from scripts import live_engines as le

LOCAL = ('sqlite', 'duckdb')
SERVERS = ('postgresql', 'mysql', 'mssql', 'oracle')
WORD = re.compile(r'[A-Z][A-Z0-9_]*')

# name: (the answer that says "this is a name, and there is no such table", a table every account can read, a column of it)
ASK = {
    'postgresql': (lambda w: f'relation "{w.lower()}" does not exist', 'pg_catalog.pg_class', 'relname'),
    'mysql': (lambda w: "doesn't exist", 'information_schema.schemata', 'schema_name'),
    'mssql': (lambda w: f"Invalid object name '{w}'", 'sys.objects', 'name'),
    'oracle': (lambda w: 'ORA-00942', 'dual', 'dummy'),
    'sqlite': (lambda w: f'no such table: {w}', 'sqlite_master', 'name'),
    'duckdb': (lambda w: f'Table with name {w} does not exist', 'information_schema.tables', 'table_name'),
}


def _sender(engine):
    """(function(sql) -> 'ran' or the engine's error text, function() that closes)."""
    if engine == 'sqlite':
        con = sqlite3.connect(':memory:')

        def send(sql):
            try:
                con.execute(sql).fetchall()
                return 'ran'
            except sqlite3.Error as exc:
                return str(exc)
        return send, con.close
    if engine == 'duckdb':
        from app.sources import duck
        con = duck.open_memory()                        # as the app opens it: a Python variable is never read as a table

        def send(sql):
            try:
                con.execute(sql).fetchall()
                return 'ran'
            except Exception as exc:
                return str(exc)
        return send, con.close
    eng = le.admin_engine(engine)
    conn = eng.connect().execution_options(isolation_level='AUTOCOMMIT')

    def send(sql):
        try:
            conn.exec_driver_sql(sql).fetchall()
            return 'ran'
        except Exception as exc:
            return str(exc)

    def close():
        conn.close()
        eng.dispose()
    return send, close


def own_keywords(engine, send=None):
    """The engine's own list of its key words, where it has one: {WORD: its category}."""
    if engine == 'sqlite':
        return {}
    if engine == 'duckdb':
        from app.sources import duck
        con = duck.open_memory()
        try:
            return {w.upper(): c for w, c in con.execute('SELECT keyword_name, keyword_category FROM duckdb_keywords()').fetchall()}
        finally:
            con.close()
    statement = {'postgresql': 'SELECT word, catcode FROM pg_get_keywords()',
                 'mysql': 'SELECT WORD, RESERVED FROM INFORMATION_SCHEMA.KEYWORDS'}.get(engine)
    if statement is None:
        return {}
    eng = le.admin_engine(engine)
    try:
        with eng.connect() as conn:
            return {str(w).upper(): str(c) for w, c in conn.exec_driver_sql(statement).fetchall()}
    finally:
        eng.dispose()


def needs_as():
    """PostgreSQL: the key words that are not the label of a column unless AS stands in front of them."""
    eng = le.admin_engine('postgresql')
    try:
        with eng.connect() as conn:
            return {str(w).upper() for (w,) in conn.exec_driver_sql('SELECT word FROM pg_get_keywords() WHERE NOT barelabel').fetchall()}
    finally:
        eng.dispose()


def candidates(engines=()):
    """Every word worth asking about."""
    words = set(OTHER_WORDS.split())
    for value in vars(sqlguard).values():
        if isinstance(value, (set, frozenset)) and all(isinstance(w, str) for w in value):
            words |= value
        elif isinstance(value, dict) and all(isinstance(v, (set, frozenset)) for v in value.values()):
            for group in value.values():
                words |= {w for w in group if isinstance(w, str)}
    for engine in engines:
        words |= set(own_keywords(engine))
    return sorted(w for w in {str(w).upper() for w in words} if WORD.fullmatch(w))


def ask(engine, words):
    """{WORD: (a table name?, a table alias?)} as this engine answers."""
    no_table, table, column = ASK[engine]
    send, close = _sender(engine)
    try:
        assert send(f'SELECT zz_probe.{column} FROM {table} zz_probe') == 'ran', f'{engine}: the question itself does not run'
        assert no_table('ZZ_PROBE' if engine != 'postgresql' else 'zz_probe').lower() in send('SELECT 1 FROM zz_probe').lower()
        found = {}
        for word in words:
            w = word.lower()
            answer = send(f'SELECT 1 FROM {w}')
            is_alias = send(f'SELECT {w}.{column} FROM {table} {w}') == 'ran'
            # A name: the engine looks for a table called that. It may even find one (PLANS is a table of the demo
            # schema) - but a word that gives rows and cannot be an alias is the engine's own: USER, DUAL.
            is_table = no_table(w).lower() in answer.lower() or answer == 'ran' and is_alias
            found[word] = (is_table, is_alias)
        return found
    finally:
        close()


def reserved(engine, words=None):
    """The words this engine does not take for a table or for the alias of one."""
    words = candidates([engine]) if words is None else words
    return {word for word, (is_table, is_alias) in ask(engine, words).items() if not (is_table and is_alias)}


def reachable(engine):
    if engine in LOCAL:
        try:
            __import__('duckdb' if engine == 'duckdb' else 'sqlite3')
            return True
        except ImportError:
            return False
    return le.reachable(engine, seeded=False)


def main(argv):
    listing = '--list' in argv
    wanted = [a for a in argv if not a.startswith('--')] or list(LOCAL + SERVERS)
    known = getattr(sqlguard, 'RESERVED', {})
    for engine in wanted:
        if not reachable(engine):
            print(f'{engine}: cannot be reached (scripts\\live_db.ps1 start)')
            continue
        words = candidates([engine])
        answers = ask(engine, words)
        found = {w for w, (is_table, is_alias) in answers.items() if not (is_table and is_alias)}
        only_alias = sorted(w for w, (is_table, is_alias) in answers.items() if is_table and not is_alias)
        only_table = sorted(w for w, (is_table, is_alias) in answers.items() if is_alias and not is_table)
        print(f'{engine}: {len(words)} words asked, {len(found)} are not a name '
              f'({len(only_alias)} of them a table but not an alias, {len(only_table)} an alias but not a table)')
        if listing:
            line = ''
            for word in sorted(found):
                if len(line) + len(word) > 116:
                    print(line.rstrip())
                    line = ''
                line += word + ' '
            print(line.rstrip())
            print('  a table, not an alias:', ' '.join(only_alias))
            print('  an alias, not a table:', ' '.join(only_table))
        if engine in known:
            mine = set(known[engine])
            print(f'  sqlguard.RESERVED has {len(mine)}: the engine reserves and the guard does not: '
                  f'{sorted(found - mine) or "none"}; the guard reserves and the engine does not: '
                  f'{sorted((mine & set(words)) - found) or "none"}')
    return 0


# Words that are in none of the lists above: SQL Server's reserved words (it has no list to ask), ODBC's, SQLite's
# key words, and words of FROM clauses and of select lists in one engine or another.
OTHER_WORDS = '''
ABORT ABSOLUTE ACTION ADA ADD AFTER ALL ALLOCATE ALTER ALWAYS ANALYSE ANALYZE AND ANTI ANY APPLY ARE ARRAY AS ASC ASOF
ASSERTION ASYMMETRIC AT ATTACH AUTHORIZATION AUTOINCREMENT AVG BACKUP BEFORE BEGIN BETWEEN BIGINT BINARY BIT BIT_LENGTH
BOTH BREAK BROWSE BULK BY CALL CASCADE CASCADED CASE CAST CATALOG CHANGE CHAR CHARACTER CHARACTER_LENGTH CHAR_LENGTH
CHECK CHECKPOINT CLOSE CLUSTERED COALESCE COLLATE COLLATION COLUMN COLUMNS COMMENT COMMIT COMPUTE CONCURRENTLY CONFLICT
CONNECT CONNECTION CONSTRAINT CONSTRAINTS CONTAINS CONTAINSTABLE CONTINUE CONVERT CORRESPONDING COUNT CREATE CROSS CUBE
CUME_DIST CURRENT CURRENT_CATALOG CURRENT_DATE CURRENT_PATH CURRENT_ROLE CURRENT_SCHEMA CURRENT_TIME CURRENT_TIMESTAMP
CURRENT_USER CURSOR DATA DATABASE DATABASES DATE DAY DAY_HOUR DAY_MICROSECOND DAY_MINUTE DAY_SECOND DBCC DEALLOCATE DEC
DECIMAL DECLARE DEFAULT DEFERRABLE DEFERRED DELAYED DELETE DENSE_RANK DENY DESC DESCRIBE DESCRIPTOR DETACH
DETERMINISTIC DIAGNOSTICS DISCONNECT DISK DISTINCT DISTINCTROW DISTRIBUTED DIV DO DOMAIN DOUBLE DROP DUAL DUMP EACH
ELSE ELSEIF EMPTY ENCLOSED END ERRLVL ESCAPE ESCAPED EXCEPT EXCEPTION EXCLUDE EXCLUSIVE EXEC EXECUTE EXISTS EXIT
EXPLAIN EXTERNAL EXTRACT FAIL FALSE FETCH FILE FILLFACTOR FILTER FINAL FIRST FIRST_VALUE FLOAT FLOAT4 FLOAT8 FOLLOWING
FOR FORCE FOREIGN FORTRAN FOUND FREETEXT FREETEXTTABLE FREEZE FROM FULL FULLTEXT FUNCTION GENERATED GET GLOB GLOBAL GO
GOTO GRANT GROUP GROUPING GROUPS HASH HAVING HIGH_PRIORITY HOLDLOCK HOUR HOUR_MICROSECOND HOUR_MINUTE HOUR_SECOND
IDENTITY IDENTITYCOL IDENTITY_INSERT IF IGNORE ILIKE IMMEDIATE IN INCLUDE INDEX INDEXED INDICATOR INFILE INITIALLY
INNER INOUT INPUT INSENSITIVE INSERT INSTEAD INT INT1 INT2 INT3 INT4 INT8 INTEGER INTERSECT INTERVAL INTO IO_AFTER_GTIDS
IO_BEFORE_GTIDS IS ISNULL ISOLATION ITERATE JOIN JSON_TABLE KEY KEYS KILL LAG LANGUAGE LAST LAST_VALUE LATERAL LEAD
LEADING LEAVE LEFT LEVEL LIKE LIMIT LINEAR LINENO LINES LOAD LOCAL LOCALTIME LOCALTIMESTAMP LOCK LONG LONGBLOB LONGTEXT
LOOP LOWER LOW_PRIORITY MASTER_BIND MASTER_SSL_VERIFY_SERVER_CERT MATCH MATERIALIZED MAX MAXVALUE MEDIUMBLOB MEDIUMINT
MEDIUMTEXT MERGE MIDDLEINT MIN MINUS MINUTE MINUTE_MICROSECOND MINUTE_SECOND MOD MODE MODIFIES MODULE MONTH NAMES
NATIONAL NATURAL NCHAR NEXT NO NOCHECK NOLOCK NONCLUSTERED NONE NOT NOTHING NOTNULL NO_WRITE_TO_BINLOG NTH_VALUE NTILE
NULL NULLIF NULLS NUMERIC OCTET_LENGTH OF OFF OFFSET OFFSETS ON ONLY OPEN OPENDATASOURCE OPENJSON OPENQUERY OPENROWSET
OPENXML OPTIMIZE OPTIMIZER_COSTS OPTION OPTIONALLY OR ORDER OTHERS OUT OUTER OUTFILE OUTPUT OVER OVERLAPS PAD PARTIAL
PARTITION PASCAL PERCENT PERCENT_RANK PIVOT PLACING PLAN POSITION POSITIONAL PRAGMA PRECEDING PRECISION PREPARE
PRESERVE PRIMARY PRINT PRIOR PRIVILEGES PROC PROCEDURE PUBLIC PURGE QUALIFY QUERY RAISE RAISERROR RANGE RANK READ
READPAST READS READTEXT READ_WRITE REAL RECONFIGURE RECURSIVE REFERENCES REGEXP REINDEX RELATIVE RELEASE REMOTE RENAME
REPEAT REPLACE REPLICATION REQUIRE RESIGNAL RESOURCE RESTORE RESTRICT RETURN RETURNING REVERT REVOKE RIGHT RLIKE
ROLLBACK ROW ROWCOUNT ROWGUIDCOL ROWID ROWNUM ROWS ROW_NUMBER RULE SAMPLE SAVE SAVEPOINT SCHEMA SCHEMAS SCROLL SECOND
SECOND_MICROSECOND SECTION SECURITYAUDIT SELECT SEMANTICKEYPHRASETABLE SEMANTICSIMILARITYDETAILSTABLE
SEMANTICSIMILARITYTABLE SEMI SENSITIVE SEPARATOR SESSION SESSION_USER SET SETUSER SHOW SHUTDOWN SIGNAL SIMILAR SIZE
SMALLINT SOME SPACE SPATIAL SPECIFIC SQL SQLCA SQLCODE SQLERROR SQLEXCEPTION SQLSTATE SQLWARNING SQL_BIG_RESULT
SQL_CALC_FOUND_ROWS SQL_SMALL_RESULT SSL START STARTING STATISTICS STORED STRAIGHT_JOIN SUBSTRING SUM SUMMARIZE
SYMMETRIC SYSTEM SYSTEM_USER TABLE TABLESAMPLE TEMP TEMPORARY TERMINATED TEXTSIZE THEN TIES TIME TIMESTAMP
TIMEZONE_HOUR TIMEZONE_MINUTE TINYBLOB TINYINT TINYTEXT TO TOP TRAILING TRAN TRANSACTION TRANSLATE TRANSLATION TRIGGER
TRIM TRUE TRUNCATE TRY_CONVERT TSEQUAL UNBOUNDED UNDO UNION UNIQUE UNKNOWN UNLOCK UNPIVOT UNSIGNED UPDATE UPDATETEXT
UPPER USAGE USE USER USING UTC_DATE UTC_TIME UTC_TIMESTAMP VACUUM VALUE VALUES VARBINARY VARCHAR VARCHARACTER VARIADIC
VARYING VERBOSE VIEW VIRTUAL WAITFOR WHEN WHENEVER WHERE WHILE WINDOW WITH WITHIN WITHOUT WORK WRITE WRITETEXT XOR YEAR
YEAR_MONTH ZEROFILL ZONE
'''


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
