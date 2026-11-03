"""SQL parser: DDL plus the relationships implied by joins in queries.

Copied from tools/sql_to_drawio.py and tools/sql_to_erd.py (identical in both).
App additions: column_def() and table_constraint() record NOT NULL / PRIMARY KEY
in Column.nullable, and a column that is unique by itself in Column.unique (UNIQUE
on the column, UNIQUE (one column), ALTER TABLE ... ADD UNIQUE, CREATE UNIQUE INDEX).
"""

import sys

from .tokenizer import (ARITH, CLAUSE_NEXT, CLAUSES, COL_STOP, COMMON_TYPES, IDENT, JOIN_WORDS,
                        NOT_ALIAS, SET_OPS, SQL_WORDS, STMT_START)


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
            if u == 'INDEX' and 'UNIQUE' in (self.kw(j) for j in range(a + 1, k)):
                self.unique_index(k + 1, b)           # app addition: CREATE UNIQUE INDEX ... ON t (one column)
                return
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
        if col.nullable is None:                  # app addition
            col.nullable = True
        for x in range(j, b):
            if self.kw(x) == 'NOT' and self.kw(x + 1) == 'NULL':     # app addition
                col.nullable = False
            if self.kw(x) == 'PRIMARY' and self.kw(x + 1) == 'KEY':
                col.pk = tbl.ddl_pk = True
                col.nullable = False              # app addition
            elif self.kw(x) == 'UNIQUE' and self.outside_brackets(j, x):      # app addition
                col.unique = True
            elif self.kw(x) == 'REFERENCES':
                parts, nx = self.read_name(x + 1)
                refcols, _ = self.paren_names(nx)
                if parts:
                    self.foreign_key(tbl, [col.name], parts, refcols)

    def outside_brackets(self, a, x):
        """App addition: True when token x is not inside brackets that open at or after token a
        (a word of a CHECK (...) or DEFAULT (...) expression is not a word of the column's definition)."""
        k = a
        while k < x:
            if self.tx(k) == '(' and self.m[k] is not None:
                if self.m[k] >= x:
                    return False
                k = self.m[k] + 1
            else:
                k += 1
        return True

    def unique_index(self, k, b):
        """App addition: CREATE UNIQUE INDEX [IF NOT EXISTS] name ON table (column). One column, the whole
        table: an index of several columns, on an expression or with a WHERE clause makes no column unique."""
        while k < b and self.kw(k) != 'ON':
            k += 1
        parts, k = self.read_name(k + 1)
        if not parts or self.tx(k) != '(' or self.m[k] is None:
            return
        end = self.m[k]
        inside = [self.t[j] for j in range(k + 1, end)]
        if any(self.kw(j) == 'WHERE' for j in range(end + 1, b)):
            return
        if not inside or inside[0].kind not in IDENT or any(tok.text in (',', '(') for tok in inside):
            return
        key = '.'.join(p.upper() for p in parts)
        tbl = self.model.by_key.get(key) or next((t for t in self.model.tables if t.name.upper() == parts[-1].upper()), None)
        if tbl is not None and inside[0].text.upper() in tbl.columns:      # a column the table is known to have
            tbl.columns[inside[0].text.upper()].unique = True

    def table_constraint(self, tbl, a, b):
        u = self.kw(a)
        if u == 'UNIQUE':                         # app addition: UNIQUE [KEY | INDEX] [name] (one column)
            j = a + 1
            while j < b and self.tx(j) != '(':
                j += 1
            cols, _ = self.paren_names(j) if j < b else ([], j)
            if len(cols) == 1:
                tbl.col(cols[0]).unique = True
            return
        if u == 'PRIMARY':
            j = a + 2 if self.kw(a + 1) == 'KEY' else a + 1
            while self.kw(j) in ('CLUSTERED', 'NONCLUSTERED'):
                j += 1
            cols, _ = self.paren_names(j)
            for c in cols:
                tbl.col(c).pk = True
                tbl.col(c).nullable = False       # app addition
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
            elif self.kw(j) == 'UNIQUE' and self.tx(j + 1) == '(':      # app addition: ADD [CONSTRAINT n] UNIQUE (col)
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
