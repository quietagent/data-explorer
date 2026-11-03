"""Which columns of which tables a column of a query result comes from.

    origins(tokens, dialect, model, names)   one Origin per result column - or None where it is not known

The SQL tab masks personal columns. Whether a column is personal depends on its table: store.name
is the name of a shop, employee.first_name that of a person, and the user marks one column of one
table. So a result column has to be followed back to the table columns it is built from:

    SELECT s.name, e.first_name AS who, UPPER(e.last_name), COUNT(e.email)
    FROM store s JOIN employee e ON e.store_id = s.store_id

    name     store.name            as it is
    who      employee.first_name   as it is, under another name
    (third)  employee.last_name    built from it
    (fourth) a count               of rows, whatever it counts

What is followed: aliases in FROM and JOIN, schema-qualified and quoted names, subqueries in FROM,
WITH queries, every branch of UNION / INTERSECT / EXCEPT, and SELECT * / t.* (expanded from the
source's structure, in its order). A name without a table in front of it is looked for in the
tables of its own query block, then in the blocks around it. Names are compared the way the engine
keeps them: upper case on Oracle, lower case on PostgreSQL (a quoted name as written), without
regard to case elsewhere.

The statement is read with the guard's own reading of a statement (sqlguard._Names): its query
blocks, its FROM clauses with their tables, aliases and subqueries, its select lists. Only what
the names MEAN is new here.

Nothing is guessed. A column is "not known" - and the caller then judges it by its name alone, as
before - whenever something could not be followed for sure:

  * a table the source's structure does not have, a function or VALUES in FROM, PIVOT ...
  * a name that two tables of the FROM clause both have, or that no table in scope has
    (a column alias used as a value, a named window, a word this module does not know)
  * a WITH query that reads itself
  * a * whose tables are not all known, or a * of a join with USING / NATURAL (the engine merges
    columns there)
and the whole result is "not known" when the names the engine gave the result columns do not match
what was worked out (a table that has changed since the source was read, a syntax read wrongly).

A word that is not understood is never skipped: every name in an expression must be a column, a
table, a function in front of its bracket or one of WORDS - otherwise the column is not known.
That is what keeps SELECT TOP 5 first_name from being read as "TOP 5, called first_name".
"""

from __future__ import annotations

from ..sqlguard import (JOIN_WORDS, SET_OPERATORS, TYPE_CALLS, Known, ReadOnlyViolation, _Block, _is, _kw, _Names,
                        _Source)

COUNTS = ('COUNT', 'COUNT_BIG', 'APPROX_COUNT_DISTINCT')
EXACT_CASE = ('oracle', 'postgresql')       # a quoted name is another name than the unquoted one
MAIN_IS_THE_DATABASE = ('sqlite', 'duckdb') # main.customers is customers
# Words of SQL that may stand in an expression without being a name. Every one of them is a word to
# the guard's reading of a select list too (sqlguard._Names._word), so that a name behind it is never
# taken for an alias - except the ones that are a complete value (NULL, TRUE, SYSDATE ...), where it is
# one. Not here, on purpose: TOP, DISTINCTROW and the other words that can stand in front of a select
# list, and operators written as words (DIV, MOD, BINARY, COLLATE, CONNECT_BY_ROOT).
WORDS = frozenset('''
AND OR NOT NULL IS IN LIKE ILIKE BETWEEN CASE WHEN THEN ELSE END DISTINCT ALL ANY SOME EXISTS OVER PARTITION BY ORDER
ASC DESC NULLS FIRST LAST ROWS RANGE GROUPS UNBOUNDED PRECEDING FOLLOWING CURRENT ROW FILTER WHERE WITHIN GROUP TRUE
FALSE UNKNOWN ESCAPE FROM FOR BOTH LEADING TRAILING YEAR MONTH DAY HOUR MINUTE SECOND AT TIME ZONE LOCAL INTERVAL DATE
TIMESTAMP TIES RESPECT IGNORE PRIOR NOCYCLE SIBLINGS DENSE_RANK SYSDATE SYSTIMESTAMP CURRENT_DATE CURRENT_TIMESTAMP
LOCALTIMESTAMP USER ROWNUM LEVEL
'''.split())
AFTER_A_BRANCH = ('ORDER', 'LIMIT', 'OFFSET', 'FETCH')      # ( SELECT ... ) ORDER BY ...: sorts, gives no column
READ_IN_A_SUBQUERY = ('WHERE', 'GROUP', 'HAVING', 'ORDER', 'CONNECT', 'START')


class _Unsure(Exception):
    """Something in the statement could not be followed for sure."""


class Origin:
    """Where one column of a result comes from.

    columns   ((table, column name), ...): the columns of the source's tables it is built from, in
              the order they are written - `table` is the Table of the source's structure. Empty:
              built from no column (a constant, a count). None: not known.
    plain     True when it is one such column as it is, under its own name or another, in every
              branch of a UNION. False for anything built from columns.
    count     True for COUNT(...) and nothing else, in every branch.
    label     the name the statement gives it (an alias, or the column's own name), as written;
              None for an expression without a name.
    """
    __slots__ = ('name', 'label', 'columns', 'plain', 'count')

    def __init__(self, name, label, columns, plain=False, count=False):
        self.name, self.label, self.columns, self.plain, self.count = name, label, columns, plain, count

    def __repr__(self):
        if self.columns is None:
            return f'<{self.label}: not known>'
        what = ', '.join(f'{t.id}.{c}' for t, c in self.columns) or ('a count' if self.count else 'no column')
        return f'<{self.label}: {"" if self.plain or not self.columns else "built from "}{what}>'


class _Columns:
    """The columns a table, a subquery or a WITH query gives: in their order, and by name."""
    __slots__ = ('list', 'by_name')

    def __init__(self, columns):
        self.list, self.by_name = list(columns), {}
        for c in self.list:
            if c.name is not None:
                self.by_name[c.name] = None if c.name in self.by_name else c      # twice: which one is not known

    def has(self, name):
        return name in self.by_name

    def get(self, name):
        found = self.by_name.get(name)
        if found is None:
            raise _Unsure
        return found


class _Scope(_Block):
    """One SELECT. merged: its FROM joins with USING or NATURAL, so a column can belong to two tables."""
    __slots__ = ('merged',)

    def __init__(self):
        super().__init__()
        self.merged = False


_ITSELF = object()                          # a WITH query, while its own text is read


def only_counts(tokens):
    """True for COUNT(...) and nothing else: a count of a personal column is not personal."""
    if len(tokens) < 3 or tokens[0].kind != 'ident' or tokens[0].up not in COUNTS or not _is(tokens[1], '('):
        return False
    depth = 0
    for i, t in enumerate(tokens[1:], 1):
        if _is(t, '('):
            depth += 1
        elif _is(t, ')'):
            depth -= 1
            if depth == 0:
                return i == len(tokens) - 1
    return False


def _unique(columns):
    seen, out = set(), []
    for table, column in columns:
        if (id(table), column) not in seen:
            seen.add((id(table), column))
            out.append((table, column))
    return tuple(out)


def _gather(found):
    out = []
    for c in found:
        if c.columns is None:
            raise _Unsure
        out += c.columns
    return out


class _Lineage(_Names):
    """The guard's reading of a statement (query blocks, FROM clauses, select lists), with the tables
    of a source's structure behind the names instead of a judgement on them."""

    def __init__(self, toks, dialect, model):
        super().__init__(toks, dialect, Known())        # the brackets, Oracle's syntax, this engine's reserved words
        self.dialect, self.exact = dialect, dialect in EXACT_CASE
        self.tables, self.by_name, self.reading = {}, {}, set()
        for table in model.tables:
            if not any(o.kind != 'ref' for o in table.origins):
                continue                                # only known as the target of a key: its columns were not read
            columns = _Columns(Origin(self._key(c.name), c.name, ((table, c.name),), True) for c in table.columns)
            schema = self._key(table.schema) if table.schema else None
            self.tables.setdefault((schema, self._key(table.name)), []).append(columns)
            self.by_name.setdefault(self._key(table.name), []).append(columns)

    def run(self):
        found = self._query(0, len(self.toks), (), {})
        return None if found is None else found.list

    # ------------------------------------------------------------------ names
    def _key(self, name):
        """A name of the source's structure as names are compared on this engine."""
        return name if self.exact else name.upper()

    def _fold(self, tok):
        """A name of the statement as the engine keeps it."""
        if tok.kind == 'qident' and self.exact:
            return tok.text
        return tok.text.lower() if self.dialect == 'postgresql' else tok.up

    def _judge(self, *args, **kwargs):                  # the guard has judged the statement already
        pass

    def _declares(self, tok, what):
        pass

    # ----------------------------------------------------------------- blocks
    def _query(self, lo, hi, outer, ctes):
        """The columns a query gives, in their order - or None when they could not be followed."""
        try:
            return self._read(lo, hi, outer, ctes)
        except (_Unsure, ReadOnlyViolation):            # ReadOnlyViolation: the guard's reader could not follow it
            return None

    def _open(self, lo, hi, ctes):
        """A query without the brackets around it and without its WITH clause: (start, end, WITH queries)."""
        toks = self.toks
        while lo < hi and _is(toks[lo], '(') and self.match[lo] == hi - 1:
            lo, hi = lo + 1, hi - 1
        if lo >= hi:
            raise _Unsure
        if _kw(toks[lo], 'WITH'):
            ctes = dict(ctes)
            lo = self._with(lo, hi, ctes)
        return lo, hi, ctes

    def _branches(self, lo, hi):
        """[(start, end)] of the SELECTs that UNION / INTERSECT / EXCEPT / MINUS put together."""
        toks, out, start, i = self.toks, [], lo, lo
        while i < hi:
            if _is(toks[i], '('):
                i = self.match[i] + 1
                continue
            if _kw(toks[i], *SET_OPERATORS) and not _is(self._at(i - 1), '.'):
                out.append((start, i))
                i += 1
                if i < hi and _kw(toks[i], 'ALL', 'DISTINCT'):
                    i += 1
                start = i
                continue
            i += 1
        out.append((start, hi))
        if any(a >= b for a, b in out):
            raise _Unsure
        return out

    def _read(self, lo, hi, outer, ctes):
        lo, hi, ctes = self._open(lo, hi, ctes)
        results = [self._branch(a, b, outer, ctes) for a, b in self._branches(lo, hi)]
        if any(r is None for r in results) or len({len(r.list) for r in results}) != 1:
            return None
        if len(results) == 1:
            return results[0]
        merged = []                                     # a column of a UNION carries what any branch puts into it
        for i, first in enumerate(results[0].list):
            group = [r.list[i] for r in results]
            if any(c.columns is None for c in group):
                merged.append(Origin(first.name, first.label, None))
            else:
                merged.append(Origin(first.name, first.label, _unique(_gather(group)), all(c.plain for c in group),
                                     all(c.count for c in group)))
        return _Columns(merged)

    def _branch(self, a, b, outer, ctes):
        toks = self.toks
        if _is(toks[a], '('):                           # ( SELECT ... ) [ORDER BY ...]
            j = self.match[a]
            if j + 1 < b and not _kw(toks[j + 1], *AFTER_A_BRANCH):
                raise _Unsure
            return self._query(a + 1, j, outer, ctes)
        if _kw(toks[a], 'SELECT'):
            return self._block(a, b, outer, ctes)
        return None                                     # VALUES ...

    def _with(self, lo, hi, ctes):
        """WITH [RECURSIVE] name [(columns)] AS ( query ) [, ...]: fills ctes {name: its columns} and
        returns where the query itself starts."""
        toks, i, queries = self.toks, lo + 1, []
        if i < hi and _kw(toks[i], 'RECURSIVE'):
            i += 1
        while True:
            name = toks[i] if i < hi else None
            if name is None or name.kind not in ('ident', 'qident'):
                raise _Unsure
            i += 1
            listed = None
            if i < hi and _is(toks[i], '('):
                j = self.match[i]
                listed = [x for x in toks[i + 1:j] if x.kind in ('ident', 'qident')]
                i = j + 1
            if not (i < hi and _kw(toks[i], 'AS')):
                raise _Unsure
            i += 1
            if i < hi and _kw(toks[i], 'NOT'):
                i += 1
            if i < hi and _kw(toks[i], 'MATERIALIZED'):
                i += 1
            if not (i < hi and _is(toks[i], '(')):
                raise _Unsure
            queries.append((self._fold(name), listed, i + 1, self.match[i]))
            i = self.match[i] + 1
            if i < hi and _is(toks[i], ','):
                i += 1
                continue
            break
        # SQLite lets a WITH query read one that stands further down the list (and so does PostgreSQL under
        # RECURSIVE), where it then hides a table of that name. So every name of the list is taken from the
        # start: until its own query has been read, its columns are not known.
        for key, _, _, _ in queries:
            ctes[key] = None
        for key, listed, a, b in queries:
            inside = dict(ctes)
            inside[key] = _ITSELF
            self.reading.discard(key)
            columns = self._query(a, b, (), inside)
            if key in self.reading:                     # it reads itself (or a table of its own name): not followed
                self.reading.discard(key)
                columns = None
            if columns is not None and listed is not None:
                columns = (None if len(listed) != len(columns.list) else _Columns(
                    Origin(self._fold(x), x.text, c.columns, c.plain, c.count) for x, c in zip(listed, columns.list)))
            ctes[key] = columns
        return i

    def _scope(self, lo, hi, outer, ctes):
        """One SELECT, its tables read: (the block, its clauses, its ON conditions, its select items)."""
        block, conditions = _Scope(), []
        clauses = self._clauses(lo, hi)
        for word, a, b in clauses:
            if word == 'FROM':
                conditions = self._from(a, b, block, outer, ctes)
                block.merged = self._merges(a, b)
        items, more = self._items(self._select_start(clauses[0][1], clauses[0][2]), clauses[0][2])
        for _, _, alias, _, _ in items:
            if alias is not None:
                block.aliases.add(self._fold(alias))
        return block, clauses, conditions, items, more

    def _block(self, lo, hi, outer, ctes):
        """One SELECT: what each column of its select list comes from. None: the list hides its columns."""
        block, _, _, items, _ = self._scope(lo, hi, outer, ctes)
        here, out = (block,) + outer, []
        for a, end, alias, star, name in items:
            if star is not None:
                found = self._starred(star, block)
                if found is None:
                    return None
                out += [Origin(c.name, c.label, c.columns, c.plain, c.count) for s in found for c in s.columns.list]
            elif self._hidden_star(a, end):
                return None
            else:
                out.append(self._column(a, end, alias or name, here, ctes))
        return _Columns(out)

    def _select_start(self, lo, hi):
        """SQL Server: SELECT [ALL | DISTINCT] TOP (n) | n [PERCENT] [WITH TIES] - where the list starts."""
        toks, i = self.toks, lo
        if self.dialect != 'mssql':
            return lo
        if i < hi and _kw(toks[i], 'DISTINCT', 'ALL'):
            i += 1
        if not (i < hi and _kw(toks[i], 'TOP')):
            return lo
        i += 1
        if i < hi and _is(toks[i], '('):
            i = self.match[i] + 1
        elif i < hi and toks[i].kind == 'number':
            i += 1
        else:
            raise _Unsure
        if i < hi and _kw(toks[i], 'PERCENT'):
            i += 1
        if i + 1 < hi and _kw(toks[i], 'WITH') and _kw(toks[i + 1], 'TIES'):
            i += 2
        return i

    def _merges(self, lo, hi):
        """True when a FROM clause joins with USING or NATURAL (not inside its subqueries)."""
        toks, i = self.toks, lo
        while i < hi:
            if _is(toks[i], '(') and self._is_query(i + 1, self.match[i]):
                i = self.match[i] + 1
                continue
            if _kw(toks[i], 'USING', 'NATURAL') and not _is(self._at(i - 1), '.'):
                return True
            i += 1
        return False

    def _starred(self, star, block):
        """The tables a * or t.* stands for, in the order of the FROM clause - or None when their columns
        are not all known."""
        if star:
            wanted = self._fold(star[-1])
            found = [s for s in block.sources if wanted in s.names]
            if len(found) != 1:
                return None
        else:
            found = list(block.sources)
            if block.merged or not found:               # USING / NATURAL: the engine shows the joined columns once
                return None
        return None if any(s.columns is None for s in found) else found

    def _hidden_star(self, a, end):
        """True when a select item holds a * that is not a multiplication (* EXCLUDE ( ... ), (t).*, a word
        in front of a *): it gives more columns than one, and which ones is not known."""
        toks, k = self.toks, a
        while k < end:
            if _is(toks[k], '('):
                k = self.match[k] + 1
                continue
            if _is(toks[k], '*') and (k == a or k == end - 1 or not self._operand_ends(toks[k - 1])):
                return True
            k += 1
        return False

    @staticmethod
    def _operand_ends(tok):
        return tok.kind in ('number', 'string', 'ident', 'qident') or tok.kind == 'op' and tok.text in (')', ']')

    # ------------------------------------------------------------------- FROM
    def _table(self, parts, ctes):
        source = _Source()
        names = [self._fold(p) for p in parts]
        source.table, source.schema = names[-1], names[-2] if len(names) > 1 else None
        source.names = {names[-1]}
        if len(names) == 1 and names[0] in ctes:
            source.columns, source.table = ctes[names[0]], None
            if source.columns is _ITSELF:
                self.reading.add(names[0])
                source.columns = None
        elif self.oracle and names == ['DUAL'] and 'DUAL' not in self.by_name:
            source.columns = _Columns([Origin('DUMMY', 'DUMMY', ())])
        else:
            found = self.tables.get((source.schema, source.table)) if source.schema else self.by_name.get(source.table)
            if found is None and source.schema == 'MAIN' and self.dialect in MAIN_IS_THE_DATABASE:
                found = self.tables.get((None, source.table))
            source.columns = found[0] if found and len(found) == 1 else None    # two tables of that name: which is not known
        return source

    def _after(self, i, hi, source):
        toks, given = self.toks, source.columns
        if self.dialect == 'mssql':                     # FROM t [AS a] WITH (NOLOCK): a table hint, not a name
            k = i
            while k < hi and not (_is(toks[k], ',') or _kw(toks[k], 'ON', 'USING', 'WITH', *JOIN_WORDS)):
                k = self.match[k] + 1 if _is(toks[k], '(') else k + 1
            if k + 1 < hi and _kw(toks[k], 'WITH') and _is(toks[k + 1], '('):
                super()._after(i, k, source)
                i = self.match[k + 1] + 1
        i = super()._after(i, hi, source)
        if source.columns is not given:                 # AS t (a, b): the columns are renamed by their place
            source.columns = None
        return i

    def _find(self, name, chain):
        """The table of a FROM clause that answers to this name, the nearest query block first."""
        for block in chain:
            found = [s for s in block.sources if name in s.names]
            if len(found) > 1:
                raise _Unsure                           # two tables under one name: the engine refuses it, or picks one
            if found:
                return found[0]
        return None

    # ------------------------------------------------------------ expressions
    def _column(self, a, end, named, here, ctes):
        """One select item (without its alias): what it comes from."""
        key, label = (self._fold(named), named.text) if named is not None else (None, None)
        if only_counts(self.toks[a:end]):
            return Origin(key, label, (), False, True)
        try:
            chain = self._chain(a, end)
            if chain is None:
                return Origin(key, label, _unique(self._inputs(a, end, here, ctes)))
            found, plain = self._dotted(chain, here) if len(chain) > 1 else self._bare(chain[0], here)
            if plain:                                   # one column as it is: what it was, under this name
                return Origin(key, label, tuple(_gather(found)), found[0].plain, found[0].count)
            return Origin(key, label, _unique(_gather(found)))
        except (_Unsure, ReadOnlyViolation):
            return Origin(key, label, None)

    def _bare(self, tok, chain, known=frozenset()):
        """A name with no table in front of it: ([the columns it stands for], True when it is one column
        as it is). known: column aliases whose own columns are counted already."""
        name = self._fold(tok)
        for block in chain:
            hits = [s for s in block.sources if s.columns is not None and s.columns.has(name)]
            unknown = any(s.columns is None for s in block.sources)
            if len(hits) == 1 and not unknown:
                return [hits[0].columns.get(name)], True
            if hits or unknown:
                raise _Unsure                           # in two tables of this FROM, or maybe in one that is not known
        # every table in scope is known, and none of them has a column of this name
        source = self._find(name, chain)
        if source is not None:
            return list(source.columns.list), False     # the table itself: SELECT c FROM customers c is every column of it
        if any(name in block.aliases for block in chain):
            if name in known:
                return [], False
            raise _Unsure                               # a column alias used as a value
        if tok.kind == 'ident' and tok.up in WORDS:
            return [], False
        raise _Unsure

    def _dotted(self, parts, chain):
        """t.column, schema.table.column, t.*: ([the columns], True when it is one column as it is)."""
        star = _is(parts[-1], '*')
        names = [self._fold(p) for p in (parts[:-1] if star else parts)]
        source, rest = self._find(names[0], chain), names[1:]
        if source is None and len(names) >= (2 if star else 3):
            at = len(names) - (1 if star else 2)        # schema.table: a table that stands in FROM under its own name
            source = next((s for b in chain for s in b.sources if s.table == names[at] and s.schema in (None, names[at - 1])),
                          None)
            rest = names[at + 1:]
        if source is None:
            if not star and len(names) == 2:            # column.field: a part of a column
                found, plain = self._bare(parts[0], chain)
                if plain:
                    return found, False
            raise _Unsure
        if source.columns is None or bool(rest) == star:
            raise _Unsure
        if star:
            return list(source.columns.list), False
        return [source.columns.get(rest[0])], len(rest) == 1

    def _inputs(self, lo, hi, chain, ctes, call=None, known=frozenset()):
        """The table columns an expression is built from, as written. Raises _Unsure for a name that is
        neither a column, a table, a function in front of its bracket nor a word of SQL."""
        toks, out, i = self.toks, [], lo
        while i < hi:
            t = toks[i]
            if _is(t, '('):
                j, head = self.match[i], self._at(i - 1)
                name = head.up if head is not None and head.kind == 'ident' and not _is(self._at(i - 2), '.') else None
                if self._is_query(i + 1, j):            # EXISTS ( ... ): whether there are rows, not what they hold
                    out += self._subquery(i + 1, j, chain, ctes, listed=name != 'EXISTS')
                else:
                    end = j
                    if name in TYPE_CALLS:              # CAST(x AS NUMBER(10)): what follows AS is a type
                        end = i + 1
                        while end < j and not _kw(toks[end], 'AS'):
                            end = self.match[end] + 1 if _is(toks[end], '(') else end + 1
                    out += self._inputs(i + 1, end, chain, ctes, name, known)
                i = j + 1
                if _is(self._at(i), '.'):
                    raise _Unsure                       # ( ... ).field
                continue
            if t.kind == 'op':
                if t.text == '*':
                    if i == lo and i + 1 == hi:         # f(*)
                        if call not in COUNTS:
                            raise _Unsure
                    elif i == lo or i + 1 >= hi or not self._operand_ends(toks[i - 1]):
                        raise _Unsure                   # not a multiplication
                elif t.text == '::':                    # x::numeric(10, 2): a type follows
                    if not (i + 1 < hi and toks[i + 1].kind in ('ident', 'qident')):
                        raise _Unsure
                    i += 2
                    if i < hi and _is(toks[i], '('):
                        i = self.match[i] + 1
                    continue
                elif t.text == '.':
                    raise _Unsure
                i += 1
                continue
            if t.kind not in ('ident', 'qident'):
                i += 1
                continue
            parts, k = [t], i + 1
            while k + 1 < hi and _is(toks[k], '.') and (toks[k + 1].kind in ('ident', 'qident') or _is(toks[k + 1], '*')):
                parts.append(toks[k + 1])
                k += 2
            if k < hi and _is(toks[k], '('):
                if _is(self._at(k + 1), '+') and _is(self._at(k + 2), ')'):
                    k += 3                              # Oracle's outer join marker: b.id (+)
                elif len(parts) > 1:
                    raise _Unsure                       # schema.function( ... )
                else:
                    i = k                               # a function, or a word in front of a bracket: its inside is next
                    continue
            found, _ = self._dotted(parts, chain) if len(parts) > 1 else self._bare(t, chain, known)
            out += _gather(found)
            i = k
        return out

    def _subquery(self, lo, hi, chain, ctes, listed=True):
        """A subquery inside an expression: every table column it reads - in its select list, its joins
        and its conditions alike. (SELECT 'high' FROM t WHERE t.salary > x) is built from the salary.
        listed=False: without its select list. A COUNT(...) in the list is a number of rows there too."""
        toks, out = self.toks, []
        lo, hi, ctes = self._open(lo, hi, ctes)
        for a, b in self._branches(lo, hi):
            if _is(toks[a], '('):
                j = self.match[a]
                if j + 1 < b and not _kw(toks[j + 1], *AFTER_A_BRANCH):
                    raise _Unsure
                out += self._subquery(a + 1, j, chain, ctes, listed)
                continue
            if not _kw(toks[a], 'SELECT'):
                raise _Unsure
            block, clauses, conditions, items, more = self._scope(a, b, chain, ctes)
            if block.merged:
                raise _Unsure
            here, known = (block,) + chain, frozenset(block.aliases)
            for x, y in more:
                out += self._inputs(x, y, here, ctes)
            for x, end, alias, star, name in items if listed else ():
                if star is not None:
                    found = self._starred(star, block)
                    if found is None:
                        raise _Unsure
                    out += _gather(c for s in found for c in s.columns.list)
                elif self._hidden_star(x, end):
                    raise _Unsure
                elif not only_counts(toks[x:end]):
                    out += self._inputs(x, end, here, ctes)
            for x, y in conditions:
                out += self._inputs(x, y, here, ctes, None, known)
            for word, x, y in clauses[1:]:
                if word in READ_IN_A_SUBQUERY:
                    out += self._inputs(x, y, here, ctes, None, known)
        return out


def origins(tokens, dialect, model, names=None):
    """What each column of a statement's result comes from: [Origin, or None where it is not known],
    or None when nothing of the result is known.

    tokens    the statement as the guard read it (Checked.tokens)
    dialect   the engine it runs on
    model     the structure of the source it runs on (a SchemaModel)
    names     the names the engine gave the result's columns. With them the answer is checked: as
              many columns, and every name that the statement itself gives (an alias, a column, the
              columns of a *) where it was expected. If not, nothing is known.
    """
    try:
        found = _Lineage(tokens, dialect, model).run()
    except Exception:                                   # whatever it is: not known, and the caller falls back
        return None
    if found is None:
        return None
    if names is not None:
        if len(found) != len(names):
            return None
        if any(o.label is not None and o.label.upper() != str(actual).upper() for o, actual in zip(found, names)):
            return None
    return [o if o.columns is not None else None for o in found]
