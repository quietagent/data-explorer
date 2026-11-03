"""Every setting of SQL Lineage, in one place.

Change a value here and rerun the scan. Nothing else in the package holds a
number or a word list that changes behaviour.
"""

# ---------------------------------------------------------------------------
# Dialects
# ---------------------------------------------------------------------------

# The --dialect values accepted. Each is a sqlglot dialect name.
DIALECTS = ("oracle", "tsql", "postgres", "mysql", "sqlite")

# How names are folded into keys. "upper" and "lower" change the case;
# "upper" is also used for the case-blind dialects, so keys look the same.
NAME_FOLDING = {
    "oracle": "upper",
    "sqlite": "upper",
    "postgres": "lower",
    "tsql": "upper",
    "mysql": "upper",
}
# Dialects where a quoted name keeps the case it was written in.
QUOTED_NAMES_KEEP_CASE = ("postgres",)

# A table qualified with its dialect's default schema is the same table as the
# bare name: dbo.customer is customer in tsql. Remove an entry to keep them apart.
DEFAULT_SCHEMA = {
    "tsql": "dbo",
    "sqlite": "main",
    "postgres": "public",
}

# ---------------------------------------------------------------------------
# Walking the folder
# ---------------------------------------------------------------------------

# File endings read, and the kind of script each one is.
SCRIPT_EXTENSIONS = {".sql": "sql", ".py": "python"}

# Folders never entered.
SKIP_FOLDERS = (".git", ".svn", ".hg", "__pycache__", ".venv", "venv", "node_modules")

# A folder with more script files than this is refused with an error before
# any file is read.
MAX_FILES = 5000
# A script bigger than this many MB is not read. It is listed as unreadable
# (REASON_TOO_BIG below).
MAX_FILE_MB = 5

# Encodings tried in this order. UTF-8 also reads a file that starts with a BOM
# (recorded as "utf-8-sig"). Windows-1256 is Arabic, Windows-1252 is Western.
ENCODINGS = ("utf-8", "cp1256", "cp1252")
# A file that starts with one of these byte order marks is UTF-16 (SSMS can save
# scripts that way). UTF-16 without a mark is not guessed: it stays unreadable.
UTF16_BOMS = {b"\xff\xfe": "utf-16-le", b"\xfe\xff": "utf-16-be"}

# ---------------------------------------------------------------------------
# Splitting .sql files
# ---------------------------------------------------------------------------

# A line holding only one of these ends the statement before it.
# GO is the tsql batch separator, / is the Oracle SQL*Plus one.
SEPARATOR_LINES = ("GO", "/")

# Statements that start with these words are procedural blocks. They are kept
# whole (not split on ;) and listed as unreadable. Leading CREATE modifiers such
# as OR REPLACE, OR ALTER and EDITIONABLE are skipped before matching.
BLOCK_STARTS = {
    "oracle": ("DECLARE", "BEGIN", "CREATE PROCEDURE", "CREATE FUNCTION",
               "CREATE PACKAGE", "CREATE TRIGGER", "CREATE TYPE BODY"),
    "tsql": ("CREATE PROCEDURE", "CREATE PROC", "CREATE FUNCTION", "CREATE TRIGGER",
             "ALTER PROCEDURE", "ALTER PROC", "ALTER FUNCTION", "ALTER TRIGGER"),
    "mysql": ("CREATE PROCEDURE", "CREATE FUNCTION", "CREATE TRIGGER", "CREATE EVENT"),
    "sqlite": ("CREATE TRIGGER",),
    "postgres": (),  # Postgres bodies sit inside $$ strings, so ; splitting is safe.
}
# Words skipped between CREATE and the object type when matching BLOCK_STARTS.
CREATE_MODIFIERS = ("OR", "REPLACE", "ALTER", "EDITIONABLE", "NONEDITIONABLE",
                    "DEFINER", "TEMPORARY", "TEMP")
# Blocks that open at their first word (no BEGIN needed): Oracle package specs
# and bodies end with END name; without a BEGIN.
BLOCKS_OPEN_AT_START = ("CREATE PACKAGE", "CREATE TYPE BODY")
# BEGIN followed by one of these starts a transaction, not a block.
BEGIN_TRANSACTION_WORDS = ("TRANSACTION", "TRAN", "WORK", "DEFERRED", "IMMEDIATE",
                           "EXCLUSIVE", "DISTRIBUTED")

# Oracle SQL*Plus commands. At the start of a statement they run to the end of
# their line and need no ;. They are not SQL, so like comments they make no
# statement (SET SERVEROUTPUT ON is not in the statement list).
SQLPLUS_COMMANDS = ("SET", "SPOOL", "PROMPT", "REM", "REMARK", "START", "EXIT", "QUIT",
                    "WHENEVER", "COLUMN", "COL", "TTITLE", "BTITLE", "BREAK", "COMPUTE",
                    "DEFINE", "UNDEFINE", "ACCEPT", "PAUSE", "SHOW", "CONNECT", "CONN",
                    "DISCONNECT", "CLEAR", "HOST", "VARIABLE", "VAR", "PRINT",
                    "EXEC", "EXECUTE")
# Of those, the ones that call a procedure: kept as statements and listed as
# unreadable, since what the procedure reads and writes is not known.
SQLPLUS_CALL_COMMANDS = ("EXEC", "EXECUTE")

# ---------------------------------------------------------------------------
# Finding SQL in Python scripts
# ---------------------------------------------------------------------------

# A string passed to a call with one of these names is SQL. A dotted name
# (spark.sql) must match the end of the call: spark.sql(...), self.spark.sql(...).
# executescript may hold several statements; they are split like a .sql file.
PYTHON_SQL_CALLS = ("execute", "executemany", "executescript", "read_sql", "read_sql_query",
                    "exec_driver_sql", "spark.sql", "text")
# Keyword names under which those calls may receive the SQL.
PYTHON_SQL_KEYWORDS = ("sql", "query", "statement", "operation", "sql_script", "sqlQuery")
# Calls that read a whole table named by a string: recorded as SELECT * FROM it.
# The keyword names give the table and its schema when they are not positional.
PYTHON_TABLE_CALLS = ("read_sql_table",)
PYTHON_TABLE_KEYWORD = "table_name"
PYTHON_SCHEMA_KEYWORD = "schema"
# Any other string is SQL only when it starts with one of these words and
# sqlglot reads it as that kind of statement.
SQL_START_WORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE")
# Strings passed straight to calls with these names are messages, never SQL.
PYTHON_MESSAGE_CALLS = ("print", "debug", "info", "warning", "warn", "error",
                        "exception", "critical", "log", "echo", "write")
# How many times a variable is followed back to its assignment (a = b, b = "...").
PYTHON_MAX_VARIABLE_HOPS = 10

# ---------------------------------------------------------------------------
# Parsing statements
# ---------------------------------------------------------------------------

# Words that look like columns to sqlglot but are values of the database.
# Unqualified (SYSDATE) or after a sequence name (seq.NEXTVAL), they are not columns.
PSEUDO_COLUMNS = ("SYSDATE", "SYSTIMESTAMP", "ROWNUM", "ROWID", "LEVEL", "USER", "UID",
                  "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_USER",
                  "SESSION_USER", "LOCALTIMESTAMP", "NEXTVAL", "CURRVAL")
# One-row tables used to select values (SELECT 1 FROM DUAL). Not recorded as tables.
DUMMY_TABLES = ("DUAL",)
# Statements with no lineage that sqlglot reads only as a raw command, or not at all.
# They are recorded with their first word as the type, and no edges.
COMMANDS_WITHOUT_LINEAGE = ("VACUUM", "REINDEX", "CHECKPOINT", "SAVEPOINT", "RELEASE",
                            "BEGIN", "END", "PRINT", "RAISERROR", "DBCC", "LOCK", "UNLOCK")
# Statements that call a procedure: unreadable, since its body is not known.
PROCEDURE_CALL_WORDS = ("EXEC", "EXECUTE", "CALL")
# A statement that can't be read takes its first word as its type when it is one
# of these: f"DELETE FROM {table}" is a DELETE. Otherwise its type is UNKNOWN.
FAILED_TYPE_WORDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "MERGE")

# ---------------------------------------------------------------------------
# Reasons given for unreadable items. Tests and the report use these texts.
# ---------------------------------------------------------------------------

REASON_PLSQL = "PL/SQL block not read yet"
REASON_NAME_AT_RUN_TIME = "name built at run time"
REASON_SQL_AT_RUN_TIME = "SQL assembled at run time"
REASON_PROCEDURE_CALL = "procedure call: what it reads and writes is not known"
REASON_NOT_DECODED = "file could not be decoded"
REASON_TOO_BIG = "file larger than {mb} MB (MAX_FILE_MB), not read"

# ---------------------------------------------------------------------------
# Repeated join groups ("reduce")
# ---------------------------------------------------------------------------

# Biggest group of joins looked for inside one statement.
MAX_GROUP_JOINS = 4
# A group is reported when it appears in at least this many statements ...
MIN_GROUP_STATEMENTS = 3
# ... coming from at least this many different scripts.
MIN_GROUP_SCRIPTS = 2

# ---------------------------------------------------------------------------
# Impact
# ---------------------------------------------------------------------------

# How many levels of "this statement writes a table that another reads" to follow.
IMPACT_MAX_DEPTH = 10
