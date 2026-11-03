#!/usr/bin/env python3
"""
sql_to_erd.py - turn SQL into a simple ER diagram image (PNG).

Each table is a box with its name and key columns; lines show how tables
are linked (1 - N = one-to-many).

Reads
  * DDL (CREATE TABLE / ALTER TABLE): real columns, types and keys.
  * Queries (SELECT, WITH, views, INSERT ... SELECT): tables, and the
    relationships implied by the joins.

Usage
  python sql_to_erd.py query.sql                    -> query_erd.png next to it
  python sql_to_erd.py a.sql b.sql -o model.png     -> several files, one diagram
  python sql_to_erd.py C:\\path\\to\\sql_folder        -> every .sql in the folder
  python sql_to_erd.py tables.sql --all-columns     -> show every column, not just keys
  python sql_to_erd.py query.sql -o diagram.svg     -> .svg or .pdf also work

Needs Python 3.8+ and matplotlib  (pip install matplotlib).
"""

import argparse
import math
import re
import sys
from collections import OrderedDict, deque
from itertools import product
from pathlib import Path


# =============================================================== tokenizer ====

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


# ================================================================ keywords ====

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


# =================================================================== model ====

class Column:
    def __init__(self, name):
        self.name = name
        self.type = None
        self.pk = False          # declared in DDL
        self.fk = False          # declared in DDL
        self.ddl_order = None
        self.show_pk = False     # what the diagram shows (declared or inferred)
        self.show_fk = False


class Table:
    def __init__(self, parts):
        self.name = parts[-1]
        self.schema = '.'.join(parts[:-1]) or None
        self.aliases = []
        self.columns = OrderedDict()
        self.from_ddl = False
        self.ddl_pk = False

    def col(self, name):
        key = name.upper()
        c = self.columns.get(key)
        if c is None:
            c = self.columns[key] = Column(name)
        return c

    def add_alias(self, alias):
        if alias.upper() != self.name.upper() and alias.upper() not in (a.upper() for a in self.aliases):
            self.aliases.append(alias)


class Rel:
    def __init__(self, a, b):
        self.a, self.b = a, b    # (Table, COLUMN_KEY)
        self.kinds = set()
        self.one = None          # endpoint on the "one" side, None = unknown
        self.declared = False


class Model:
    def __init__(self):
        self.tables = []
        self.by_key = {}
        self.rels = OrderedDict()
        self.pending_fks = []

    def table(self, parts):
        key = '.'.join(p.upper() for p in parts)
        t = self.by_key.get(key)
        if t is not None:
            return t
        short = parts[-1].upper()
        if len(parts) == 1:                       # "ACCOUNT" after "RDB_OWNER.ACCOUNT"
            same = [x for x in self.tables if x.name.upper() == short]
            if len(same) == 1:
                self.by_key[key] = same[0]
                return same[0]
        else:                                     # "RDB_OWNER.ACCOUNT" after "ACCOUNT"
            t = self.by_key.get(short)
            if t is not None and t.schema is None:
                t.schema = '.'.join(parts[:-1])
                self.by_key[key] = t
                return t
        t = Table(parts)
        self.tables.append(t)
        self.by_key[key] = t
        return t

    def relate(self, x, y, kind, one=None):
        if x == y:
            return
        k = frozenset((x, y))
        r = self.rels.get(k)
        if r is None:
            r = self.rels[k] = Rel(x, y)
        r.kinds.add(kind)
        if one is not None:
            r.one, r.declared = one, True


# ================================================================== parser ====

class Source:
    __slots__ = ('kind', 'table', 'out')

    def __init__(self, kind, table=None, out=None):
        self.kind, self.table, self.out = kind, table, out or []


class Scope:
    def __init__(self, parent=None):
        self.parent = parent
        self.aliases = {}
        self.ctes = {}
        self.sources = []

    def find(self, name):
        s = self
        while s is not None:
            if name in s.aliases:
                return s.aliases[name]
            s = s.parent
        return None

    def find_cte(self, name):
        s = self
        while s is not None:
            if name in s.ctes:
                return s.ctes[name]
            s = s.parent
        return None


class Parser:
    def __init__(self, model, toks):
        self.model, self.t, self.n = model, toks, len(toks)
        self.m = [None] * self.n
        stack = []
        for i, tk in enumerate(toks):
            if tk.kind == 'op' and tk.text == '(':
                stack.append(i)
            elif tk.kind == 'op' and tk.text == ')' and stack:
                j = stack.pop()
                self.m[i], self.m[j] = j, i

    # ---------------------------------------------------------------- helpers
    def kw(self, i):
        return self.t[i].up if 0 <= i < self.n and self.t[i].kind == 'ident' else ''

    def tx(self, i):
        return self.t[i].text if 0 <= i < self.n and self.t[i].kind == 'op' else ''

    def top(self, i, j):
        """Indexes in [i, j) at the current parenthesis depth."""
        k = i
        while k < j:
            yield k
            if self.tx(k) == '(' and self.m[k] is not None and self.m[k] < j:
                k = self.m[k] + 1
            else:
                k += 1

    def split_commas(self, i, j):
        parts, start = [], i
        for k in self.top(i, j):
            if self.tx(k) == ',':
                parts.append((start, k))
                start = k + 1
        parts.append((start, j))
        return [(a, b) for a, b in parts if a < b]

    def starts_query(self, i, j):
        while i < j and self.tx(i) == '(':
            i += 1
        return i < j and self.kw(i) in ('SELECT', 'WITH')

    def is_join_word(self, i):                    # LEFT( / RIGHT( are string functions
        u = self.kw(i)
        return u in JOIN_WORDS and not (u in ('LEFT', 'RIGHT') and self.tx(i + 1) == '(')

    def outer_mark(self, i):                      # Oracle (+)
        return self.tx(i) == '(' and self.tx(i + 1) == '+' and self.tx(i + 2) == ')'

    def read_name(self, k):
        parts = []
        while k < self.n and self.t[k].kind in IDENT:
            parts.append(self.t[k].text)
            k += 1
            if self.tx(k) == '.':
                k += 1
            else:
                break
        return parts, k

    def paren_names(self, k):
        if self.tx(k) != '(' or self.m[k] is None:
            return [], k
        e = self.m[k]
        names = [self.t[a].text for a, b in self.split_commas(k + 1, e) if self.t[a].kind in IDENT]
        return names, e + 1

    # ------------------------------------------------------------- statements
    def run(self):
        start = 0
        for k in self.top(0, self.n):
            sep = self.tx(k) == ';'
            if not sep and (self.tx(k) == '/' or self.kw(k) == 'GO'):
                sep = k + 1 >= self.n or self.kw(k + 1) in STMT_START
            if sep:
                self.statement(start, k)
                start = k + 1
        self.statement(start, self.n)

    def statement(self, a, b):
        if a >= b:
            return
        try:
            u = self.kw(a)
            if u in ('SELECT', 'WITH') or self.tx(a) == '(':
                self.query(a, b, None)
            elif u == 'CREATE':
                self.create(a, b)
            elif u == 'ALTER':
                self.alter(a, b)
            elif u in ('INSERT', 'MERGE', 'UPDATE', 'DELETE', 'REPLACE', 'UPSERT'):
                for k in self.top(a, b):
                    if self.kw(k) in ('SELECT', 'WITH'):
                        self.query(k, b, None)
                        break
        except Exception as exc:                  # one odd statement shouldn't stop the run
            head = ' '.join(t.text for t in self.t[a:a + 6])
            print(f"  ! skipped a statement starting '{head} ...' ({exc})", file=sys.stderr)

    # ---------------------------------------------------------------- queries
    def query(self, a, b, parent):
        """Parse a query; return its output columns as [(NAME, {(Table, COL)})]."""
        while a < b and self.tx(a) == '(' and self.m[a] == b - 1:
            a, b = a + 1, b - 1
        scope = parent
        if self.kw(a) == 'WITH':
            scope = Scope(parent)
            a = self.with_clause(a + 1, b, scope)
        branches, start = [], a
        for k in self.top(a, b):
            if self.kw(k) in SET_OPS:
                branches.append((start, k))
                start = k + 1
                if self.kw(start) in ('ALL', 'DISTINCT'):
                    start += 1
        branches.append((start, b))
        out = None
        for x, y in branches:
            cols = self.select(x, y, scope)
            if out is None:
                out = [[nm, set(lin)] for nm, lin in cols]
            else:                                 # UNION: columns line up by position
                for i, (nm, lin) in enumerate(cols):
                    if i < len(out):
                        out[i][1] |= lin
        return [(nm, lin) for nm, lin in (out or [])]

    def with_clause(self, a, b, scope):
        if self.kw(a) == 'RECURSIVE':
            a += 1
        while a < b and self.t[a].kind in IDENT:
            name = self.t[a].up
            a += 1
            cols = None
            if self.tx(a) == '(' and self.m[a] is not None and self.kw(self.m[a] + 1) == 'AS':
                cols = [self.t[j].up for j in range(a + 1, self.m[a]) if self.t[j].kind in IDENT]
                a = self.m[a] + 1
            if self.kw(a) != 'AS':
                return a
            a += 1
            if self.kw(a) == 'NOT':
                a += 1
            if self.kw(a) == 'MATERIALIZED':
                a += 1
            if self.tx(a) != '(' or self.m[a] is None:
                return a
            e = self.m[a]
            scope.ctes[name] = []
            out = self.query(a + 1, e, scope)
            if cols:
                out = [(c, out[i][1] if i < len(out) else set()) for i, c in enumerate(cols)]
            scope.ctes[name] = out
            a = e + 1
            if self.tx(a) == ',':
                a += 1
            else:
                break
        return a

    def select(self, a, b, parent):
        if a >= b:
            return []
        if self.tx(a) == '(' and self.m[a] is not None and self.m[a] < b:
            return self.query(a + 1, self.m[a], parent)
        if self.kw(a) != 'SELECT':
            return []

        marks = []
        for k in self.top(a, b):
            u = self.kw(k)
            if u in CLAUSES and (k == a or u != 'SELECT') and self.tx(k - 1) != '.':
                nxt = CLAUSE_NEXT.get(u)
                if nxt and self.kw(k + 1) != nxt:
                    continue
                marks.append((u, k))
        clause = {}
        for i, (u, k) in enumerate(marks):
            if u not in clause:
                clause[u] = (k, marks[i + 1][1] if i + 1 < len(marks) else b)

        scope = Scope(parent)
        conds = []
        if 'FROM' in clause:
            fa, fb = clause['FROM']
            conds = self.from_clause(fa + 1, fb, scope)
        single = len(scope.sources) == 1

        sa, sb = clause['SELECT']
        sa += 1
        while sa < sb and self.kw(sa) in ('DISTINCT', 'UNIQUE', 'ALL', 'SQL_CALC_FOUND_ROWS',
                                          'HIGH_PRIORITY', 'STRAIGHT_JOIN', 'SQL_NO_CACHE'):
            sa += 1
        if self.kw(sa) == 'TOP':                  # SQL Server
            sa += 1
            sa = self.m[sa] + 1 if self.tx(sa) == '(' and self.m[sa] is not None else sa + 1
            if self.kw(sa) == 'PERCENT':
                sa += 1
            if self.kw(sa) == 'WITH' and self.kw(sa + 1) == 'TIES':
                sa += 2

        exclude, out = set(), []
        for x, y in self.split_commas(sa, sb):
            out.extend(self.select_item(x, y, scope, single, exclude))
        self.scan(sa, sb, scope, single, exclude)
        for x, y in conds:
            self.scan(x, y, scope, False, exclude)
        for u in ('WHERE', 'GROUP', 'HAVING', 'CONNECT', 'START', 'QUALIFY'):
            if u in clause:
                x, y = clause[u]
                self.scan(x + 1, y, scope, single, exclude)
        if 'ORDER' in clause:
            x, y = clause['ORDER']
            self.scan(x + 2, y, scope, False, exclude)
        return out

    def select_item(self, a, b, scope, single, exclude):
        t, n = self.t, b - a
        if n == 1 and self.tx(a) == '*':
            res = []
            for src in scope.sources:
                if src.kind == 'derived':
                    res.extend((nm, set(lin)) for nm, lin in src.out)
            return res
        if n == 3 and self.tx(b - 1) == '*' and self.tx(a + 1) == '.':
            src = scope.find(t[a].up)
            return [(nm, set(lin)) for nm, lin in src.out] if src and src.kind == 'derived' else []
        name, eb = None, b
        last = t[b - 1]
        if n >= 2 and self.kw(b - 2) == 'AS' and last.kind in IDENT:
            name, eb = last.up, b - 2
            exclude.update((b - 2, b - 1))
        elif (n >= 2 and last.kind in IDENT and not (last.kind == 'ident' and last.up in SQL_WORDS)
              and self.tx(b - 2) != '.'
              and (t[b - 2].kind in ('ident', 'qident', 'string', 'number') or self.tx(b - 2) == ')')):
            name, eb = last.up, b - 1
            exclude.add(b - 1)
        ref = self.read_ref(a, eb, scope, single, exclude)
        if ref and ref[2] == eb:                  # the whole expression is one column
            return [(name or ref[0], ref[1])]
        return [(name, set())]

    def from_clause(self, a, b, scope):
        t, conds = self.t, []
        prev = cur = None
        k = a
        while k < b:
            u, s = self.kw(k), self.tx(k)
            if s == ',' or self.is_join_word(k):
                k += 1
                continue
            if u == 'ON':
                e = b
                for j in self.top(k + 1, b):
                    if self.tx(j) == ',' or self.is_join_word(j):
                        e = j
                        break
                conds.append((k + 1, e))
                k = e
                continue
            if u == 'USING' and self.tx(k + 1) == '(' and self.m[k + 1] is not None:
                e = self.m[k + 1]
                if prev is not None and cur is not None:
                    for j in range(k + 2, e):
                        if t[j].kind in IDENT:
                            for x in self.lineage(prev, t[j]):
                                for y in self.lineage(cur, t[j]):
                                    self.model.relate(x, y, 'join')
                k = e + 1
                continue

            src, names = None, []
            if s == '(' and self.m[k] is not None and self.m[k] < b:
                e = self.m[k]
                if self.starts_query(k + 1, e):
                    lateral = self.kw(k - 1) in ('LATERAL', 'APPLY')
                    src = Source('derived', out=self.query(k + 1, e, scope if lateral else scope.parent))
                else:                             # ( a JOIN b ON ... )
                    conds += self.from_clause(k + 1, e, scope)
                k = e + 1
            elif t[k].kind in IDENT:
                if (u in ('TABLE', 'UNNEST', 'JSON_TABLE', 'XMLTABLE', 'OPENJSON', 'GENERATE_SERIES')
                        and self.tx(k + 1) == '(' and self.m[k + 1] is not None):
                    k = self.m[k + 1] + 1
                else:
                    parts = [t[k].text]
                    k += 1
                    while k + 1 < b and self.tx(k) == '.' and t[k + 1].kind in IDENT:
                        parts.append(t[k + 1].text)
                        k += 2
                    if self.tx(k) == '@':           # Oracle db link
                        k += 1
                        while k < b and (t[k].kind in IDENT or self.tx(k) == '.'):
                            k += 1
                    full = '.'.join(parts).upper()
                    cte = scope.find_cte(full) if len(parts) == 1 else None
                    if cte is not None:
                        src, names = Source('derived', out=cte), [full]
                    elif full not in ('DUAL', 'SYS.DUAL'):
                        tbl = self.model.table(parts)
                        src, names = Source('table', table=tbl), [tbl.name.upper(), full]
            else:
                k += 1
                continue

            k = self.skip_modifiers(k, b)
            alias = None
            if self.kw(k) == 'AS':
                k += 1
            if k < b and t[k].kind in IDENT and not (t[k].kind == 'ident' and t[k].up in NOT_ALIAS):
                alias = t[k]
                k += 1
                if self.tx(k) == '(' and self.m[k] is not None:     # AS x (c1, c2, ...)
                    if src is not None and src.kind == 'derived':
                        cols = [t[j].up for j in range(k + 1, self.m[k]) if t[j].kind in IDENT]
                        src.out = [(c, src.out[i][1] if i < len(src.out) else set())
                                   for i, c in enumerate(cols)]
                    k = self.m[k] + 1
            k = self.skip_modifiers(k, b)
            if src is not None:
                scope.sources.append(src)
                if alias is not None:
                    scope.aliases[alias.up] = src
                    if src.kind == 'table':
                        src.table.add_alias(alias.text)
                else:
                    for nm in names:
                        scope.aliases.setdefault(nm, src)
                prev, cur = cur, src
        return conds

    def skip_modifiers(self, k, b):
        while k < b:
            u, j = self.kw(k), k + 1
            if u in ('PARTITION', 'SUBPARTITION', 'SAMPLE', 'TABLESAMPLE', 'WITH', 'PIVOT', 'UNPIVOT'):
                if self.kw(j) in ('BLOCK', 'SYSTEM', 'BERNOULLI', 'XML', 'FOR'):
                    j += 1
            elif u in ('USE', 'FORCE', 'IGNORE') and self.kw(j) in ('INDEX', 'KEY'):
                j += 1
            else:
                break
            if self.tx(j) == '(' and self.m[j] is not None:
                k = self.m[j] + 1
            else:
                break
        return k

    # ------------------------------------------------------- column references
    def lineage(self, src, tok):
        if src is None or tok.text == '*':
            return set()
        if src.kind == 'table':
            src.table.col(tok.text)
            return {(src.table, tok.up)}
        for nm, lin in src.out:
            if nm == tok.up:
                return set(lin)
        return set()

    def read_ref(self, k, b, scope, single, exclude):
        """Column reference at k -> (NAME, lineage, end) or None."""
        t = self.t
        if k >= b or t[k].kind not in IDENT or self.tx(k - 1) in ('.', ':', '&', '@', '?'):
            return None
        if self.tx(k + 1) == '.':
            if not (k + 2 < b and t[k + 2].kind in IDENT):
                return None
            if self.tx(k + 3) == '.':               # schema.table.column
                if not (k + 4 < b and t[k + 4].kind in IDENT) or self.tx(k + 5) in ('(', '.'):
                    return None
                qual, col, end = t[k].up + '.' + t[k + 2].up, t[k + 4], k + 5
            else:
                if self.tx(k + 3) == '(':           # package.function(...)
                    return None
                qual, col, end = t[k].up, t[k + 2], k + 3
            src = scope.find(qual) if scope else None
            return (col.up, self.lineage(src, col) if src else set(), end)
        if not single or k in exclude:
            return None
        if t[k].kind == 'ident' and t[k].up in SQL_WORDS:
            return None
        if self.tx(k + 1) == '(' or self.kw(k - 1) == 'AS':
            return None
        return (t[k].up, self.lineage(scope.sources[0], t[k]), k + 1)

    def scan(self, a, b, scope, single, exclude):
        """Record column usage, run nested sub-queries, and find  x.col = y.col  joins."""
        refs, ends = {}, {}
        k = a
        while k < b:
            if self.tx(k) == '(' and self.m[k] is not None and self.m[k] < b:
                e = self.m[k]
                if self.starts_query(k + 1, e):
                    out = self.query(k + 1, e, scope)
                    op = k - 1
                    if self.kw(op) == 'IN' or self.tx(op) == '=':
                        j = op - 1 if self.kw(op - 1) == 'NOT' else op
                        lin = ends.get(j)
                        if lin and len(out) == 1:
                            for x in lin:
                                for y in out[0][1]:
                                    self.model.relate(x, y, 'in')
                    k = e + 1
                    continue
                k += 1
                continue
            r = self.read_ref(k, b, scope, single, exclude)
            if r:
                refs[k] = (r[2], r[1])
                ends[r[2]] = r[1]
                k = r[2]
                continue
            k += 1

        for s, (e, lin) in refs.items():
            if not lin:
                continue
            j = e + 3 if self.outer_mark(e) else e
            if self.tx(j) != '=' or (j + 1) not in refs:
                continue
            e2, lin2 = refs[j + 1]
            after = e2 + 3 if self.outer_mark(e2) else e2
            if not lin2 or self.tx(s - 1) in ARITH or self.tx(after) in ARITH:
                continue
            for x in lin:
                for y in lin2:
                    self.model.relate(x, y, 'join')

    # --------------------------------------------------------------------- DDL
    def create(self, a, b):
        k, kind = a + 1, None
        while k < min(b, a + 12):
            u = self.kw(k)
            if u in ('TABLE', 'VIEW'):
                kind = u
                k += 1
                break
            if u in ('INDEX', 'SEQUENCE', 'PROCEDURE', 'FUNCTION', 'PACKAGE', 'TRIGGER', 'TYPE',
                     'SYNONYM', 'USER', 'ROLE', 'SCHEMA', 'DATABASE', 'TABLESPACE', 'EXTENSION'):
                return
            k += 1
        if kind is None:
            return
        if self.kw(k) == 'IF':
            k += 3 if self.kw(k + 1) == 'NOT' else 2
        parts, k = self.read_name(k)
        if not parts:
            return
        if kind == 'VIEW':
            for j in self.top(k, b):
                if self.kw(j) == 'AS' and self.starts_query(j + 1, b):
                    self.query(j + 1, b, None)
                    return
            return
        tbl = self.model.table(parts)
        tbl.from_ddl = True
        if self.tx(k) == '(' and self.m[k] is not None:
            e = self.m[k]
            for x, y in self.split_commas(k + 1, e):
                self.column_def(tbl, x, y)
            k = e + 1
        for j in self.top(k, b):                    # CREATE TABLE x AS SELECT ...
            if self.kw(j) == 'AS' and self.starts_query(j + 1, b):
                self.query(j + 1, b, None)
                break

    def column_def(self, tbl, a, b):
        t = self.t
        u = self.kw(a)
        if u == 'CONSTRAINT':
            a += 2
            u = self.kw(a)
        if u in ('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'INDEX', 'FULLTEXT', 'SPATIAL',
                 'EXCLUDE', 'PERIOD', 'LIKE', 'CLUSTERED') or (u == 'KEY' and self.kw(a + 1) not in COMMON_TYPES):
            self.table_constraint(tbl, a, b)
            return
        if t[a].kind not in IDENT:
            return
        col = tbl.col(t[a].text)
        if col.ddl_order is None:
            col.ddl_order = sum(1 for c in tbl.columns.values() if c.ddl_order is not None)
        j, type_toks = a + 1, []
        while j < b:
            if t[j].kind == 'ident' and t[j].up in COL_STOP:
                break
            if self.tx(j) == '(' and self.m[j] is not None and self.m[j] < b:
                type_toks.extend(t[j:self.m[j] + 1])
                j = self.m[j] + 1
                continue
            type_toks.append(t[j])
            j += 1
        if type_toks:
            col.type = join_tokens(type_toks)
        for x in range(j, b):
            if self.kw(x) == 'PRIMARY' and self.kw(x + 1) == 'KEY':
                col.pk = tbl.ddl_pk = True
            elif self.kw(x) == 'REFERENCES':
                parts, nx = self.read_name(x + 1)
                refcols, _ = self.paren_names(nx)
                if parts:
                    self.foreign_key(tbl, [col.name], parts, refcols)

    def table_constraint(self, tbl, a, b):
        u = self.kw(a)
        if u == 'PRIMARY':
            j = a + 2 if self.kw(a + 1) == 'KEY' else a + 1
            while self.kw(j) in ('CLUSTERED', 'NONCLUSTERED'):
                j += 1
            cols, _ = self.paren_names(j)
            for c in cols:
                tbl.col(c).pk = True
            if cols:
                tbl.ddl_pk = True
        elif u == 'FOREIGN':
            j = a + 2 if self.kw(a + 1) == 'KEY' else a + 1
            if j < self.n and self.t[j].kind in IDENT and self.tx(j + 1) == '(':
                j += 1                            # MySQL: FOREIGN KEY name (cols)
            cols, j = self.paren_names(j)
            while j < b and self.kw(j) != 'REFERENCES':
                j += 1
            if j < b:
                parts, nx = self.read_name(j + 1)
                refcols, _ = self.paren_names(nx)
                if parts and cols:
                    self.foreign_key(tbl, cols, parts, refcols)

    def alter(self, a, b):
        if self.kw(a + 1) != 'TABLE':
            return
        k = a + 2
        while self.kw(k) in ('ONLY', 'IF', 'EXISTS'):
            k += 1
        parts, k = self.read_name(k)
        if not parts:
            return
        tbl = self.model.table(parts)
        for j in range(k, b):
            if self.kw(j) in ('PRIMARY', 'FOREIGN') and self.kw(j + 1) == 'KEY':
                self.table_constraint(tbl, j, b)

    def foreign_key(self, tbl, cols, refparts, refcols):
        rt = self.model.table(refparts)
        for c in cols:
            tbl.col(c).fk = True
        self.model.pending_fks.append((tbl, cols, rt, refcols))


def join_tokens(toks):
    s = ''
    for tk in toks:
        x = tk.text
        if s and x not in (')', ',', '(') and not s.endswith('('):
            s += ' '
        s += x
    return s


# =============================================================== inference ====

def pk_like(tbl, key):
    c = tbl.columns.get(key)
    if tbl.ddl_pk:
        return bool(c and c.pk)
    if c is not None and c.pk:
        return True
    if key in ('ID', 'OBJECT_ID', 'OBJID', 'OID', 'UUID', 'GUID', 'ROW_ID'):
        return True
    stems = set()
    for s in (tbl.name.upper(), tbl.name.upper().rsplit('_', 1)[-1]):
        stems.add(s)
        if s.endswith('IES'):
            stems.add(s[:-3] + 'Y')
        elif s.endswith('S') and not s.endswith('SS'):
            stems.add(s[:-1])
    return any(key in (s + '_ID', s + 'ID', s + '_KEY') for s in stems)


def guess_one(a, b):
    pa, pb = pk_like(*a), pk_like(*b)
    if pa and not pb:
        return a
    if pb and not pa:
        return b
    if pa and pb and a[0] is not b[0]:
        na, nb = a[0].name.upper(), b[0].name.upper()
        if nb.startswith(na) and nb != na:        # R_OE_BPI  <-  R_OE_BPI_SUSPEND_REASON
            return a
        if na.startswith(nb) and na != nb:
            return b
    return None


def finalize(model):
    for tbl, cols, rt, refcols in model.pending_fks:
        if not refcols:                           # REFERENCES t  (no column list)
            pk = [c.name for c in rt.columns.values() if c.pk]
            guess = [c.name for k, c in rt.columns.items() if pk_like(rt, k)]
            refcols = pk if len(pk) == len(cols) else guess if len(guess) == len(cols) else cols
        for c, rc in zip(cols, refcols):
            rt.col(rc)
            model.relate((tbl, c.upper()), (rt, rc.upper()), 'fk', one=(rt, rc.upper()))
    many_side = set()
    for r in model.rels.values():
        if r.one is None:
            r.one = guess_one(r.a, r.b)
        if r.one is not None:
            many_side.add(r.b if r.one == r.a else r.a)
    for t in model.tables:
        for key, c in t.columns.items():
            if t.ddl_pk or c.pk:
                c.show_pk = c.pk
            else:
                c.show_pk = pk_like(t, key) and (t, key) not in many_side
            c.show_fk = c.fk or (t, key) in many_side


# ================================================================== layout ====

HGAP, VGAP = 150, 40


def layout(tables, rels, sizes, rowidx):
    order = {t: i for i, t in enumerate(tables)}
    adj = {t: set() for t in tables}
    for r in rels:
        a, b = r.a[0], r.b[0]
        if a is not b:
            adj[a].add(b)
            adj[b].add(a)
    comps, seen = [], set()
    for t in sorted(tables, key=lambda t: (-len(adj[t]), order[t])):
        if t in seen:
            continue
        comp, dq = [], deque([t])
        seen.add(t)
        while dq:
            u = dq.popleft()
            comp.append(u)
            for v in sorted(adj[u], key=order.get):
                if v not in seen:
                    seen.add(v)
                    dq.append(v)
        comps.append(comp)

    pos, y, right = {}, 0.0, 0.0
    for comp in comps:
        if len(comp) == 1:
            continue
        placed = place(arrange(comp, adj, order), adj, sizes, rowidx)
        top = min(p[1] for p in placed.values())
        bottom = max(p[1] + sizes[t][1] for t, p in placed.items())
        for t, (px, py) in placed.items():
            pos[t] = (px, py - top + y)
        right = max(right, max(p[0] + sizes[t][0] for t, p in placed.items()))
        y += bottom - top + 100
    singles = [c[0] for c in comps if len(c) == 1]
    if singles:
        limit, x, row_h = max(right, 1000.0), 0.0, 0.0
        for t in singles:
            w, h = sizes[t]
            if x > 0 and x + w > limit:
                x, y, row_h = 0.0, y + row_h + 60, 0.0
            pos[t] = (x, y)
            x += w + 60
            row_h = max(row_h, h)
    return pos


def arrange(comp, adj, order):
    """Pick a central table and spread its neighbours to the left and right in columns."""
    best = None
    for root in sorted(comp, key=lambda t: (-len(adj[t]), order[t]))[:15]:
        lay, group, dq = {root: 0}, {root: None}, deque([root])
        while dq:
            u = dq.popleft()
            for v in sorted(adj[u], key=order.get):
                if v not in lay:
                    lay[v] = lay[u] + 1
                    group[v] = v if u is root else group[u]
                    dq.append(v)
        kids = sorted((t for t in comp if lay[t] == 1), key=order.get)
        weight = {k: 0 for k in kids}
        for t in comp:
            if group[t] is not None:
                weight[group[t]] += 1
        edges = [(u, v) for u in comp for v in adj[u] if order[u] < order[v]]

        def cost(side):
            cross = sum(1 for u, v in edges if u is not root and v is not root
                        and side[group[u]] != side[group[v]])
            left = sum(weight[k] for k in kids if side[k] < 0)
            rgt = sum(weight[k] for k in kids if side[k] > 0)
            return cross * 10 + abs(rgt - left) + (0.1 if left > rgt else 0)

        if len(kids) <= 10:
            side = min((dict(zip(kids, combo)) for combo in product((1, -1), repeat=len(kids))), key=cost)
        else:
            side, lw, rw = {}, 0, 0
            for k in sorted(kids, key=lambda k: -weight[k]):
                side[k] = 1 if rw <= lw else -1
                rw, lw = (rw + weight[k], lw) if side[k] > 0 else (rw, lw + weight[k])
        col = {t: 0 if t is root else lay[t] * side[group[t]] for t in comp}
        same = sum(1 for u, v in edges if col[u] == col[v])
        span = sum(1 for u, v in edges if abs(col[u] - col[v]) > 1)
        counts = {}
        for t in comp:
            counts[col[t]] = counts.get(col[t], 0) + 1
        score = same * 4 + span * 5 + max(counts.values()) * 2 + len(counts)
        if best is None or score < best[0]:
            best = (score, col)
    cols = {}
    for t in sorted(comp, key=order.get):
        cols.setdefault(best[1][t], []).append(t)
    return cols


def place(cols, adj, sizes, rowidx):
    keys = sorted(cols)
    yc = {}

    def off(a, b):                                # y of a's row that joins b, from a's top
        return HEAD_H + rowidx.get((a, b), 0) * ROW_H + ROW_H / 2

    def aligned(t, v):                            # centre of t that lines up the joined rows
        return yc[v] - sizes[v][1] / 2 + off(v, t) - off(t, v) + sizes[t][1] / 2

    for t in cols[0]:
        yc[t] = 0.0
    for c in sorted((c for c in keys if c != 0), key=lambda c: (abs(c), c < 0)):
        inner = set(cols.get(c - 1 if c > 0 else c + 1, []))
        want, link_y = {}, {}
        for t in cols[c]:
            near = [v for v in adj[t] if v in inner] or [v for v in adj[t] if v in yc]
            ys = [aligned(t, v) for v in near] or [0.0]
            want[t] = sum(ys) / len(ys)
            ly = [yc[v] - sizes[v][1] / 2 + off(v, t) for v in near]   # where its lines land
            link_y[t] = sum(ly) / len(ly) if ly else want[t]
        cols[c].sort(key=lambda t: (link_y[t], want[t]))
        tops, cur = [], -1e18
        for t in cols[c]:
            top = max(want[t] - sizes[t][1] / 2, cur)
            tops.append(top)
            cur = top + sizes[t][1] + VGAP
        shift = sum(want[t] - (tp + sizes[t][1] / 2) for t, tp in zip(cols[c], tops)) / len(cols[c])
        for t, tp in zip(cols[c], tops):
            yc[t] = tp + shift + sizes[t][1] / 2
    for t in cols[0]:                             # centre the hub on its neighbours
        ys = [aligned(t, v) for v in adj[t] if v in yc]
        if ys:
            yc[t] = sum(ys) / len(ys)
    widths = {c: max(sizes[t][0] for t in cols[c]) for c in keys}
    pos, x = {}, 0.0
    for c in keys:
        for t in cols[c]:
            w, h = sizes[t]
            pos[t] = (x + (widths[c] - w if c < 0 else 0), yc[t] - h / 2)
        x += widths[c] + HGAP
    return pos


# ================================================================== render ====

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    from matplotlib.patches import Rectangle
    from matplotlib.textpath import TextToPath
except ImportError:
    sys.exit('This script needs matplotlib. Install it with:  pip install matplotlib')

ROW_H, HEAD_H, PAD, MARGIN, TITLE_H = 24, 30, 14, 40, 56
INK, LINE, HEAD_FILL, MUTED = '#1f2937', '#5b6b7f', '#dde7f3', '#6b7280'
PK_COLOR, FK_COLOR = '#b45309', '#2563eb'
PT = 0.72                                       # px -> pt at 100 dpi
_t2p = TextToPath()


def measure(s, px, bold=False):
    prop = FontProperties(weight='bold' if bold else 'normal', size=px * PT)
    return _t2p.get_text_width_height_descent(str(s), prop, ismath=False)[0] / PT


def shown_columns(t, linked, all_cols):
    cols = list(t.columns.values())
    if all_cols and t.from_ddl:
        return sorted(cols, key=lambda c: (c.ddl_order is None, c.ddl_order or 0))
    if not all_cols:
        cols = [c for c in cols if c.show_pk or c.show_fk or (t, c.name.upper()) in linked]
    return ([c for c in cols if c.show_pk] + [c for c in cols if c.show_fk and not c.show_pk]
            + [c for c in cols if not c.show_pk and not c.show_fk])


def render(model, out_path, label, all_cols):
    tables, rels = model.tables, list(model.rels.values())
    linked = {end for r in rels for end in (r.a, r.b)}
    multi = len({t.schema for t in tables if t.schema}) > 1

    info = {}
    for t in tables:
        cols = shown_columns(t, linked, all_cols)
        title = (t.schema + '.' if multi and t.schema else '') + t.name
        tags = [' '.join(k for k, on in (('PK', c.show_pk), ('FK', c.show_fk)) if on) for c in cols]
        tag_w = max((measure(x, 10, True) for x in tags if x), default=0)
        tag_w = tag_w + 8 if tag_w else 0
        name_w = max((measure(c.name, 12) for c in cols), default=0)
        type_w = max((measure(c.type, 11) + 16 for c in cols if c.type), default=0) if all_cols else 0
        w = max(measure(title, 13, True) + 2 * PAD, tag_w + name_w + type_w + 2 * PAD, 120)
        w = math.ceil(w / 10) * 10
        h = HEAD_H + ROW_H * len(cols) + (6 if cols else 0)
        info[t] = (cols, tags, title, tag_w, w, h)

    rowidx = {}
    for r in rels:
        for (ta, ca), (tb, _) in ((r.a, r.b), (r.b, r.a)):
            i = [c.name.upper() for c in info[ta][0]].index(ca)
            rowidx[(ta, tb)] = min(i, rowidx.get((ta, tb), i))
    pos = layout(tables, rels, {t: (v[4], v[5]) for t, v in info.items()}, rowidx)
    P = {t: (x + MARGIN, y + MARGIN + TITLE_H) for t, (x, y) in pos.items()}

    W = max(P[t][0] + info[t][4] for t in tables) + MARGIN + 60
    H = max(P[t][1] + info[t][5] for t in tables) + MARGIN
    W = max(W, measure(f'ER diagram - {label}', 16, True) + 2 * MARGIN, 520)
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis('off')

    # title + legend
    ax.text(MARGIN, 26, f'ER diagram - {label}', fontsize=16 * PT, weight='bold', color=INK, va='center')
    notes = ['1 - N = one-to-many', 'PK primary key', 'FK foreign key']
    if any(r.one is None for r in rels):
        notes.append('dashed = matched by value')
    if any(not r.declared for r in rels):
        notes.append('keys inferred from the joins')
    ax.text(MARGIN, 48, '   ·   '.join(notes), fontsize=11 * PT, color=MUTED, va='center')

    # relationship lines (under the boxes)
    def anchor(t, key):
        x, y = P[t]
        i = [c.name.upper() for c in info[t][0]].index(key)
        return x, y + HEAD_H + 3 + i * ROW_H + ROW_H / 2, x + info[t][4]

    loops = {}
    for r in rels:
        one, many = (r.one, r.b if r.one == r.a else r.a) if r.one else (r.a, r.b)
        lx1, y1, rx1 = anchor(*one)
        lx2, y2, rx2 = anchor(*many)
        if rx1 <= lx2:
            x1, x2, d1, d2 = rx1, lx2, 1, -1
        elif rx2 <= lx1:
            x1, x2, d1, d2 = lx1, rx2, -1, 1
        else:                                     # same column: loop round the right side
            x1, x2, d1, d2 = rx1, rx2, 1, 1
        if d1 == d2:
            k = round(max(x1, x2) / 10)
            loops[k] = loops.get(k, 0) + 1
            ex = max(x1, x2) + 18 + 12 * loops[k]
            xs, ys = [x1, ex, ex, x2], [y1, y1, y2, y2]
        else:
            xs, ys = [x1, x1 + d1 * 22, x2 + d2 * 22, x2], [y1, y1, y2, y2]
        ax.plot(xs, ys, color=LINE, lw=1.3, ls=(0, (4, 3)) if r.one is None else '-', zorder=1)
        if r.one is not None:
            for x, y, d, s in ((x1, y1, d1, '1'), (x2, y2, d2, 'N')):
                ax.text(x + d * 9, y - 3, s, ha='center', va='bottom', fontsize=11 * PT,
                        weight='bold', color=INK, zorder=2)

    # tables
    for t in tables:
        cols, tags, title, tag_w, w, h = info[t]
        x, y = P[t]
        ax.add_patch(Rectangle((x, y), w, h, facecolor='white', edgecolor=LINE, lw=1.2, zorder=3))
        ax.add_patch(Rectangle((x, y), w, HEAD_H, facecolor=HEAD_FILL, edgecolor=LINE, lw=1.2, zorder=4))
        ax.text(x + w / 2, y + HEAD_H / 2, title, ha='center', va='center', fontsize=13 * PT,
                weight='bold', color=INK, zorder=5)
        for i, (c, tag) in enumerate(zip(cols, tags)):
            cy = y + HEAD_H + 3 + i * ROW_H + ROW_H / 2
            if tag:
                ax.text(x + PAD, cy, tag, va='center', fontsize=10 * PT, weight='bold',
                        color=PK_COLOR if c.show_pk else FK_COLOR, zorder=5)
            ax.text(x + PAD + tag_w, cy, c.name, va='center', fontsize=12 * PT, color=INK, zorder=5)
            if all_cols and c.type:
                ax.text(x + w - PAD, cy, c.type, ha='right', va='center', fontsize=11 * PT,
                        color=MUTED, zorder=5)

    fig.savefig(out_path, dpi=200, facecolor='white')
    plt.close(fig)


# ==================================================================== main ====

def main(argv=None):
    ap = argparse.ArgumentParser(description='Turn SQL (DDL or queries) into a simple ER diagram image.')
    ap.add_argument('inputs', nargs='+', help='.sql files and/or folders containing .sql files')
    ap.add_argument('-o', '--output', help='output image (.png, .svg or .pdf). Default: <input>.png')
    ap.add_argument('--all-columns', action='store_true', help='show every column, not just the keys')
    args = ap.parse_args(argv)

    files = []
    for p in map(Path, args.inputs):
        if p.is_dir():
            files += sorted(p.rglob('*.sql'))
        elif p.is_file():
            files.append(p)
        else:
            print(f'Not found: {p}', file=sys.stderr)
    if not files:
        sys.exit('No .sql files found.')

    model = Model()
    for f in files:
        Parser(model, tokenize(f.read_text(encoding='utf-8-sig', errors='replace'))).run()
    finalize(model)
    if not model.tables:
        sys.exit('No tables found in the SQL.')

    first = Path(args.inputs[0])
    out = Path(args.output) if args.output else (
        first / (first.name + '_erd.png') if first.is_dir() else first.with_name(first.stem + '_erd.png'))
    label = files[0].name if len(files) == 1 else f'{len(files)} SQL files'
    render(model, out, label, args.all_columns)
    print(f'{len(model.tables)} tables, {len(model.rels)} relationships')
    print(f'Saved: {out}')


if __name__ == '__main__':
    main()
