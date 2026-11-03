"""The guard for SQL that a person types (the SQL tab).

check(sql, dialect) raises ReadOnlyViolation unless the text is ONE read-only query, and returns
it as it may be sent: the same text with its comments blanked out and no trailing semicolon.
security.check_read_only_sql(sql, dialect, strict=True) is this function.

It works on tokens, read the way the target engine reads them, because every known way past a
keyword check hides text from the checker that the engine still executes:

    Oracle        q'[ ... ]' and nq'{ ... }' strings (their ends are not a plain quote)
    [ ... ]       a quoted name on SQL Server and SQLite only; on PostgreSQL it is an array subscript,
                  so what stands inside is code
    ` ... `       a quoted name on MySQL and SQLite only
    comments      are never sent: the engine cannot disagree with the guard about where one ends
    backslashes, dollar quotes, MySQL's "--x" and "#": refused where they can change what a string is

What is allowed, after that:

  * a statement that starts with SELECT or WITH - nothing else
  * no word that writes, locks or runs code: INSERT, UPDATE, DELETE, MERGE, INTO, DDL, EXEC, CALL,
    COPY, ATTACH, LOAD ...; no FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE; no SQL Server lock
    hint (UPDLOCK, XLOCK, HOLDLOCK, TABLOCK, TABLOCKX, ROWLOCK, PAGLOCK, SERIALIZABLE ...); no
    sequence (NEXTVAL, NEXT VALUE FOR); no second statement
  * functions: only those on the engine's list in app/analysis/sql_functions.json. A function with
    a schema or package in front of it (dbms_lock.sleep, dbo.my_function) is refused whatever its
    name, and so is any name the list does not have. A user-defined function can do anything -
    in Oracle it can write through an autonomous transaction, inside a read-only transaction
  * calls without brackets (see "names in scope" below, and `Known`):
      Oracle       calls a function that takes no argument without brackets - SELECT my_function
                   FROM dual. So every name is judged in its own query block: a bare name must be
                   a column of a table in that block's FROM (or of a block around it, for a
                   subquery in an expression); a table alias or the name of a WITH query is only
                   accepted in front of a dot; a column alias only in the ORDER BY of its own
                   block. Quoted names are judged like the others. Nothing may carry the name of
                   something the database can call, nor of a schema: the source reads those names
                   from the catalog, and without that list no statement passes.
      PostgreSQL   calls f(t) for t.f and (t).f. So in t.name, t must be a table or alias of the
                   query block and name a column the source knows that table has.
    To judge names, a statement is read: its query blocks, the tables and aliases of each FROM
    clause, the names its select lists give. An unquoted word is taken for a table or an alias
    only when the engine takes it for one - each engine has its own reserved words (RESERVED):
    FROM public.customers is a schema and a table on PostgreSQL, and not a name on Oracle.

This is a filter in front of the real protections - the read-only transaction, the statement
timeout and a database account that cannot write - not a replacement for them. What it cannot
see is listed in README.md ("SQL tab").
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

FUNCTIONS_FILE = Path(__file__).parent / 'analysis' / 'sql_functions.json'
DIALECTS = ('oracle', 'mssql', 'postgresql', 'mysql', 'sqlite', 'duckdb')
BACKSLASH_SAFE = ('oracle', 'mssql', 'sqlite')      # engines where \ is never an escape character
BRACKET_NAMES = ('mssql', 'sqlite')                 # [name] is a quoted name here; elsewhere [ ] is code
BACKTICK_NAMES = ('mysql', 'sqlite')


class ReadOnlyViolation(Exception):
    """A statement that is not a plain read was about to be sent to a database."""


# ------------------------------------------------------------------ the words ----

# Words that turn a query into a write, a lock or a call - on every engine. Unquoted only: a column
# really called "update" has to be written in quotes.
WRITE_WORDS = {
    'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'UPSERT', 'INTO', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE', 'GRANT', 'REVOKE',
    'EXEC', 'EXECUTE', 'CALL', 'LOCK', 'OUTFILE', 'DUMPFILE', 'NEXTVAL', 'CURRVAL', 'SETVAL', 'LASTVAL',
}
# Words that only ever start a statement of their own. A second statement needs a semicolon (refused)
# on most engines, so there these words can only be names. Not on SQL Server, where statements follow
# each other without one; and on DuckDB (files) and an unknown engine they are refused to be sure.
STATEMENT_WORDS = {
    'COPY', 'ATTACH', 'DETACH', 'INSTALL', 'LOAD', 'EXPORT', 'IMPORT', 'PRAGMA', 'VACUUM', 'REINDEX', 'COMMIT',
    'ROLLBACK', 'SAVEPOINT', 'HANDLER', 'DECLARE', 'PREPARE', 'DEALLOCATE', 'LISTEN', 'NOTIFY', 'UNLISTEN', 'REFRESH',
    'CLUSTER', 'DISCARD', 'CHECKPOINT', 'SHUTDOWN', 'KILL', 'FLUSH', 'PURGE', 'WAITFOR', 'DBCC', 'BACKUP', 'RESTORE',
    'RECONFIGURE', 'BULK', 'RENAME', 'DENY',
}
STATEMENT_WORD_ENGINES = ('mssql', 'duckdb', None)
MSSQL_WORDS = {'SET', 'USE', 'BEGIN', 'IF', 'WHILE', 'GOTO', 'RETURN', 'PRINT', 'RAISERROR', 'THROW', 'GO', 'READTEXT',
               'WRITETEXT', 'UPDATETEXT', 'ENABLE', 'DISABLE'}
LOCK_HINTS = {'UPDLOCK', 'XLOCK', 'HOLDLOCK', 'TABLOCK', 'TABLOCKX', 'ROWLOCK', 'PAGLOCK', 'SERIALIZABLE',
              'REPEATABLEREAD', 'READCOMMITTEDLOCK', 'BROWSE'}
# MySQL has string functions with these names; followed by a bracket they are judged as functions.
MYSQL_FUNCTION_WORDS = {'TRUNCATE'}

# Words that may stand in front of a bracket without being a function call.
BRACKET_WORDS = {
    'IN', 'EXISTS', 'ANY', 'ALL', 'SOME', 'NOT', 'AND', 'OR', 'ON', 'WHEN', 'THEN', 'ELSE', 'SELECT', 'FROM', 'WHERE',
    'HAVING', 'BY', 'UNION', 'INTERSECT', 'EXCEPT', 'MINUS', 'JOIN', 'USING', 'OVER', 'FILTER', 'GROUP', 'WITHIN',
    'AS', 'LATERAL', 'CASE', 'BETWEEN', 'LIKE', 'ILIKE', 'IS', 'VALUES', 'DISTINCT', 'LIMIT', 'OFFSET', 'TOP', 'ONLY',
    'WITH', 'RECURSIVE', 'MATERIALIZED', 'APPLY', 'PIVOT', 'UNPIVOT', 'PARTITION', 'SUBPARTITION', 'TABLE', 'SAMPLE',
    'TABLESAMPLE', 'SYSTEM', 'BERNOULLI', 'REPEATABLE', 'SEED', 'BLOCK', 'ESCAPE', 'FETCH', 'FIRST', 'NEXT', 'PERCENT',
    'ROWS', 'RANGE', 'GROUPS', 'INTERVAL', 'ARRAY', 'ROW', 'CUBE', 'ROLLUP', 'SETS', 'KEEP', 'OPTION', 'INDEX', 'USE',
    'FORCE', 'IGNORE', 'FOR', 'END', 'WINDOW', 'QUALIFY', 'ORDER', 'ANTI', 'SEMI', 'ASOF', 'MATCH', 'AGAINST',
}
# Oracle: words of the language. Most of them are NOT reserved: Oracle lets a function be called FIRST,
# YEAR, VALUE or CASE and calls it without brackets (tried on Oracle 23, one function per word), so a
# word of this list only counts as a word where the syntax puts it (_Names._as_word). The list itself
# is used for one thing: a name behind one of these words is not taken for an alias.
ORACLE_WORDS = BRACKET_WORDS | set('''
ASC DESC NULL NULLS LAST TRUE FALSE UNKNOWN LEFT RIGHT FULL INNER OUTER CROSS NATURAL JOIN CONNECT START PRIOR NOCYCLE
LEVEL ROWNUM ROWID DUAL SYSDATE SYSTIMESTAMP CURRENT_DATE CURRENT_TIMESTAMP LOCALTIMESTAMP SESSIONTIMEZONE DBTIMEZONE
USER UID DATE TIMESTAMP TIME ZONE AT LOCAL YEAR MONTH DAY HOUR MINUTE SECOND TO UNBOUNDED PRECEDING FOLLOWING CURRENT
TIES SIBLINGS DENSE_RANK RANK EPOCH BOTH LEADING TRAILING CHAR BYTE NUMBER VARCHAR VARCHAR2 NVARCHAR2 NCHAR INTEGER
INT DECIMAL NUMERIC FLOAT BINARY_FLOAT BINARY_DOUBLE CLOB BLOB RAW LONG CAST OF XML JSON VALUE EMPTY ERROR ABSENT
PRESENT CONDITIONAL UNCONDITIONAL WRAPPER WITHOUT KEYS FORMAT PATH COLUMNS NESTED ORDINALITY EXISTS INCLUDE EXCLUDE
RESPECT MEASURES DIMENSION RULES ITERATE UNTIL REFERENCE MAIN VERSIONS SCN MATCH_RECOGNIZE PATTERN DEFINE SUBSET
PER PAST SKIP ONE DUMMY CONNECT_BY_ISLEAF CONNECT_BY_ISCYCLE CONNECT_BY_ROOT NAN INFINITE A SET DEFAULT SHARE MODE
NOWAIT WAIT LOCKED OJ ONLY CHECK READ CONSTRAINT RETURNING COLLATE GROUPING WHEN
'''.split())
# Oracle's reserved words: nothing can be created under one of these names, so they are never a call.
ORACLE_RESERVED = set('''
ACCESS ADD ALL ALTER AND ANY AS ASC AUDIT BETWEEN BY CHAR CHECK CLUSTER COLUMN COMMENT COMPRESS CONNECT CREATE CURRENT
DATE DECIMAL DEFAULT DELETE DESC DISTINCT DROP ELSE EXCEPT EXCLUSIVE EXISTS FILE FLOAT FOR FROM GRANT GROUP HAVING
IDENTIFIED IMMEDIATE IN INCREMENT INDEX INITIAL INSERT INTEGER INTERSECT INTO IS LEVEL LIKE LOCK LONG MAXEXTENTS MINUS
MLSLABEL MODE MODIFY NOAUDIT NOCOMPRESS NOT NOWAIT NULL NUMBER OF OFFLINE ON ONLINE OPTION OR ORDER PCTFREE PRIOR PUBLIC
RAW RENAME RESOURCE REVOKE ROW ROWID ROWNUM ROWS SELECT SESSION SET SHARE SIZE SMALLINT START SUCCESSFUL SYNONYM SYSDATE
TABLE THEN TO TRIGGER UID UNION UNIQUE UPDATE USER VALIDATE VALUES VARCHAR VARCHAR2 VIEW WHENEVER WHERE WITH
'''.split())
# Not reserved, but the built-in wins over a function of that name (tried the same way).
ORACLE_BUILT_IN = {'CURRENT_DATE', 'CURRENT_TIMESTAMP', 'LOCALTIMESTAMP', 'SYSTIMESTAMP', 'DBTIMEZONE', 'SESSIONTIMEZONE',
                   'CONNECT_BY_ISLEAF', 'CONNECT_BY_ISCYCLE', 'CONNECT_BY_ROOT', 'ORA_ROWSCN'}
ORACLE_NEVER_A_CALL = ORACLE_RESERVED | ORACLE_BUILT_IN
# What each engine does NOT take for the name of a table, or for the alias of one, when it is written without
# quotes. The reader of a statement (_Names) goes by the list of the engine the statement is for: PUBLIC, SESSION,
# COMMENT, LEVEL, RESOURCE and SIZE are reserved on Oracle and plain names everywhere else; USE and KEY are
# reserved on MySQL only. Until 13 Oct 2026 Oracle's list was used for every engine, and PostgreSQL's
# FROM public.customers was refused.
# The lists were asked of the engines themselves, word by word (scripts/reserved_words.py: "SELECT 1 FROM <word>"
# must answer that there is no such table, and "FROM <a table> <word>" must make the word its alias) - on
# PostgreSQL 16.15, MySQL 8.4.11, SQL Server 2022 (16.0), SQLite 3.43 and DuckDB 1.5. On PostgreSQL that is exactly
# what pg_get_keywords() calls reserved or "type or function name", on MySQL what INFORMATION_SCHEMA.KEYWORDS
# calls reserved. Added by hand: two words that MySQL 8.0 still reserved, and five that DuckDB's own list of key
# words calls reserved although it takes them for names.
# A word too many here costs a refusal ("write the name in quotes"). A word too few would let the reader take for
# a table what the engine reads as syntax: add to a list, and do not take a word out of one without asking the
# engine (tests/test_sqlguard.py and tests/test_live_sql.py do).
RESERVED = {
    'oracle': ORACLE_NEVER_A_CALL,
    'postgresql': set('''
ALL ANALYSE ANALYZE AND ANY ARRAY AS ASC ASYMMETRIC AUTHORIZATION BINARY BOTH CASE CAST CHECK COLLATE COLLATION COLUMN
CONCURRENTLY CONSTRAINT CREATE CROSS CURRENT_CATALOG CURRENT_DATE CURRENT_ROLE CURRENT_SCHEMA CURRENT_TIME
CURRENT_TIMESTAMP CURRENT_USER DEFAULT DEFERRABLE DESC DISTINCT DO ELSE END EXCEPT FALSE FETCH FOR FOREIGN FREEZE FROM
FULL GRANT GROUP HAVING ILIKE IN INITIALLY INNER INTERSECT INTO IS ISNULL JOIN LATERAL LEADING LEFT LIKE LIMIT
LOCALTIME LOCALTIMESTAMP NATURAL NOT NOTNULL NULL OFFSET ON ONLY OR ORDER OUTER OVERLAPS PLACING PRIMARY REFERENCES
RETURNING RIGHT SELECT SESSION_USER SIMILAR SOME SYMMETRIC SYSTEM_USER TABLE TABLESAMPLE THEN TO TRAILING TRUE UNION
UNIQUE USER USING VARIADIC VERBOSE WHEN WHERE WINDOW WITH
'''.split()),
    'mysql': set('''
ACCESSIBLE ADD ALL ALTER ANALYZE AND AS ASC ASENSITIVE BEFORE BETWEEN BIGINT BINARY BLOB BOTH BY CALL CASCADE CASE
CHANGE CHAR CHARACTER CHECK COLLATE COLUMN CONDITION CONSTRAINT CONTINUE CONVERT CREATE CROSS CUBE CUME_DIST
CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DATABASES DAY_HOUR DAY_MICROSECOND DAY_MINUTE
DAY_SECOND DEC DECIMAL DECLARE DEFAULT DELAYED DELETE DENSE_RANK DESC DESCRIBE DETERMINISTIC DISTINCT DISTINCTROW DIV
DOUBLE DROP DUAL EACH ELSE ELSEIF EMPTY ENCLOSED ESCAPED EXCEPT EXISTS EXIT EXPLAIN FALSE FETCH FIRST_VALUE FLOAT
FLOAT4 FLOAT8 FOR FORCE FOREIGN FROM FULLTEXT FUNCTION GENERATED GET GRANT GROUP GROUPING GROUPS HAVING HIGH_PRIORITY
HOUR_MICROSECOND HOUR_MINUTE HOUR_SECOND IF IGNORE IN INDEX INFILE INNER INOUT INSENSITIVE INSERT INT INT1 INT2 INT3
INT4 INT8 INTEGER INTERSECT INTERVAL INTO IO_AFTER_GTIDS IO_BEFORE_GTIDS IS ITERATE JOIN JSON_TABLE KEY KEYS KILL LAG
LAST_VALUE LATERAL LEAD LEADING LEAVE LEFT LIKE LIMIT LINEAR LINES LOAD LOCALTIME LOCALTIMESTAMP LOCK LONG LONGBLOB
LONGTEXT LOOP LOW_PRIORITY MASTER_BIND MASTER_SSL_VERIFY_SERVER_CERT MATCH MAXVALUE MEDIUMBLOB MEDIUMINT MEDIUMTEXT
MIDDLEINT MINUTE_MICROSECOND MINUTE_SECOND MOD MODIFIES NATURAL NOT NO_WRITE_TO_BINLOG NTH_VALUE NTILE NULL NUMERIC OF
ON OPTIMIZE OPTIMIZER_COSTS OPTION OPTIONALLY OR ORDER OUT OUTER OUTFILE OVER PARTITION PERCENT_RANK PRECISION PRIMARY
PROCEDURE PURGE QUALIFY RANGE RANK READ READS READ_WRITE REAL RECURSIVE REFERENCES REGEXP RELEASE RENAME REPEAT
REPLACE REQUIRE RESIGNAL RESTRICT RETURN REVOKE RIGHT RLIKE ROW ROWS ROW_NUMBER SCHEMA SCHEMAS SECOND_MICROSECOND
SELECT SENSITIVE SEPARATOR SET SHOW SIGNAL SMALLINT SPATIAL SPECIFIC SQL SQLEXCEPTION SQLSTATE SQLWARNING
SQL_BIG_RESULT SQL_CALC_FOUND_ROWS SQL_SMALL_RESULT SSL STARTING STORED STRAIGHT_JOIN SYSTEM TABLE TABLESAMPLE
TERMINATED THEN TINYBLOB TINYINT TINYTEXT TO TRAILING TRIGGER TRUE UNDO UNION UNIQUE UNLOCK UNSIGNED UPDATE USAGE USE
USING UTC_DATE UTC_TIME UTC_TIMESTAMP VALUES VARBINARY VARCHAR VARCHARACTER VARYING VIRTUAL WHEN WHERE WHILE WINDOW
WITH WRITE XOR YEAR_MONTH ZEROFILL
'''.split()),
    'mssql': set('''
ADD ALL ALTER AND ANY AS ASC AUTHORIZATION BACKUP BEGIN BETWEEN BREAK BROWSE BULK BY CASCADE CASE CHECK CHECKPOINT
CLOSE CLUSTERED COALESCE COLLATE COLUMN COMMIT COMPUTE CONSTRAINT CONTAINS CONTAINSTABLE CONTINUE CONVERT CREATE CROSS
CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DBCC DEALLOCATE DECLARE DEFAULT
DELETE DENY DESC DISTINCT DISTRIBUTED DOUBLE DROP ELSE END ERRLVL ESCAPE EXCEPT EXEC EXECUTE EXISTS EXIT EXTERNAL
FETCH FILE FILLFACTOR FOR FOREIGN FREETEXT FREETEXTTABLE FROM FULL FUNCTION GOTO GRANT GROUP HAVING HOLDLOCK IDENTITY
IDENTITYCOL IDENTITY_INSERT IF IN INDEX INNER INSERT INTERSECT INTO IS JOIN KEY KILL LEFT LIKE LINENO MERGE NATIONAL
NOCHECK NONCLUSTERED NOT NULL NULLIF OF OFF OFFSETS ON OPEN OPENDATASOURCE OPENQUERY OPENROWSET OPENXML OPTION OR
ORDER OUTER OVER PERCENT PIVOT PLAN PRIMARY PRINT PROC PROCEDURE PUBLIC RAISERROR READ READTEXT RECONFIGURE REFERENCES
REPLICATION RESTORE RESTRICT RETURN REVERT REVOKE RIGHT ROLLBACK ROWCOUNT ROWGUIDCOL RULE SAVE SCHEMA SELECT
SEMANTICKEYPHRASETABLE SEMANTICSIMILARITYDETAILSTABLE SEMANTICSIMILARITYTABLE SESSION_USER SET SETUSER SHUTDOWN SOME
STATISTICS SYSTEM_USER TABLE TABLESAMPLE TEXTSIZE THEN TO TOP TRAN TRANSACTION TRIGGER TRUNCATE TRY_CONVERT TSEQUAL
UNION UNIQUE UNPIVOT UPDATE UPDATETEXT USE USER VALUES VARYING VIEW WAITFOR WHEN WHERE WHILE WITH WRITETEXT
'''.split()),
    'sqlite': set('''
ADD ALL ALTER AND AS AUTOINCREMENT BETWEEN CASE CAST CHECK COLLATE COMMIT CONSTRAINT CREATE CROSS CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP DEFAULT DEFERRABLE DELETE DISTINCT DROP ELSE ESCAPE EXCEPT EXISTS FOREIGN FROM FULL
GROUP HAVING IN INDEX INDEXED INNER INSERT INTERSECT INTO IS ISNULL JOIN LEFT LIMIT NATURAL NOT NOTHING NOTNULL NULL
ON OR ORDER OUTER PRIMARY RAISE REFERENCES RETURNING RIGHT SELECT SET TABLE THEN TO TRANSACTION UNION UNIQUE UPDATE
USING VALUES WHEN WHERE
'''.split()),
    'duckdb': set('''
ALL ANALYSE ANALYZE AND ANTI ANY ARRAY AS ASC ASOF ASYMMETRIC AT AUTHORIZATION BINARY BOTH BY CASE CAST CHECK COLLATE
COLLATION COLUMN COLUMNS CONCURRENTLY CONSTRAINT CREATE CROSS DEFAULT DEFERRABLE DESC DESCRIBE DISTINCT DO ELSE END
EXCEPT FALSE FETCH FOR FOREIGN FREEZE FROM FULL GENERATED GLOB GROUP HAVING ILIKE IN INITIALLY INNER INTERSECT INTO IS
ISNULL JOIN LAMBDA LATERAL LEADING LEFT LIKE LIMIT MAP NATURAL NOT NOTNULL NULL OFFSET ON ONLY OR ORDER OUTER OVERLAPS
PIVOT PIVOT_LONGER PIVOT_WIDER PLACING POSITIONAL PRIMARY QUALIFY REFERENCES RETURNING RIGHT SELECT SEMI SHOW SIMILAR
SOME STRUCT SUMMARIZE SYMMETRIC TABLE TABLESAMPLE THEN TO TRAILING TRUE TRY_CAST UNION UNIQUE UNPACK UNPIVOT USING
VARIADIC VERBOSE WHEN WHERE WINDOW WITH
'''.split()),
}
# What the engines' own answers do not hold (see above): kept apart so that the tests can tell them from a mistake.
RESERVED_BY_HAND = {'mysql': {'MASTER_BIND', 'MASTER_SSL_VERIFY_SERVER_CERT'},
                    'duckdb': {'COLUMNS', 'GENERATED', 'MAP', 'STRUCT', 'TRY_CAST'}}
RESERVED_ANYWHERE = frozenset().union(*RESERVED.values())      # for an engine the guard does not know: the strictest
# PostgreSQL: key words that are names, but not the label of a column unless AS stands in front of them
# (pg_get_keywords(): not reserved, barelabel false). Two of them end a type: x::character varying and
# x::double precision. Read as "x::character, called varying", a subquery would seem to give a column "varying",
# and s.varying - which calls a function varying(s) if the database has one - would pass.
NEEDS_AS = {'postgresql': frozenset('CHAR CHARACTER DAY FILTER HOUR MINUTE MONTH OVER PRECISION SECOND VARYING WITHIN WITHOUT '
                                    'YEAR'.split())}
# Words after which an expression is complete, so that a name behind them is an alias.
ENDS_AN_EXPRESSION = {'END', 'NULL', 'TRUE', 'FALSE', 'SYSDATE', 'ROWNUM', 'ROWID', 'LEVEL', 'USER', 'UID'} | ORACLE_BUILT_IN
DATE_FIELDS = {'YEAR', 'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND', 'TIMEZONE_HOUR', 'TIMEZONE_MINUTE', 'TIMEZONE_REGION',
               'TIMEZONE_ABBR'}
SET_OPERATORS = {'UNION', 'INTERSECT', 'MINUS', 'EXCEPT'}
JOIN_WORDS = {'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'CROSS', 'NATURAL', 'APPLY'}
# What the guard does not follow in a FROM clause: it says so instead of guessing.
FROM_NOT_READ = {'PIVOT', 'UNPIVOT', 'MATCH_RECOGNIZE', 'VERSIONS', 'MODEL', 'WITH', 'FOR', 'ROWS'}
TYPE_CALLS = {'CAST', 'XMLCAST', 'TREAT', 'TRY_CAST'}          # CAST(x AS type): what follows AS is a type
SCOPED = ('oracle', 'postgresql')                               # the engines whose names are judged in their scope


# -------------------------------------------------------------- the functions ----

def load_functions(path=None, extra=None):
    """{dialect: set of allowed function names (upper case)}, plus 'types'.
    extra: 'name,dialect:name,...' - what DATA_EXPLORER_SQL_FUNCTIONS adds."""
    data = json.loads(Path(path or FUNCTIONS_FILE).read_text(encoding='utf-8'))
    common = {str(n).upper() for n in data.get('all', [])}
    out = {d: common | {str(n).upper() for n in data.get(d, [])} for d in DIALECTS}
    out['types'] = {str(n).upper() for n in data.get('types', [])}
    for item in re.split(r'[,;\s]+', extra if extra is not None else config.SQL_EXTRA_FUNCTIONS):
        if not item:
            continue
        dialect, _, name = item.rpartition(':')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$#]*', name) or (dialect and dialect.lower() not in DIALECTS):
            raise ValueError(f'DATA_EXPLORER_SQL_FUNCTIONS: "{item}" is not a function name or dialect:name')
        for d in ([dialect.lower()] if dialect else DIALECTS):
            out[d].add(name.upper())
    return out


FUNCTIONS = load_functions()


def use_functions(path=None, extra=None):
    """Load the function lists again (at start, and by tests). Returns them."""
    global FUNCTIONS
    FUNCTIONS = load_functions(path, extra)
    return FUNCTIONS


# ------------------------------------------------------------------- tokens ----

class Token:
    __slots__ = ('kind', 'text', 'up', 'start', 'end')

    def __init__(self, kind, text, start, end):
        self.kind, self.text, self.up, self.start, self.end = kind, text, text.upper(), start, end

    def __repr__(self):
        return f'{self.kind}:{self.text}'


Q_CLOSE = {'[': ']', '{': '}', '(': ')', '<': '>'}
NUMBER = re.compile(r'\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+')
IDENT = re.compile(r'[^\W\d][\w$#@]*|@@?[\w$#@]+|#{1,2}[\w$#@]+')
OPERATORS = ('<=>', '<>', '<=', '>=', '!=', '||', ':=', '::', '=>', '->>', '->', '@>', '<@', '!<', '!>')


def tokenize(sql, dialect=None):
    """The statement as the given engine reads it. Raises ReadOnlyViolation for text whose meaning
    depends on something the guard cannot know (an unclosed string, a backslash escape ...)."""
    tokens, i, n = [], 0, len(sql)
    brackets, backticks = dialect in BRACKET_NAMES, dialect in BACKTICK_NAMES

    def refuse(message):
        raise ReadOnlyViolation(message)

    def quoted(start, quote, kind):
        """A '...' string or "..." name starting at `start` (the opening quote); the quote doubles as its escape."""
        j = start + 1
        while True:
            j = sql.find(quote, j)
            if j < 0:
                refuse(f'A {quote} is never closed.')
            if sql[j:j + 2] == quote * 2:
                j += 2
                continue
            return j + 1

    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        two = sql[i:i + 2]
        if two == '--':
            # MySQL only takes "--" as a comment when white space follows; "5--1" is arithmetic there
            if dialect in ('mysql', None) and i + 2 < n and not sql[i + 2].isspace():
                refuse('Write "-- " with a space to start a comment.')
            end = sql.find('\n', i)
            end = n if end < 0 else end
            tokens.append(Token('comment', sql[i:end], i, end))
            i = end
            continue
        if two == '/*':
            end = sql.find('*/', i + 2)
            if end < 0:
                refuse('A /* comment is never closed.')
            if '/*' in sql[i + 2:end]:
                refuse('Comments inside comments are not allowed.')        # engines disagree on where such a comment ends
            tokens.append(Token('comment', sql[i:end + 2], i, end + 2))
            i = end + 2
            continue
        if ch == '#' and dialect in ('mysql', None):
            refuse('"#" is not allowed.')                                   # a comment on MySQL, and only there
        if ch == "'":
            end = quoted(i, "'", 'string')
            tokens.append(Token('string', sql[i:end], i, end))
            i = end
            continue
        if ch in 'qQnN' and dialect in ('oracle', None):
            # Oracle: q'[ ... ]'  q'! ... !'  nq'{ ... }' - the string ends at the delimiter followed by a quote
            m = re.match(r"[nN]?[qQ]'(.)", sql[i:])
            if m and not (i and (sql[i - 1].isalnum() or sql[i - 1] in '_$#')):
                if dialect is None:
                    refuse("q'...' strings are not allowed.")
                opener = m.group(1)
                closer = Q_CLOSE.get(opener, opener) + "'"
                if opener.isspace():
                    refuse("A q'...' string cannot use white space as its delimiter.")
                end = sql.find(closer, i + len(m.group(0)))
                if end < 0:
                    refuse("A q'...' string is never closed.")
                tokens.append(Token('string', sql[i:end + 2], i, end + 2))
                i = end + 2
                continue
        if ch in 'nNeEbBxXuU' and re.match(r"(?:[nNeEbBxX]|[uU]&)'", sql[i:]) and not (
                i and (sql[i - 1].isalnum() or sql[i - 1] in '_$#')):
            prefix = 2 if sql[i + 1] == '&' else 1
            if sql[i] in 'eEuU' and dialect not in ('mssql', 'oracle', 'sqlite'):
                refuse("E'...' and U&'...' strings (with escapes) are not allowed.")
            end = quoted(i + prefix, "'", 'string')
            tokens.append(Token('string', sql[i:end], i, end))
            i = end
            continue
        if ch == '"':
            end = quoted(i, '"', 'qident')
            tokens.append(Token('qident', sql[i + 1:end - 1].replace('""', '"'), i, end))
            i = end
            continue
        if ch == '`':
            if not backticks:
                refuse('A backtick (`) is not valid here.')
            end = sql.find('`', i + 1)
            if end < 0:
                refuse('A ` is never closed.')
            tokens.append(Token('qident', sql[i + 1:end], i, end + 1))
            i = end + 1
            continue
        if ch == '[' and brackets:
            end = sql.find(']', i + 1)
            if end < 0 or '\n' in sql[i:end]:
                refuse('A [ is never closed.')
            tokens.append(Token('qident', sql[i + 1:end], i, end + 1))
            i = end + 1
            continue
        if ch == '$' and dialect in ('postgresql', 'duckdb', None) and re.match(r'\$\w*\$', sql[i:]):
            refuse('Dollar-quoted strings are not allowed.')
        m = NUMBER.match(sql, i)
        if m and (ch.isdigit() or ch == '.' and m.end() > i + 1):
            tokens.append(Token('number', m.group(), i, m.end()))
            i = m.end()
            continue
        m = IDENT.match(sql, i)
        if m:
            tokens.append(Token('ident', m.group(), i, m.end()))
            i = m.end()
            continue
        op = next((o for o in OPERATORS if sql.startswith(o, i)), ch)
        tokens.append(Token('op', op, i, i + len(op)))
        i += len(op)
    return tokens


# --------------------------------------------------------------------- check ----

class Checked:
    """A statement that passed: `sql` is what may be sent; `tokens` are its tokens without comments."""

    def __init__(self, sql, tokens, dialect):
        self.sql, self.tokens, self.dialect = sql, tokens, dialect

    def names(self):
        """Every name the statement uses, as written (for error messages: these are not data)."""
        return [t.text for t in self.tokens if t.kind in ('ident', 'qident')]


def check(sql, dialect=None, names=None):
    """Raise ReadOnlyViolation unless `sql` is one read-only query. Returns a Checked.

    dialect   oracle / mssql / postgresql / mysql / sqlite / duckdb; None = unknown, the strictest reading
    names     a Known: what the source knows (tables with their columns; on Oracle also what the
              database can call). Used on Oracle and PostgreSQL, where a name can be a call without
              brackets. Without it - the guard alone, no source - names are not checked.
    """
    if isinstance(names, dict):
        names = Known(names.get('tables'), names.get('callable'), names.get('schemas') or ())
    text = (sql or '').strip()
    if not text:
        raise ReadOnlyViolation('Empty statement.')
    if len(text) > config.SQL_MAX_CHARS:
        raise ReadOnlyViolation(f'The statement is longer than {config.SQL_MAX_CHARS:,} characters.')
    if '\x00' in text:
        raise ReadOnlyViolation('Statement contains a NUL character.')
    if '/*!' in text or '/*M!' in text or '/*+' in text:
        raise ReadOnlyViolation('Executable comments and optimizer hints (/*! ... */, /*+ ... */) are not allowed.')
    if dialect not in BACKSLASH_SAFE and '\\' in text:
        raise ReadOnlyViolation('Backslashes are not allowed.')
    if dialect == 'oracle' and '@' in _outside_strings(text, dialect):
        raise ReadOnlyViolation('"@" (a database link) is not allowed.')

    every = tokenize(text, dialect)
    toks = [t for t in every if t.kind != 'comment']
    while toks and toks[-1].kind == 'op' and toks[-1].text == ';':
        toks.pop()
    if not toks:
        raise ReadOnlyViolation('Empty statement.')
    if any(t.kind == 'op' and t.text == ';' for t in toks):
        raise ReadOnlyViolation('Only one statement at a time is allowed.')

    first = next((t for t in toks if not (t.kind == 'op' and t.text == '(')), None)
    word = first.up if first is not None and first.kind == 'ident' else ''
    if word not in ('SELECT', 'WITH'):
        start = word or (first.text if first is not None else '(')
        raise ReadOnlyViolation(f"Only read-only queries can run (statement starts with '{start}').")

    allowed = FUNCTIONS.get(dialect) if dialect in DIALECTS else set.intersection(*[FUNCTIONS[d] for d in DIALECTS])
    types = FUNCTIONS['types']
    cte_names, _ = _ctes(toks)

    def at(i):
        return toks[i] if 0 <= i < len(toks) else None

    def is_op(tok, text):
        return tok is not None and tok.kind == 'op' and tok.text == text

    for i, t in enumerate(toks):
        nxt, prev = at(i + 1), at(i - 1)
        if t.kind == 'op':
            if t.text == ':=':
                raise ReadOnlyViolation('Assignments (:=) are not allowed.')
            if t.text == '#' and dialect != 'mssql':
                raise ReadOnlyViolation('"#" is not allowed.')
            continue
        if t.kind == 'string':
            if dialect == 'duckdb' and prev is not None and prev.kind == 'ident' and prev.up in ('FROM', 'JOIN'):
                raise ReadOnlyViolation('A file cannot be read as a table: only the tables of this source can be queried.')
            continue
        if t.kind == 'qident':
            if is_op(nxt, '('):
                raise ReadOnlyViolation(f'"{t.text}"() - a function with a quoted name - is not allowed.')
            continue
        if t.kind != 'ident':
            continue
        call = is_op(nxt, '(')
        if t.up in WRITE_WORDS and not (call and dialect == 'mysql' and t.up in MYSQL_FUNCTION_WORDS):
            if t.up in ('NEXTVAL', 'CURRVAL', 'SETVAL', 'LASTVAL'):
                raise ReadOnlyViolation(f'{t.up} is not allowed: a sequence changes when it is read.')
            if t.up == 'LOCK':
                raise ReadOnlyViolation('Locking reads (LOCK IN SHARE MODE) are not allowed.')
            raise ReadOnlyViolation(f'{t.up} is not allowed: only read-only queries can run.')
        if t.up == 'FOR' and nxt is not None and nxt.up in ('UPDATE', 'SHARE', 'KEY', 'NO', 'BROWSE'):
            raise ReadOnlyViolation('Locking reads (FOR UPDATE / FOR SHARE) are not allowed.')
        if t.up == 'NEXT' and nxt is not None and nxt.up == 'VALUE':
            raise ReadOnlyViolation('NEXT VALUE FOR is not allowed: a sequence changes when it is read.')
        if dialect in STATEMENT_WORD_ENGINES and t.up in STATEMENT_WORDS:
            raise ReadOnlyViolation(f'{t.up} is not allowed: only one read-only query can run.')
        if dialect in ('mssql', None) and t.up in LOCK_HINTS:
            raise ReadOnlyViolation(f'{t.up} is not allowed: lock hints make a read hold locks.')
        if dialect == 'mssql' and t.up in MSSQL_WORDS:
            raise ReadOnlyViolation(f'{t.up} is not allowed: only one read-only query can run.')
        if not call:
            continue

        # ---- a name followed by a bracket: a function call, unless it is one of these
        if i in cte_names:
            continue                                # WITH name (columns) AS ( ... )
        if is_op(prev, '::'):
            continue                                # PostgreSQL cast: x::numeric(10, 2)
        if prev is not None and (is_op(prev, ')') or prev.kind == 'ident' and prev.up == 'AS'):
            continue                                # an alias with its column list, or the type of a CAST
        if is_op(at(i + 2), '+') and is_op(at(i + 3), ')'):
            continue                                # Oracle's outer join marker: b.id (+)
        if is_op(prev, '.'):
            raise ReadOnlyViolation(
                f'{_qualified(toks, i)}() is not allowed: a function with a schema or package in front of it '
                f'cannot be called here.')
        if t.up in BRACKET_WORDS or t.up in types:
            continue
        if t.up not in allowed:
            raise ReadOnlyViolation(
                f'{t.text}() is not on the list of functions that may be used here. Only built-in functions that '
                f'read their arguments are allowed; your own functions are not, because a function can change data. '
                f'(The list is app/analysis/sql_functions.json.)')

    if names is not None and dialect in SCOPED:
        if dialect == 'oracle' and names.callable is None:
            # fail closed: without the list a name cannot be told from a call, and there is no weaker check
            raise ReadOnlyViolation('The names of what this Oracle database can call (functions, packages, types, '
                                    'schemas) have not been read, so no name can be checked. Refresh the source.')
        _Names(toks, dialect, names).run()

    clean, at_char = [], 0
    for t in every:
        if t.kind == 'comment':
            clean.append(text[at_char:t.start] + ' ')
            at_char = t.end
    clean.append(text[at_char:])
    return Checked(''.join(clean).strip().rstrip(';').rstrip(), toks, dialect)


def _outside_strings(text, dialect):
    """The statement without its strings, quoted names and comments (to look for a character that
    must not appear in code)."""
    return ' '.join(t.text for t in tokenize(text, dialect) if t.kind in ('ident', 'op', 'number'))


def _qualified(toks, i):
    parts = [toks[i].text]
    while i >= 2 and toks[i - 1].kind == 'op' and toks[i - 1].text == '.' and toks[i - 2].kind in ('ident', 'qident'):
        parts.insert(0, toks[i - 2].text)
        i -= 2
    return '.'.join(parts)


def _closing(toks, i):
    """The index of the bracket that closes the one at i, or None."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].kind == 'op' and toks[j].text == '(':
            depth += 1
        elif toks[j].kind == 'op' and toks[j].text == ')':
            depth -= 1
            if depth == 0:
                return j
    return None


def _ctes(toks):
    """({index of a CTE's name token}, {names its column list declares}) for every WITH clause:
    WITH [RECURSIVE] name [(columns)] AS [NOT] [MATERIALIZED] ( ... ) [, name ...]."""
    names, columns = set(), set()
    for i, t in enumerate(toks):
        if t.kind != 'ident' or t.up != 'WITH':
            continue
        j = i + 1
        if j < len(toks) and toks[j].up == 'RECURSIVE':
            j += 1
        while j < len(toks) and toks[j].kind in ('ident', 'qident'):
            name, k, listed = j, j + 1, []
            if k < len(toks) and toks[k].kind == 'op' and toks[k].text == '(':
                end = _closing(toks, k)
                if end is None:
                    break
                listed = [x for x in toks[k + 1:end] if x.kind in ('ident', 'qident')]
                k = end + 1
            if not (k < len(toks) and toks[k].up == 'AS'):
                break
            k += 1
            if k < len(toks) and toks[k].up == 'NOT':
                k += 1
            if k < len(toks) and toks[k].up == 'MATERIALIZED':
                k += 1
            if not (k < len(toks) and toks[k].kind == 'op' and toks[k].text == '('):
                break
            end = _closing(toks, k)
            if end is None:
                break
            names.add(name)
            columns.update(x.up for x in listed)
            columns.add(toks[name].up)
            j = end + 1
            if j < len(toks) and toks[j].kind == 'op' and toks[j].text == ',':
                j += 1
            else:
                break
    return names, columns


# ------------------------------------------------------- what a source knows ----

class Known:
    """What a source knows, for judging names (Oracle and PostgreSQL): its tables with their columns,
    spelled as the catalog spells them, and - Oracle - the names of everything the database can call
    (functions, procedures, packages, types, operators and the synonyms for them) and of its schemas.

        Known({'TELECOM.CUSTOMERS': ['CUSTOMER_ID', 'CITY'], 'NOTES': ['ID']}, callable=['X_PING'], schemas=['HR'])

    callable=None means "never read": on Oracle check() then refuses every statement."""

    def __init__(self, tables=None, callable=None, schemas=()):
        self.tables = {}                                # (schema or None, table) -> its columns
        for key, columns in (tables or {}).items():
            if not isinstance(key, tuple):
                schema, _, name = str(key).rpartition('.')
                key = (schema or None, name)
            self.tables[key] = frozenset(str(c) for c in columns)
        self._by_name = {}
        for (_, name), columns in self.tables.items():
            self._by_name.setdefault(name, []).append(columns)
        self.callable = None if callable is None else frozenset(str(n) for n in callable)
        self.schemas = frozenset(str(s) for s in schemas) | {s for s, _ in self.tables if s}

    @classmethod
    def of(cls, model, catalog=None):
        """From a source's structure (a SchemaModel) and what its catalog said can be called.
        A table that is only known as the target of a foreign key has not been read: it is left out."""
        catalog = catalog or {}
        tables = {(t.schema or None, t.name): [c.name for c in t.columns] for t in model.tables
                  if any(o.kind != 'ref' for o in t.origins)}
        return cls(tables, catalog.get('callable'), catalog.get('schemas') or ())

    def columns(self, schema, name):
        """The columns of a table, or None when the source does not know the table. Without a schema:
        the columns that every table of that name has - the session decides which one is meant."""
        if schema is not None:
            return self.tables.get((schema, name))
        sets = self._by_name.get(name)
        return frozenset.intersection(*sets) if sets else None


# ------------------------------------------------------------ names in scope ----

def _is(tok, text):
    return tok is not None and tok.kind == 'op' and tok.text == text


def _kw(tok, *words):
    return tok is not None and tok.kind == 'ident' and tok.up in words


class _Source:
    """One table of a FROM clause: the names it answers to in front of a dot, and its columns
    (None = not known: a table outside the source's structure, a function, SELECT * of one of those)."""
    __slots__ = ('names', 'columns', 'schema', 'table')

    def __init__(self):
        self.names, self.columns, self.schema, self.table = set(), None, None, None


class _Block:
    """One SELECT: the tables of its FROM and the column aliases of its select list."""
    __slots__ = ('sources', 'aliases')

    def __init__(self):
        self.sources, self.aliases = [], set()


class _Names:
    """Every name of a statement, judged where it stands.

    A statement is read as query blocks: each SELECT at its own bracket level, each branch of
    UNION / INTERSECT / MINUS on its own. A block knows the tables of its FROM clause and what
    columns each gives - from the source's structure, or from the select list of a subquery or WITH
    query. A subquery in an expression also sees the blocks around it; a subquery in FROM sees only
    itself (unless it is LATERAL), as on the server.

    Oracle       a bare name must be a column of a table in scope - or a column alias, in the ORDER BY
                 of its own block and nowhere else. In front of a dot only a table or alias in scope
                 may stand. A word of the language counts as one only where the syntax puts it.
                 Nothing - no name, no alias, no qualifier - may be called like something the
                 database can call, or like a schema.
    PostgreSQL   in t.name, t must be a table or alias in scope and name one of its columns.

    What cannot be followed is refused, never guessed."""

    def __init__(self, toks, dialect, known):
        self.toks, self.known, self.oracle = toks, known, dialect == 'oracle'
        self.match, stack = {}, []
        for i, t in enumerate(toks):
            if _is(t, '('):
                stack.append(i)
            elif _is(t, ')'):
                if not stack:
                    raise ReadOnlyViolation('A ) has no ( in front of it.')
                self.match[stack.pop()] = i
        if stack:
            raise ReadOnlyViolation('A ( is never closed.')
        self.taken = (known.callable or frozenset()) if self.oracle else frozenset()
        self.schemas = known.schemas if self.oracle else frozenset()
        # the words this engine does not take for a name: no table, no alias of a table or of a column is one of them
        self.reserved = RESERVED.get(dialect, RESERVED_ANYWHERE)
        self.needs_as = NEEDS_AS.get(dialect, frozenset())

    def run(self):
        self._query(0, len(self.toks), (), {})

    # ---------------------------------------------------------------- helpers
    def _at(self, i):
        return self.toks[i] if 0 <= i < len(self.toks) else None

    def _fold(self, tok):
        """A name as the engine keeps it: unquoted, upper case on Oracle and lower case on PostgreSQL."""
        if tok.kind == 'qident':
            return tok.text
        return tok.up if self.oracle else tok.text.lower()

    def _unread(self, i, what='statement'):
        tok = self._at(i)
        near = f' near "{tok.text}"' if tok is not None else ''
        raise ReadOnlyViolation(
            f'The guard could not follow this {what}{near}. It works out which table every name belongs to, because '
            f'on this engine a name can be a function call without brackets - and it refuses what it cannot follow. '
            f'Write the query more simply.')

    def _word(self, tok):
        return tok.kind == 'ident' and (tok.up in ORACLE_WORDS or tok.up in self.reserved)

    def _ends(self, tok):
        """True when an expression can end with this token - so a name behind it is an alias."""
        if tok is None:
            return False
        if tok.kind == 'op':
            return tok.text in (')', ']')
        if tok.kind != 'ident':
            return tok.kind in ('number', 'string', 'qident')
        return tok.up in ENDS_AN_EXPRESSION or not self._word(tok)

    def _is_query(self, a, b):
        while a < b and _is(self.toks[a], '('):
            a += 1
        return a < b and _kw(self.toks[a], 'SELECT', 'WITH')

    def _chain(self, a, b):
        """The names of tokens a..b when they are name[.name ...] and nothing else, otherwise None."""
        body = self.toks[a:b]
        if len(body) % 2 == 0 or any(not _is(x, '.') if k % 2 else x.kind not in ('ident', 'qident')
                                     for k, x in enumerate(body)):
            return None
        return body[::2]

    def _declares(self, tok, what):
        """An alias (of a table, a column, a WITH query) may not have the name of something callable."""
        name = self._fold(tok)
        if name in self.taken or name in self.schemas:
            raise ReadOnlyViolation(
                f'{tok.text} is used as a {what}, but it is also the name of {self._what_is(name)} in this database: '
                f'rename the alias. On Oracle a name can be a function call without brackets, so an alias must not '
                f'be called like something that can be called.')

    def _what_is(self, name):
        return ('something that can be called (a function, procedure, package, type or a synonym for one)'
                if name in self.taken else 'a schema')

    # ----------------------------------------------------------------- blocks
    def _query(self, lo, hi, outer, ctes):
        """SELECT ... [UNION SELECT ...] between lo and hi. Returns the columns it gives (None = not known)."""
        toks = self.toks
        while lo < hi and _is(toks[lo], '(') and self.match[lo] == hi - 1:
            lo, hi = lo + 1, hi - 1
        if lo >= hi:
            self._unread(lo)
        if _kw(toks[lo], 'WITH'):
            ctes = dict(ctes)
            lo = self._with(lo, hi, ctes)
        branches, start, i = [], lo, lo
        while i < hi:
            if _is(toks[i], '('):
                i = self.match[i] + 1
                continue
            if _kw(toks[i], *SET_OPERATORS) and not _is(self._at(i - 1), '.'):
                branches.append((start, i))
                i += 1
                if i < hi and _kw(toks[i], 'ALL', 'DISTINCT'):
                    i += 1
                start = i
                continue
            i += 1
        branches.append((start, hi))
        gives = named = None
        for n, (a, b) in enumerate(branches):
            # the ORDER BY behind a UNION sorts the whole of it, by the names of the first branch
            order = None if len(branches) == 1 else named if n == len(branches) - 1 else frozenset()
            columns, names = self._branch(a, b, outer, ctes, order)
            if n == 0:
                gives, named = columns, names
        return gives

    def _branch(self, a, b, outer, ctes, order):
        toks = self.toks
        if a >= b:
            self._unread(a)
        if _is(toks[a], '('):                           # ( SELECT ... ) [ORDER BY ...]
            j = self.match[a]
            columns = self._query(a + 1, j, outer, ctes)
            names = columns or frozenset()
            if j + 1 < b:
                self._judge(j + 1, b, (), ctes, 'ORDER', names if order is None else order)
            return columns, names
        if _kw(toks[a], 'SELECT'):
            return self._block(a, b, outer, ctes, order)
        if _kw(toks[a], 'VALUES') and not self.oracle:
            self._judge(a + 1, b, outer, ctes, None)
            return None, frozenset()
        self._unread(a)

    def _with(self, lo, hi, ctes):
        """WITH [RECURSIVE] name [(columns)] AS ( query ) [, ...]: fills ctes {name: columns} and returns
        where the query itself starts."""
        toks, i = self.toks, lo + 1
        if i < hi and _kw(toks[i], 'RECURSIVE'):
            i += 1
        while True:
            name = toks[i] if i < hi else None
            if name is None or name.kind not in ('ident', 'qident'):
                self._unread(i, 'WITH clause')
            if self.oracle:
                self._declares(name, 'name of a WITH query')
            i += 1
            listed = None
            if i < hi and _is(toks[i], '('):
                j = self.match[i]
                listed = [x for x in toks[i + 1:j] if x.kind in ('ident', 'qident')]
                for column in listed if self.oracle else ():
                    self._declares(column, 'column name of a WITH query')
                i = j + 1
            if not (i < hi and _kw(toks[i], 'AS')):
                self._unread(i, 'WITH clause')
            i += 1
            if i < hi and _kw(toks[i], 'NOT'):
                i += 1
            if i < hi and _kw(toks[i], 'MATERIALIZED'):
                i += 1
            if not (i < hi and _is(toks[i], '(')):
                self._unread(i, 'WITH clause')
            j = self.match[i]
            key = self._fold(name)
            declared = frozenset(self._fold(x) for x in listed) if listed is not None else None
            inside = dict(ctes)
            inside[key] = declared                      # a recursive query reads itself
            columns = self._query(i + 1, j, (), inside)
            ctes[key] = declared if declared is not None else columns
            i = j + 1
            if i < hi and _is(toks[i], ','):
                i += 1
                continue
            return i

    def _clauses(self, lo, hi):
        """[(word, start, end)]: the clauses of one query block, SELECT first, as they stand."""
        toks, out, word, start, i = self.toks, [], 'SELECT', lo + 1, lo + 1
        while i < hi:
            t = toks[i]
            if _is(t, '('):
                i = self.match[i] + 1
                continue
            prev = toks[i - 1]
            if t.kind == 'ident' and not _is(prev, '.'):
                u, new = t.up, None
                if u == 'FROM':
                    if word == 'SELECT' and not (_kw(prev, 'DISTINCT') and _kw(self._at(i - 2), 'IS', 'NOT')):
                        new = u
                elif u in ('WHERE', 'HAVING', 'ORDER') or u == 'GROUP' and not _kw(prev, 'WITHIN'):
                    new = u
                elif u in ('CONNECT', 'START') and self.oracle:
                    new = u
                elif u == 'FETCH' and _kw(self._at(i + 1), 'FIRST', 'NEXT'):
                    new = u
                elif u == 'OFFSET' and (not self.oracle or word != 'SELECT' and (
                        self._ends(prev) or _kw(self._at(i + 2), 'ROW', 'ROWS'))):       # OFFSET 5 ROWS
                    new = u
                elif u in ('LIMIT', 'WINDOW') and not self.oracle:
                    new = u
                if new:
                    out.append((word, start, i))
                    word, start = new, i + 1
            i += 1
        out.append((word, start, hi))
        return out

    def _block(self, lo, hi, outer, ctes, order):
        """One SELECT: its tables first, then every name in it. Returns (the columns it gives or None,
        the names an ORDER BY behind a UNION may use)."""
        block = _Block()
        clauses = self._clauses(lo, hi)
        conditions = []
        for word, a, b in clauses:
            if word == 'FROM':
                conditions = self._from(a, b, block, outer, ctes)
        here = (block,) + outer
        items, more = self._items(clauses[0][1], clauses[0][2])
        for a, end, alias, star, name in items:
            if alias is not None:
                if self.oracle:
                    self._declares(alias, 'column alias')
                block.aliases.add(self._fold(alias))
        columns = self._gives(items, block)
        for a, b in more:
            self._judge(a, b, here, ctes, 'SELECT')
        for a, end, alias, star, name in items:
            self._judge(a, end, here, ctes, 'SELECT')
        for a, b in conditions:
            self._judge(a, b, here, ctes, 'ON')
        for word, a, b in clauses[1:]:
            if word == 'FROM':
                continue
            if word != 'ORDER':
                self._judge(a, b, here, ctes, word)
            elif order is None:
                self._judge(a, b, here, ctes, word, frozenset(block.aliases))
            else:
                self._judge(a, b, (), ctes, word, order)
        return columns, frozenset(block.aliases) | (columns or frozenset())

    # ------------------------------------------------------------ select list
    def _items(self, lo, hi):
        """The select list: ([(start, end of the expression, alias, star, name)], [other expressions])."""
        toks, more = self.toks, []
        if lo < hi and _kw(toks[lo], 'DISTINCT', 'UNIQUE', 'ALL'):
            lo += 1
            if lo + 1 < hi and _kw(toks[lo], 'ON') and _is(toks[lo + 1], '('):      # PostgreSQL: DISTINCT ON ( ... )
                more.append((lo + 2, self.match[lo + 1]))
                lo = self.match[lo + 1] + 1
        spans, start, i = [], lo, lo
        while i < hi:
            if _is(toks[i], '('):
                i = self.match[i] + 1
                continue
            if _is(toks[i], ','):
                spans.append((start, i))
                start = i + 1
            i += 1
        spans.append((start, hi))
        return [self._item(a, b) for a, b in spans], more

    def _item(self, a, b):
        toks = self.toks
        if a >= b:
            self._unread(a, 'select list')
        last, alias, end = toks[b - 1], None, b
        if b - a >= 2 and last.kind in ('ident', 'qident') and not _kw(last, *self.reserved):
            prev = toks[b - 2]
            if _kw(prev, 'AS'):
                alias, end = last, b - 2
            elif not _is(prev, '.') and self._ends(prev) and not self._closes(a, b) and not _kw(last, *self.needs_as):
                alias, end = last, b - 1
        if a >= end:
            self._unread(a, 'select list')
        star = name = None
        if _is(toks[end - 1], '*') and (end - a == 1 or self._chain(a, end - 2) is not None and _is(toks[end - 2], '.')):
            star = self._chain(a, end - 2) if end - a > 1 else []
        elif alias is not None:
            name = alias
        else:
            chain = self._chain(a, end)                 # a plain column gives its own name
            name = chain[-1] if chain else None
        return a, end, alias, star, name

    def _closes(self, a, b):
        """True when the last word of a select item ends its expression instead of naming it."""
        toks = self.toks
        last, prev = toks[b - 1], toks[b - 2]
        if _kw(last, 'END'):                            # ... END: of a CASE that is still open
            open_cases, i = 0, a
            while i < b - 1:
                if _is(toks[i], '('):
                    i = self.match[i] + 1
                    continue
                open_cases += 1 if _kw(toks[i], 'CASE') else -1 if _kw(toks[i], 'END') else 0
                i += 1
            return open_cases > 0
        return _kw(last, *DATE_FIELDS) and (prev.kind == 'string' or _kw(prev, 'TO'))      # INTERVAL '1' DAY

    def _gives(self, items, block):
        """The column names a select list gives, or None when a * hides them."""
        names = set()
        for a, end, alias, star, name in items:
            if star is not None:
                wanted = self._fold(star[-1]) if star else None
                found = [s for s in block.sources if wanted is None or wanted in s.names]
                if not found or any(s.columns is None for s in found):
                    return None
                for source in found:
                    names |= source.columns
            elif name is not None:
                names.add(self._fold(name))
        return frozenset(names)

    # ------------------------------------------------------------------- FROM
    def _from(self, lo, hi, block, outer, ctes):
        """The tables of a FROM clause, into block.sources. Returns the ON conditions [(start, end)]:
        they are judged when every table of the block is known."""
        toks, conditions, i, item, lateral = self.toks, [], lo, True, False
        while i < hi:
            t = toks[i]
            if item:
                # LATERAL and ONLY in front of a table - where the engine has such a word. Where it has not
                # (SQL Server, SQLite; MySQL for ONLY) it is the name of a table: FROM only store is "only", called store.
                if _kw(t, 'LATERAL') and (self.oracle or t.up in self.reserved):
                    lateral, i = True, i + 1
                    continue
                if _kw(t, 'ONLY') and not self.oracle and t.up in self.reserved and not _is(self._at(i + 1), '('):
                    i += 1
                    continue
                # a subquery in FROM sees nothing of this block - unless it is LATERAL: then the tables before it
                around = (block,) + outer if lateral else ()
                source = _Source()
                if _is(t, '('):
                    j = self.match[i]
                    if self._is_query(i + 1, j):
                        source.columns = self._query(i + 1, j, around, ctes)
                    elif _kw(self._at(i + 1), 'VALUES') and not self.oracle:
                        self._judge(i + 2, j, around, ctes, 'FROM')
                    else:                               # a join in brackets: its tables are tables of this block
                        conditions += self._from(i + 1, j, block, outer, ctes)
                        source = None
                    i = j + 1
                elif t.kind == 'qident' or t.kind == 'ident' and (t.up == 'TABLE' or t.up not in self.reserved):
                    parts, k = [t], i + 1
                    while k + 1 < hi and _is(toks[k], '.') and toks[k + 1].kind in ('ident', 'qident'):
                        parts.append(toks[k + 1])
                        k += 2
                    if k < hi and _is(toks[k], '('):    # something that gives rows: TABLE( ... ), generate_series( ... )
                        j = self.match[k]
                        self._judge(k + 1, j, (block,) + outer, ctes, 'FROM')
                        if not _kw(t, 'TABLE'):
                            source.names = {self._fold(parts[-1])}
                        i = j + 1
                    else:
                        source = self._table(parts, ctes)
                        i = k
                else:
                    self._unread(i, 'FROM clause')
                lateral = False
                if source is not None:
                    i = self._after(i, hi, source)
                    block.sources.append(source)
                item = False
                continue
            if _is(t, ','):
                item, i = True, i + 1
                continue
            if _kw(t, *JOIN_WORDS):
                if t.up in ('JOIN', 'APPLY'):
                    item, lateral = True, t.up == 'APPLY'
                i += 1
                continue
            if _kw(t, 'ON'):
                j = i + 1
                while j < hi and not _is(toks[j], ',') and not (
                        _kw(toks[j], *JOIN_WORDS) and not _is(toks[j - 1], '.') and not _is(self._at(j + 1), '(')):
                    j = self.match[j] + 1 if _is(toks[j], '(') else j + 1
                conditions.append((i + 1, j))
                i = j
                continue
            if _kw(t, 'USING') and i + 1 < hi and _is(toks[i + 1], '('):
                i = self.match[i + 1] + 1               # column names, by the syntax: never evaluated
                continue
            self._unread(i, 'FROM clause')
        if item:
            self._unread(hi - 1, 'FROM clause')
        return conditions

    def _table(self, parts, ctes):
        source = _Source()
        names = [self._fold(p) for p in parts]
        source.table, source.schema = names[-1], names[-2] if len(names) > 1 else None
        source.names = {names[-1]}
        if len(names) == 1 and names[0] in ctes:
            source.columns, source.table = ctes[names[0]], None
        elif self.oracle and names == ['DUAL'] and self.known.columns(None, 'DUAL') is None:
            source.columns = frozenset({'DUMMY'})
        else:
            source.columns = self.known.columns(source.schema, source.table)
        return source

    def _after(self, i, hi, source):
        """What follows a table in FROM - PARTITION ( ... ), SAMPLE ( ... ), its alias - up to the next
        table or condition. Returns where that is."""
        toks, named = self.toks, False
        while i < hi:
            t = toks[i]
            if _is(t, ','):
                break
            if t.kind == 'ident':
                u = t.up
                if u in JOIN_WORDS or u in ('ON', 'USING'):
                    break
                if u in ('PARTITION', 'SUBPARTITION') and i + 1 < hi and _is(toks[i + 1], '(') and not named:
                    i = self.match[i + 1] + 1           # the name of a partition: never evaluated
                    continue
                if u in ('SAMPLE', 'TABLESAMPLE'):
                    i += 1
                    while i < hi and (_kw(toks[i], 'BLOCK', 'SEED', 'REPEATABLE', 'SYSTEM', 'BERNOULLI') or _is(toks[i], '(')):
                        if _is(toks[i], '('):
                            self._judge(i + 1, self.match[i], (), {}, 'FROM')       # a number: any name is refused
                            i = self.match[i]
                        i += 1
                    continue
                if u == 'WITH' and _kw(self._at(i + 1), 'ORDINALITY'):
                    i += 2
                    continue
                if u == 'AS' and not named:
                    nxt = toks[i + 1] if i + 1 < hi else None
                    if nxt is None or nxt.kind not in ('ident', 'qident') or _kw(nxt, 'OF'):
                        self._unread(i, 'FROM clause')
                    i += 1
                    t = nxt
                elif u in FROM_NOT_READ or u in self.reserved:
                    raise ReadOnlyViolation(
                        f'{u} in a FROM clause is not something the guard can follow: it could not tell which columns '
                        f'the tables give, and on this engine a name can be a function call without brackets.')
            if t.kind in ('ident', 'qident') and not named:
                if self.oracle:
                    self._declares(t, 'table alias')
                source.names, source.table, named, i = {self._fold(t)}, None, True, i + 1
                if i < hi and _is(toks[i], '('):
                    if self.oracle:
                        self._unread(i, 'FROM clause')
                    j, columns, first = self.match[i], [], True     # PostgreSQL: AS t (a, b) or AS t (a int, b text)
                    k = i + 1
                    while k < j:
                        if _is(toks[k], '('):
                            k = self.match[k] + 1
                            continue
                        if _is(toks[k], ','):
                            first = True
                        elif first and toks[k].kind in ('ident', 'qident'):
                            columns.append(self._fold(toks[k]))
                            first = False
                        k += 1
                    source.columns = frozenset(columns)
                    i = j + 1
                continue
            self._unread(i, 'FROM clause')
        return i

    # ------------------------------------------------------------ expressions
    def _judge(self, lo, hi, chain, ctes, clause, aliases=frozenset(), fn=None):
        """Every name between lo and hi. chain: the query blocks whose tables are in scope, the nearest
        first. aliases: the column aliases that may be used here (in a block's own ORDER BY).
        fn: the word in front of the bracket this stands in."""
        toks, i = self.toks, lo
        while i < hi:
            t = toks[i]
            if _is(t, '('):
                j = self.match[i]
                if self._is_query(i + 1, j):
                    self._query(i + 1, j, chain, ctes)  # a subquery in an expression sees the blocks around it
                else:
                    head = self._at(i - 1)
                    name = head.up if head is not None and head.kind == 'ident' and not _is(self._at(i - 2), '.') else None
                    end = j
                    if name in TYPE_CALLS:              # CAST(x AS NUMBER(10)): what follows AS is a type, not a name
                        end = i + 1
                        while end < j and not _kw(toks[end], 'AS'):
                            end = self.match[end] + 1 if _is(toks[end], '(') else end + 1
                    # OVER ( ... ORDER BY ), WITHIN GROUP ( ORDER BY ), KEEP ( ... ): not the block's own ORDER BY
                    inner = frozenset() if name in ('OVER', 'GROUP', 'KEEP') else aliases
                    self._judge(i + 1, min(end, j), chain, ctes, clause, inner, name)
                    if not self.oracle and _is(self._at(j + 1), '.') and not _is(self._at(j + 2), '*'):
                        self._field(i, j, chain)
                i = j + 1
                continue
            if t.kind not in ('ident', 'qident'):
                i += 1
                continue
            prev = self._at(i - 1)
            if _is(prev, '.'):
                if not self.oracle and _is(self._at(i - 2), ']'):
                    raise ReadOnlyViolation(f'[ ... ].{t.text} is not allowed: on PostgreSQL it can call a function '
                                            f'{t.text}( ... ).')
                i += 1                                  # ( ... ).name was judged at its bracket
                continue
            parts, k = [t], i + 1
            while _is(self._at(k), '.') and self._at(k + 1) is not None and (
                    self._at(k + 1).kind in ('ident', 'qident') or _is(self._at(k + 1), '*')):
                parts.append(toks[k + 1])
                k += 2
            if _is(prev, '::'):
                i = k                                   # PostgreSQL: x::type
                continue
            if _is(self._at(k), '(') and not (_is(self._at(k + 1), '+') and _is(self._at(k + 2), ')')):
                i = k                                   # a call: the list of functions judged it; its arguments are next
                continue
            if len(parts) > 1:
                self._dotted(parts, chain)
            elif self.oracle:
                self._bare(i, chain, clause, aliases, fn)
            i = k

    def _find(self, name, chain):
        for block in chain:
            for source in block.sources:
                if name in source.names:
                    return source
        return None

    def _dotted(self, parts, chain):
        """a.b[.c]: a must be a table or an alias in scope (or schema.table of one)."""
        head, member = parts[0], parts[1]
        name, written = self._fold(head), '.'.join(p.text for p in parts)
        source = self._find(name, chain)
        if source is None and len(parts) >= 3:          # schema.table.column, of a table that stands in FROM unaliased
            table = self._fold(parts[1])
            columns = self.known.columns(name, table)
            listed = next((s for b in chain for s in b.sources if s.table == table and s.schema in (None, name)), None)
            if listed is not None and columns is not None:
                if _is(parts[2], '*') or self._fold(parts[2]) in columns and (self.oracle or len(parts) == 3):
                    return
                raise ReadOnlyViolation(self._not_a_column(written, parts[-1].text, f'{head.text}.{parts[1].text}'))
        if source is None:
            raise ReadOnlyViolation(
                f'{head.text} is not a table or an alias of this part of the query, so {written} could be a function '
                f'call: ' + ('on Oracle a function is called without brackets, and a function can change data. '
                             if self.oracle else 'on PostgreSQL t.name calls a function name(t). ')
                + 'A table alias is known inside its own SELECT and in the subqueries of its expressions - not in a '
                  'subquery in FROM, and not in another branch of a UNION.')
        if self.oracle:
            if name in self.taken or name in self.schemas:
                raise ReadOnlyViolation(
                    f'{head.text} is a table of this query, but it is also the name of {self._what_is(name)} in this '
                    f'database, so the guard cannot let it stand in front of a dot. Give the table an alias '
                    f'(FROM {head.text} t) and write t.{member.text}.')
            return                                      # alias.x is a column of that table or an error, never a call
        if _is(member, '*'):
            return
        if source.columns is None:
            raise ReadOnlyViolation(
                f'{written}: the columns of {head.text} are not known to this source, so the guard cannot tell a column '
                f'from a function call - on PostgreSQL t.name calls a function name(t) when t has no such column. Write '
                f'{member.text} without the table in front of it, or add the schema of that table to the source.')
        if self._fold(member) not in source.columns or len(parts) > 2:      # t.column.name: a call of name(t.column)
            raise ReadOnlyViolation(self._not_a_column(written, parts[-1].text, head.text))

    @staticmethod
    def _not_a_column(written, member, table):
        return (f'{written}: {member} is not a column of {table} as this source knows it. On PostgreSQL t.name calls a '
                f'function name(t) when t has no such column, and a function can do anything. (Refresh the source if '
                f'the table has changed.)')

    def _field(self, i, j, chain):
        """PostgreSQL: ( ... ).name is a field of a row - or a call of name( ... )."""
        member = self.toks[j + 2]
        end = j - 2 if _is(self.toks[j - 1], '*') and _is(self.toks[j - 2], '.') else j      # (t.*).name
        names = self._chain(i + 1, end)
        if member.kind not in ('ident', 'qident') or names is None:
            raise ReadOnlyViolation(
                f'( ... ).{member.text} is not allowed: on PostgreSQL it can call a function {member.text}( ... ), and '
                f'the guard cannot know whether the row has a column of that name. Only (table).column is accepted.')
        self._dotted(names + [member], chain)

    def _bare(self, i, chain, clause, aliases, fn):
        """Oracle: a name with nothing in front of it. It is a column in scope, or it is refused."""
        tok = self.toks[i]
        if tok.kind == 'ident' and (tok.up in ORACLE_NEVER_A_CALL or self._as_word(i, clause, fn)):
            return
        name = self._fold(tok)
        if name in self.taken or name in self.schemas:
            raise ReadOnlyViolation(
                f'{tok.text} is the name of {self._what_is(name)} in this database. On Oracle a function is called '
                f'without brackets, so the guard cannot let that name stand alone, even where a column is called the '
                f'same. Write the column with its table alias: t.{tok.text}.')
        if name in aliases:
            return                                      # a column alias, in the ORDER BY of its own query block
        unknown = False
        for block in chain:
            for source in block.sources:
                if source.columns is None:
                    unknown = True
                elif name in source.columns:
                    return
        prev = self._at(i - 1)
        hint = (' If it is meant as an alias, write AS in front of it.'
                if prev is not None and prev.kind == 'ident' and self._word(prev) and not _kw(prev, 'AS') else '')
        if unknown:
            raise ReadOnlyViolation(
                f'{tok.text} is not a column of a table this source knows in this part of the query. If it is a column '
                f'of a table outside the loaded schemas, write it with its table alias (t.{tok.text}). Bare names are '
                f'checked on Oracle because a function can be called there without brackets, and a function can '
                f'change data.' + hint)
        raise ReadOnlyViolation(
            f'{tok.text} is not a column of the tables in this part of the query, so it could be a function call: on '
            f'Oracle a function can be called without brackets, and a function can change data. A table alias or the '
            f'name of a WITH query can only stand in front of a dot (t.column), and a column alias can only be used in '
            f'the ORDER BY of its own SELECT.' + hint)

    def _as_word(self, i, clause, fn):
        """True when the word at i stands where the syntax wants that word. Everywhere else it is a name:
        Oracle lets a function be called FIRST, YEAR or CASE and calls it without brackets."""
        toks = self.toks
        tok, prev, nxt = toks[i], self._at(i - 1), self._at(i + 1)
        u = tok.up
        p = prev.up if prev is not None and prev.kind == 'ident' else None
        n = nxt.up if nxt is not None and nxt.kind == 'ident' else None
        after = self._ends(prev)                        # an expression ends in front of it
        if u in ('TIMESTAMP', 'INTERVAL'):
            return nxt is not None and nxt.kind == 'string'
        if u in DATE_FIELDS:                            # EXTRACT(YEAR FROM x), INTERVAL '1' DAY, ... YEAR TO MONTH
            return fn == 'EXTRACT' and _is(prev, '(') and n == 'FROM' or p == 'TO' or prev is not None and prev.kind == 'string'
        if u == 'CASE':                                 # CASE ... WHEN, before the expression ends
            k = i + 1
            while k < len(toks) and not _is(toks[k], ')') and not _is(toks[k], ','):
                if _kw(toks[k], 'WHEN'):
                    return True
                k = self.match[k] + 1 if _is(toks[k], '(') else k + 1
            return False
        if u == 'WHEN':
            return after or p == 'CASE'
        if u == 'END':
            return after
        if u in ('BOTH', 'LEADING', 'TRAILING'):
            return fn == 'TRIM' and _is(prev, '(')
        if u == 'DENSE_RANK':
            return fn == 'KEEP' and _is(prev, '(')
        if u in ('FIRST', 'LAST'):
            return p in ('NULLS', 'DENSE_RANK') or u == 'FIRST' and p == 'FETCH'
        if u == 'NEXT':
            return p == 'FETCH'
        if u == 'NULLS':
            return after or p in ('ASC', 'DESC', 'RESPECT', 'IGNORE')
        if u in ('RESPECT', 'IGNORE'):
            return n == 'NULLS'
        if u == 'UNBOUNDED':
            return fn == 'OVER' and n in ('PRECEDING', 'FOLLOWING')
        if u in ('PRECEDING', 'FOLLOWING'):
            return fn == 'OVER' and (after or p == 'UNBOUNDED' or p in DATE_FIELDS)
        if u in ('RANGE', 'GROUPS'):
            return fn == 'OVER' and (after or _is(prev, '('))
        if u == 'PARTITION':
            return fn == 'OVER' and n == 'BY'
        if u == 'SIBLINGS':
            return p == 'ORDER' and n == 'BY'
        if u == 'NOCYCLE':
            return clause == 'CONNECT' and p == 'BY'
        if u == 'ONLY':
            return p in ('ROWS', 'ROW')
        if u == 'PERCENT':
            return clause == 'FETCH' and after
        if u == 'TIES':
            return clause == 'FETCH' and p == 'WITH'
        if u == 'WITHIN':
            return after and n == 'GROUP'
        if u == 'AT':
            return after and n in ('TIME', 'LOCAL')
        if u == 'TIME':
            return p == 'AT' and n == 'ZONE'
        if u in ('ZONE', 'LOCAL'):
            return p == ('TIME' if u == 'ZONE' else 'AT')
        if u == 'ESCAPE':
            return after
        if u == 'GROUPING':
            return clause == 'GROUP' and n == 'SETS'
        return False
