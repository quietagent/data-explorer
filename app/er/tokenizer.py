"""SQL tokenizer and keyword sets.

Copied from tools/sql_to_drawio.py and tools/sql_to_erd.py (identical in both).
"""

import re


TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>--[^\n]*|/\*.*?\*/)
  | (?P<string>[NnEe]?'(?:[^']|'')*')
  | (?P<qident>"(?:[^"]|"")*"|`[^`]*`|\[[^\]\r\n]*\])
  | (?P<number>\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)
  | (?P<ident>[^\W\d][\w$#]*)
  | (?P<op><=|>=|<>|!=|\|\||:=|::|=>|.)
""", re.S | re.X)

IDENT = ('ident', 'qident')


class Tok:
    __slots__ = ('kind', 'text', 'up')

    def __init__(self, kind, text):
        self.kind, self.text, self.up = kind, text, text.upper()


def tokenize(sql):
    toks = []
    for m in TOKEN_RE.finditer(sql):
        kind, text = m.lastgroup, m.group()
        if kind in ('ws', 'comment'):
            continue
        if kind == 'qident':
            text = text[1:-1].replace('""', '"') if text[0] == '"' else text[1:-1]
        toks.append(Tok(kind, text))
    return toks


# ---------------------------------------------------------------- keywords

CLAUSES = {'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'CONNECT', 'START',
           'QUALIFY', 'WINDOW', 'LIMIT', 'OFFSET', 'FETCH', 'INTO', 'FOR', 'MODEL'}
CLAUSE_NEXT = {'GROUP': 'BY', 'ORDER': 'BY', 'CONNECT': 'BY', 'START': 'WITH', 'FOR': 'UPDATE'}
SET_OPS = {'UNION', 'INTERSECT', 'MINUS', 'EXCEPT'}
JOIN_WORDS = {'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'CROSS', 'NATURAL',
              'LATERAL', 'APPLY', 'STRAIGHT_JOIN'}
NOT_ALIAS = CLAUSES | SET_OPS | JOIN_WORDS | {
    'ON', 'USING', 'AS', 'WITH', 'PARTITION', 'SUBPARTITION', 'SAMPLE', 'TABLESAMPLE',
    'PIVOT', 'UNPIVOT', 'USE', 'FORCE', 'IGNORE', 'WHEN', 'THEN', 'ELSE', 'END', 'SET',
    'VALUES', 'RETURNING', 'MATCH_RECOGNIZE', 'VERSIONS', 'NOLOCK'}
SQL_WORDS = set("""
ALL AND ANY AS ASC AT BETWEEN BOTH BY CASE CAST COLLATE CURRENT CURRENT_DATE CURRENT_TIME
CURRENT_TIMESTAMP CURRENT_USER DATE DAY DEFAULT DESC DISTINCT DUAL ELSE END ESCAPE EXISTS
FALSE FIRST FOLLOWING HOUR ILIKE IN INTERVAL IS LAST LEADING LEVEL LIKE LOCALTIMESTAMP
MINUTE MONTH NEXT NOT NULL NULLS OF ONLY OR OVER PRECEDING PRIOR RANGE REGEXP RLIKE ROW
ROWID ROWNUM ROWS SECOND SIBLINGS SOME SYSDATE SYSTIMESTAMP THEN TIES TIME TIMESTAMP TO
TRAILING TRUE UNBOUNDED UNKNOWN WHEN WITHIN YEAR ZONE KEEP SEPARATOR GROUPS EXCLUDE
""".split()) | NOT_ALIAS
STMT_START = {'SELECT', 'WITH', 'CREATE', 'ALTER', 'INSERT', 'UPDATE', 'DELETE', 'MERGE',
              'DROP', 'BEGIN', 'DECLARE', 'COMMENT', 'GRANT', 'TRUNCATE', 'EXEC', 'EXECUTE',
              'SET', 'USE'}
COL_STOP = {'CONSTRAINT', 'NOT', 'NULL', 'DEFAULT', 'PRIMARY', 'REFERENCES', 'UNIQUE', 'CHECK',
            'GENERATED', 'AUTO_INCREMENT', 'AUTOINCREMENT', 'IDENTITY', 'COLLATE', 'COMMENT',
            'ENCODE', 'ENABLE', 'DISABLE', 'VISIBLE', 'INVISIBLE', 'CHARSET', 'SPARSE',
            'ROWGUIDCOL', 'ON', 'AS', 'FOREIGN'}
COMMON_TYPES = {'INT', 'INTEGER', 'BIGINT', 'SMALLINT', 'TINYINT', 'VARCHAR', 'VARCHAR2',
                'NVARCHAR', 'NVARCHAR2', 'CHAR', 'NCHAR', 'TEXT', 'NUMBER', 'NUMERIC', 'DECIMAL',
                'DATE', 'DATETIME', 'TIMESTAMP', 'UUID', 'BOOLEAN', 'BOOL', 'FLOAT', 'DOUBLE',
                'REAL', 'BLOB', 'CLOB', 'JSON', 'SERIAL', 'BIGSERIAL'}
ARITH = {'+', '-', '*', '/', '%', '||', '::'}
