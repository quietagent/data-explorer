"""Reads one parsed statement (an sqlglot tree) into StatementFacts.

What it records:
- the statement type: SELECT, INSERT, MERGE, UPDATE, DELETE, CREATE VIEW,
  CREATE TABLE AS, or another type (CREATE TABLE, DROP TABLE ...);
- tables read (FROM, JOIN, subqueries, every UNION branch, CTEs followed to
  their tables; a CTE name is never a table) and tables written;
- tables named by DDL: CREATE TABLE (without AS), CREATE INDEX, DROP TABLE or
  VIEW, TRUNCATE and ALTER TABLE. DDL is not a write;
- each column with its role: select, join, filter, group, order, set or ddl
  (SELECT * and t.* are recorded as column "*", never expanded). The columns
  a CREATE defines are recorded as "*" of the new table (the key's
  convention): role ddl for CREATE TABLE, set for CREATE TABLE AS, CREATE
  VIEW and SELECT INTO. CREATE INDEX names its columns (role ddl);
- each join: explicit JOIN ... ON / USING, comma joins with a condition in
  WHERE (type "where"), Oracle (+) (type left or right), MERGE ... ON (type
  "merge"), and cross joins;
- subquery links, recorded as joins of type "subquery": x IN / NOT IN
  (SELECT y ...) links x to y, and an equality inside a correlated subquery
  (EXISTS, NOT EXISTS, a scalar subquery) links a table of the subquery to an
  outer one. link_kind says which: in, not_in, exists, not_exists or scalar;
- unknowns: a column whose table is not certain, a USING or NATURAL join whose
  columns are not certain, a source that is not followed, a table name that
  several schemas have. Never a guess.

With a schema, every table name goes through Schema.resolve (key_of below): a
name written without its schema becomes the schema's full key when that is
certain, and its columns follow it.

Joins are grouped per pair of sources in a scope: every equality between the
same two sources (in ON and in WHERE) is one join with several column pairs.
A side that is a CTE or subquery is followed back to its base table when every
column of the join traces to that one table; otherwise the join is recorded
against the CTE or subquery name and marked derived.
"""

from sqlglot import exp

from sql_lineage import settings
from sql_lineage.parse import joins as J
from sql_lineage.parse.facts import ColumnUse, JoinUse, StatementFacts, Unknown
from sql_lineage.parse.names import fold_identifier, table_key
from sql_lineage.parse.scopes import CteDef, Output, Resolver, Scope, Source

# Which role a column has, by the SELECT clause it sits in, and whether that
# clause may name a select-list alias instead of a column.
CLAUSE_ROLES = (
    ("expressions", "select", False),
    ("distinct", "select", False),
    ("windows", "select", False),
    ("where", "filter", False),
    ("prewhere", "filter", False),
    ("connect", "filter", False),
    ("group", "group", True),
    ("having", "filter", True),
    ("qualify", "filter", True),
    ("order", "order", True),
)

QUERY_TYPES = (exp.Select, exp.SetOperation)


class StatementReader:
    def __init__(self, raw, tree, dialect, schema):
        self.raw = raw
        self.tree = tree
        self.dialect = dialect
        self.schema = schema
        self.resolver = Resolver(dialect, schema)
        self.facts = StatementFacts(script=raw.script, number=raw.number, type="", status="read", reason=None)
        self.done = set()               # ids of sqlglot nodes already read as a scope
        self.join_columns = set()       # ids of Column nodes that are part of a join condition
        self.seen_unknowns = set()
        self.seen_uses = set()

    # -- entry --------------------------------------------------------------

    def read(self):
        tree = self.tree
        if isinstance(tree, QUERY_TYPES):
            self._query_statement(tree)
        elif isinstance(tree, exp.Insert):
            self._insert(tree)
        elif isinstance(tree, exp.Update):
            self._update(tree)
        elif isinstance(tree, exp.Delete):
            self._delete(tree)
        elif isinstance(tree, exp.Merge):
            self._merge(tree)
        elif isinstance(tree, exp.Create):
            self._create(tree)
        else:
            self.facts.type = other_type(tree)
            self._other_ddl(tree)
        return self.facts

    def _query_statement(self, tree):
        into = tree.args.get("into") if isinstance(tree, exp.Select) else None
        if into is not None and isinstance(into.this, exp.Table):
            self.facts.type = "CREATE TABLE AS"
            target = self.key_of(into.this)
            self.facts.add_write(target, self.line(into))
            self.done.add(id(into))
            self.query(tree, None, {})
            self.use(target, "*", "set", into)
        else:
            self.facts.type = "SELECT"
            self.query(tree, None, {})

    # -- queries ------------------------------------------------------------

    def query(self, node, parent, ctes, link_kind=""):
        """Read a SELECT / UNION / subquery as a Scope, with its CTEs.
        link_kind: how a nested query is used (in, exists, scalar ...), for its subquery links."""
        while isinstance(node, (exp.Subquery, exp.Paren)):
            self.done.add(id(node))
            node = node.this
        self.done.add(id(node))
        ctes = self._with(node, parent, ctes)

        if isinstance(node, exp.SetOperation):
            scope = Scope("union", parent, link_kind=link_kind)
            for side in (node.this, node.expression):
                branch = self.query(side, parent, ctes, link_kind)
                scope.branches += branch.branches if branch.kind == "union" else [branch]
            return scope

        if not isinstance(node, exp.Select):
            scope = Scope("other", parent, link_kind=link_kind)
            self.walk(node, scope, "select", ctes)
            return scope

        scope = Scope("select", parent, link_kind=link_kind)
        items = []
        from_ = node.args.get("from_")
        if from_ is not None:
            items.append((from_.this, None))
            items += [(j.this, j) for j in from_.this.args.get("joins") or []]
        items += [(j.this, j) for j in node.args.get("joins") or []]
        for lateral in node.args.get("laterals") or []:
            items.append((lateral, None))
        self.add_sources(scope, items, ctes)

        for item in node.expressions:
            self._add_output(scope, item)
            if isinstance(item, exp.Star):      # SELECT *: recorded as TABLE.* for each table
                for source in scope.sources:
                    if source.kind == "table":
                        self.use(source.key, "*", "select", item)

        self.read_joins(scope, node.args.get("where"), ctes)
        for arg, role, alias_ok in CLAUSE_ROLES:
            value = node.args.get(arg)
            if value is None:
                continue
            for part in value if isinstance(value, list) else [value]:
                if isinstance(part, exp.Expression):
                    self.walk(part, scope, role, ctes, alias_ok)
        return scope

    def _with(self, node, parent, ctes):
        """Read a WITH clause; returns the CTEs visible from `node`."""
        with_ = node.args.get("with_") if isinstance(node, exp.Expression) else None
        if with_ is None:
            return ctes
        self.done.add(id(with_))
        ctes = dict(ctes)
        for cte in with_.expressions:
            name = fold_identifier(cte.args["alias"].this, self.dialect)
            definition = CteDef(name)
            alias = cte.args.get("alias")
            if alias is not None and alias.columns:
                definition.columns = [fold_identifier(c, self.dialect) for c in alias.columns]
            ctes[name] = definition     # visible to itself, for a recursive CTE
            self.done.add(id(cte))
            definition.scope = self.query(cte.this, parent, ctes)
            definition.scope.cte_columns = definition.columns
        return ctes

    def _add_output(self, scope, item):
        if isinstance(item, exp.Star):
            scope.star_all = True
            scope.outputs.append(Output(None, item))
        elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
            scope.star_aliases.append(fold_identifier(item.args.get("table"), self.dialect))
            scope.outputs.append(Output(None, item))
        elif isinstance(item, exp.Alias):
            scope.outputs.append(Output(fold_identifier(item.args.get("alias"), self.dialect), item.this,
                                        renamed=True))
        elif isinstance(item, exp.Column):
            scope.outputs.append(Output(fold_identifier(item.this, self.dialect), item))
        else:
            scope.outputs.append(Output(None, item))

    # -- sources --------------------------------------------------------------

    def add_sources(self, scope, items, ctes, record_reads=True):
        """Add FROM / JOIN items to a scope, and record their tables as read.
        A DML statement passes record_reads=False and records them once it knows its target."""
        for node, join in items:
            source = self.make_source(node, scope, ctes)
            scope.add(source)
            scope.join_items.append((source, join))
        if record_reads:
            self.record_reads(scope)

    def record_reads(self, scope):
        for source in scope.sources:
            if source.kind == "table" and not source.written:
                self.facts.add_read(source.key, source.line)

    def make_source(self, node, scope, ctes):
        source = self._make_source(node, scope, ctes)
        source.line = self.line(node)
        return source

    def _make_source(self, node, scope, ctes):
        alias = fold_identifier(node.args["alias"].this, self.dialect) \
            if isinstance(node.args.get("alias"), exp.TableAlias) and node.args["alias"].this else ""
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            name = fold_identifier(node.this, self.dialect)
            if not node.args.get("db") and name in ctes:
                return Source(alias or name, "derived", name=name, is_cte=True, cte=ctes[name])
            key = table_key(node, self.dialect)
            if key in settings.DUMMY_TABLES:
                return Source(alias or name, "dummy", key=key)
            return Source(alias or name, "table", key=self.key_of(node))
        if isinstance(node, exp.Subquery):
            child = self.query(node.this, scope.parent, ctes)
            label = alias or f"subquery{len(scope.sources) + 1}"
            return Source(label, "derived", name=label, subquery_scope=child)
        self.unknown("source", node.sql(dialect=self.dialect)[:80],
                     f"a {type(node).__name__} in FROM is not followed", node)
        self.walk(node, scope, "select", ctes)
        return Source(alias or f"source{len(scope.sources) + 1}", "unknown")

    # -- columns --------------------------------------------------------------

    def walk(self, node, scope, role, ctes, alias_ok=False):
        """Record every column under `node` with `role`; nested queries become child scopes.
        alias_ok: the clause may name a select-list alias (ORDER BY, GROUP BY, HAVING)."""
        stack = [node]
        while stack:
            current = stack.pop()
            if id(current) in self.done:
                continue
            if isinstance(current, (exp.Subquery,) + QUERY_TYPES):
                self.query(current, scope, ctes, subquery_kind(current))
                continue
            if isinstance(current, exp.Column):
                self.column(current, scope, role, alias_ok)
                continue
            if isinstance(current, exp.In) and current.args.get("query") is not None \
                    and id(current.args["query"]) not in self.done:
                self._in_link(current, scope, ctes)
            stack.extend(reversed(list(current.iter_expressions())))

    def column(self, column, scope, role, alias_ok=False):
        if isinstance(column.this, exp.Star):
            qualifier = fold_identifier(column.args.get("table"), self.dialect)
            source = self.resolver.resolve(scope, column) if qualifier else None
            if isinstance(source, Source) and source.kind == "table":
                self.use(source.key, "*", "select", column)
            return
        if not isinstance(column.this, exp.Identifier):
            return
        name = fold_identifier(column.this, self.dialect)
        qualifier = column.args.get("table")
        if name.upper() in settings.PSEUDO_COLUMNS and (not qualifier or name.upper() in ("NEXTVAL", "CURRVAL")):
            return
        if alias_ok and not qualifier and name in scope.alias_names():
            return
        if id(column) in self.join_columns:
            role = "join"
        source = self.resolver.resolve(scope, column)
        if isinstance(source, str):
            self.unknown("column", column.sql(dialect=self.dialect), source, column)
            return
        self.use_through(source, name, role, column)

    def use_through(self, source, name, role, node):
        """Record a column use on a source, following a derived source to its base tables."""
        if source.kind == "table":
            self.use(source.key, name, role, node)
        elif source.kind == "derived":
            for table, column in self.resolver.trace(source, name) or []:
                self.use(table, column, role, node)

    def use(self, table, column, role, node):
        key = (table, column, role, self.line(node))
        if key in self.seen_uses:
            return
        self.seen_uses.add(key)
        self.facts.columns.append(ColumnUse(table, column, role, key[3]))

    # -- joins ----------------------------------------------------------------

    def read_joins(self, scope, where, ctes, extra_type=None):
        """Find the joins of a scope in its JOIN clauses and its WHERE conditions."""
        links = []
        for source, join in scope.join_items:
            if join is None:
                continue
            join_type = join_type_of(join)
            if join.args.get("method") and join.args["method"].upper() == "NATURAL":
                self.unknown("join", f"NATURAL JOIN {source.alias}",
                             "the columns of a NATURAL JOIN are not read", join)
                continue
            on = join.args.get("on")
            if on is not None:
                for part in J.split_and(on):
                    self._condition(part, scope, join_type or "inner", "on", links, part)
                self.walk(on, scope, "filter", ctes)
            for identifier in join.args.get("using") or []:
                self._using(scope, source, identifier, join_type or "inner", links, join)
        if where is not None:
            for part in J.split_and(where.this):
                self._condition(part, scope, "where", "where", links, part)
        self._make_joins(scope, links)

    def _condition(self, node, scope, join_type, origin, links, line_node):
        """Turn one AND-part of an ON or WHERE into a join link, when it ties two sources."""
        columns = [c for c in J.own_columns(node) if not isinstance(c.this, exp.Star)
                   and fold_identifier(c.this, self.dialect).upper() not in settings.PSEUDO_COLUMNS]
        if not columns:
            return
        sources = []
        for column in columns:
            source = self.resolver.resolve(scope, column)
            if isinstance(source, str):
                scope.unsure = True     # recorded as an unknown column when the clause is walked
                return
            if source not in sources:
                sources.append(source)
        if len(sources) < 2:
            return
        if len(sources) > 2:
            self.unknown("join", J.unwrap(node).sql(dialect=self.dialect)[:120],
                         "one condition ties more than two tables", line_node)
            self.join_columns.update(id(c) for c in columns)
            return
        a, b = sorted(sources, key=_order)
        kind = ""
        if a not in scope.sources or b not in scope.sources:
            # A correlated subquery: the condition ties a table of the subquery to an outer one.
            if a in scope.sources:
                a, b = b, a     # the outer table first
            join_type, kind = "subquery", _link_kind(scope)
        elif origin == "where":
            # Oracle (+) marks the optional side: a, b WHERE a.x = b.x(+) is a LEFT JOIN b.
            marked = [self.resolver.resolve(scope, c) for c in columns if c.args.get("join_mark")]
            if not marked:
                join_type = "where"
            else:
                join_type = "left" if marked[0] is b else "right"
        equality = J.plain_equality(node)
        link = {"a": a, "b": b, "type": join_type, "kind": kind, "origin": origin, "line": line_node,
                "pairs": [], "conditions": [], "columns": columns}
        if equality and len({id(self.resolver.resolve(scope, c)) for c in equality}) == 2:
            left, right = equality
            if self.resolver.resolve(scope, left) is not a:
                left, right = right, left
            link["pairs"].append((fold_identifier(left.this, self.dialect),
                                  fold_identifier(right.this, self.dialect)))
        else:
            link["conditions"].append(J.unwrap(node))
        self.join_columns.update(id(c) for c in columns)
        links.append(link)

    def _using(self, scope, right, identifier, join_type, links, join):
        name = fold_identifier(identifier, self.dialect)
        earlier = [s for s in scope.sources if s.position < right.position]
        answers = [(s, self.resolver.has_column(s, name)) for s in earlier]
        yes = [s for s, a in answers if a == "yes"]
        maybe = [s for s, a in answers if a == "maybe"]
        if len(yes) == 1 and not maybe:
            left = yes[0]
        elif not yes and len(maybe) == 1:
            left = maybe[0]
        else:
            self.unknown("join", f"USING ({name})", "the table on the left of USING is not certain", join)
            return
        self.use_through(left, name, "join", join)
        self.use_through(right, name, "join", join)
        links.append({"a": left, "b": right, "type": join_type, "kind": "", "origin": "on", "line": join,
                      "pairs": [(name, name)], "conditions": [], "columns": []})

    def _in_link(self, node, scope, ctes):
        """x IN (SELECT y FROM ...): read the subquery, and link x to y as a subquery link.
        (a, b) IN (SELECT x, y ...) pairs the columns by position."""
        query = node.args["query"]
        inner = query
        while isinstance(inner, (exp.Subquery, exp.Paren)):
            inner = inner.this
        outer_columns = node.this.expressions if isinstance(node.this, exp.Tuple) else [node.this]
        outputs = [o.this if isinstance(o, exp.Alias) else o for o in inner.expressions] \
            if isinstance(inner, exp.Select) else []
        pairs = [(x, y) for x, y in zip(outer_columns, outputs)
                 if isinstance(x, exp.Column) and isinstance(y, exp.Column)
                 and isinstance(x.this, exp.Identifier) and isinstance(y.this, exp.Identifier)]
        self.join_columns.update(id(c) for pair in pairs for c in pair)
        child = self.query(query, scope, ctes, subquery_kind(query))
        links = []
        for x, y in pairs:
            a, b = self.resolver.resolve(scope, x), self.resolver.resolve(child, y)
            if isinstance(a, str) or isinstance(b, str) or a is b:
                continue
            links.append({"a": a, "b": b, "type": "subquery", "kind": child.link_kind, "origin": "where",
                          "line": node, "pairs": [(fold_identifier(x.this, self.dialect),
                                                   fold_identifier(y.this, self.dialect))],
                          "conditions": [], "columns": []})
        self._group_links(scope, links)

    def _make_joins(self, scope, links):
        self._group_links(scope, links)
        self._cross_joins(scope, links)

    def _group_links(self, scope, links):
        """Links between the same two sources become one join with all their pairs."""
        groups = {}
        for link in links:
            groups.setdefault((id(link["a"]), id(link["b"])), []).append(link)
        for group in groups.values():
            a, b = group[0]["a"], group[0]["b"]
            types = [g["type"] for g in group]
            on_types = [g["type"] for g in group if g["origin"] == "on"]
            other_types = [t for t in types if t != "where"]
            if "subquery" in types:
                join_type = "subquery"
            else:
                join_type = on_types[0] if on_types else (other_types[0] if other_types else "where")
            pairs = [p for g in group for p in g["pairs"]]
            conditions = [c for g in group for c in g["conditions"]]
            columns = [c for g in group for c in g["columns"]]
            self._add_join(scope, a, b, pairs, conditions, columns, join_type, group[0]["line"],
                           group[0]["kind"])

    def _cross_joins(self, scope, links):
        """A comma-joined or CROSS JOINed source tied to no other source by any condition is a
        cross join, recorded with the first source of the FROM clause. Nothing is recorded
        when a condition of the scope could not be read (it may have tied them)."""
        if scope.unsure:
            return
        own = [s for s in scope.sources if s.kind in ("table", "derived")]
        if len(own) < 2:
            return
        group = {id(s): id(s) for s in own}     # each source starts in a group of its own

        def root(source_id):
            while group[source_id] != source_id:
                source_id = group[source_id]
            return source_id

        for link in links:
            a, b = id(link["a"]), id(link["b"])
            if a in group and b in group:
                group[root(b)] = root(a)
        joins = {id(s): j for s, j in scope.join_items}
        first = own[0]
        joined = {root(id(first))}
        for source in own[1:]:
            join = joins.get(id(source))
            explicit = join is not None and (join.args.get("on") is not None or join.args.get("using"))
            if root(id(source)) in joined or explicit:
                continue
            joined.add(root(id(source)))
            self._add_join(scope, first, source, [], [], [], "cross", join if join is not None else self.tree)

    def _add_join(self, scope, a, b, pairs, conditions, columns, join_type, line_node, link_kind=""):
        names_a = [p[0] for p in pairs]
        names_b = [p[1] for p in pairs]
        mapping = {}
        for column in columns:
            source = self.resolver.resolve(scope, column)
            name = fold_identifier(column.this, self.dialect)
            if source is a:
                names_a.append(name)
            elif source is b:
                names_b.append(name)
        side_a = self._side(a, names_a)
        side_b = self._side(b, names_b)
        label_a = side_a[0] if side_a else self._derived_label(a)
        label_b = side_b[0] if side_b else self._derived_label(b)
        map_a = side_a[1] if side_a else {n: n for n in names_a}
        map_b = side_b[1] if side_b else {n: n for n in names_b}
        base_pairs = [(map_a[x], map_b[y]) for x, y in pairs]

        for column in columns:
            source = self.resolver.resolve(scope, column)
            name = fold_identifier(column.this, self.dialect)
            written = (column.args["table"].name.upper() if column.args.get("table") else "", column.name.upper())
            if source is a:
                mapping[written] = (label_a, map_a.get(name, name))
            elif source is b:
                mapping[written] = (label_b, map_b.get(name, name))
        texts = [J.render(c, mapping, self.dialect) for c in conditions]

        on_columns = []
        for label, side, names in ((label_a, side_a, names_a), (label_b, side_b, names_b)):
            if side:
                for name in names:
                    if (label, side[1][name]) not in on_columns:
                        on_columns.append((label, side[1][name]))

        key, table1, table2, sorted_pairs = J.canonical(label_a, label_b, base_pairs, texts)
        followed = bool(side_a and side_b) and "derived" in (a.kind, b.kind)
        self.facts.joins.append(JoinUse(
            key=key, table1=table1, table2=table2, pairs=sorted_pairs, conditions=sorted(texts),
            join_type=join_type, left_table=label_a, derived=not (side_a and side_b),
            non_key=bool(texts) or not sorted_pairs, line=self.line(line_node), on_columns=on_columns,
            link_kind=link_kind, via_cte=followed))

    def _side(self, source, names):
        """(table key, {column: base column}) when every column traces to one base table."""
        if source.kind == "table":
            return source.key, {n: n for n in names}
        if source.kind != "derived":
            return None
        table = None
        mapping = {}
        for name in names:
            traced = self.resolver.trace(source, name)
            if not traced or len(traced) != 1:
                return None
            if table is not None and traced[0][0] != table:
                return None
            table = traced[0][0]
            mapping[name] = traced[0][1]
        if table is None:
            return None
        return table, mapping

    def _derived_label(self, source):
        if source.kind == "derived":
            what = "CTE" if source.is_cte else "subquery"
            return f"{source.name} [{what} in {self.raw.script} #{self.raw.number}]"
        return f"{source.alias} [{source.kind} in {self.raw.script} #{self.raw.number}]"

    # -- DML and DDL ----------------------------------------------------------

    def _insert(self, tree):
        self.facts.type = "INSERT"
        ctes = self._with(tree, None, {})
        target = tree.this
        columns = []
        if isinstance(target, exp.Schema):
            columns = [fold_identifier(c, self.dialect) for c in target.expressions]
            target = target.this
        if not isinstance(target, exp.Table):
            self.unknown("table", target.sql(dialect=self.dialect)[:80], "INSERT target not read", tree)
            return
        key = self.key_of(target)
        self.facts.add_write(key, self.line(target))
        for name in columns:
            self.use(key, name, "set", target)
        source = tree.expression
        if source is None:
            return
        if isinstance(source, (exp.Subquery, exp.Paren) + QUERY_TYPES):
            self.query(source, None, ctes)
        else:
            self.walk(source, Scope("other"), "select", ctes)

    def _create(self, tree):
        kind = (tree.args.get("kind") or "").upper()
        query = tree.expression
        if kind not in ("TABLE", "VIEW") or not isinstance(query, (exp.Subquery,) + QUERY_TYPES):
            self.facts.type = f"CREATE {kind}".strip()
            if kind == "TABLE":
                key = self._ddl(tree.this)
                if key:
                    self.use(key, "*", "ddl", tree.this)
            elif kind == "INDEX" and isinstance(tree.this, exp.Index):
                key = self._ddl(tree.this.args.get("table"))
                params = tree.this.args.get("params")
                for item in (params.args.get("columns") or []) if key and params is not None else []:
                    column = item.this if isinstance(item, exp.Ordered) else item
                    if isinstance(column, exp.Column) and isinstance(column.this, exp.Identifier):
                        self.use(key, fold_identifier(column.this, self.dialect), "ddl", column)
            return
        target = tree.this
        if isinstance(target, exp.Schema):
            target = target.this
        key = self.key_of(target)
        self.facts.type = "CREATE VIEW" if kind == "VIEW" else "CREATE TABLE AS"
        self.facts.add_write(key, self.line(target))
        if kind == "VIEW":
            self.facts.created_views.append(key)
        ctes = self._with(tree, None, {})
        self.query(query, None, ctes)
        self.use(key, "*", "set", target)

    def _other_ddl(self, tree):
        """DROP TABLE / VIEW, TRUNCATE and ALTER TABLE name the table they change."""
        kind = (tree.args.get("kind") or "").upper()
        if isinstance(tree, exp.Drop) and kind in ("TABLE", "VIEW"):
            for table in tree.args.get("tables") or [tree.this]:
                self._ddl(table)
        elif isinstance(tree, exp.Alter) and kind == "TABLE":
            self._ddl(tree.this)
        elif isinstance(tree, exp.TruncateTable):
            for table in tree.expressions:
                self._ddl(table)

    def _ddl(self, node):
        """Record the table a DDL statement names; returns its key, or None."""
        if isinstance(node, exp.Schema):
            node = node.this
        if isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier):
            key = self.key_of(node)
            self.facts.add_ddl(key, self.line(node))
            return key
        return None

    def _target(self, node):
        """Source for a DML target table (written, not read)."""
        alias = fold_identifier(node.args["alias"].this, self.dialect) \
            if isinstance(node.args.get("alias"), exp.TableAlias) else ""
        key = self.key_of(node)
        return Source(alias or fold_identifier(node.this, self.dialect), "table", key=key, written=True,
                      line=self.line(node))

    def _update(self, tree):
        self.facts.type = "UPDATE"
        ctes = self._with(tree, None, {})
        scope = Scope("dml")
        target_node = tree.this
        items = []
        from_ = tree.args.get("from_")
        if from_ is not None:
            items.append((from_.this, None))
            items += [(j.this, j) for j in from_.this.args.get("joins") or []]
        items += [(j.this, j) for j in tree.args.get("joins") or []]

        # tsql: UPDATE x SET ... FROM t x  -- the target is an alias of the FROM clause.
        target_name = fold_identifier(target_node.this, self.dialect)
        from_aliases = {}
        for node, _ in items:
            if isinstance(node, exp.Table) and isinstance(node.args.get("alias"), exp.TableAlias):
                from_aliases[fold_identifier(node.args["alias"].this, self.dialect)] = node
        if not target_node.args.get("db") and target_name in from_aliases:
            self.add_sources(scope, items, ctes, record_reads=False)
            target = scope.by_alias(target_name)
            target.written = True
        else:
            target = self._target(target_node)
            scope.add(target)
            scope.join_items.append((target, None))
            self.add_sources(scope, items, ctes, record_reads=False)
        self.facts.add_write(target.key, target.line)
        self.record_reads(scope)

        self.read_joins(scope, tree.args.get("where"), ctes)
        for assignment in tree.expressions:
            left = assignment.this if isinstance(assignment, exp.EQ) else None
            if isinstance(left, exp.Column):
                self.use(target.key, fold_identifier(left.this, self.dialect), "set", left)
                self.done.add(id(left))
            value = assignment.expression if isinstance(assignment, exp.EQ) else assignment
            self.walk(value, scope, "select", ctes)
        if tree.args.get("where") is not None:
            self.walk(tree.args["where"], scope, "filter", ctes)

    def _delete(self, tree):
        self.facts.type = "DELETE"
        ctes = self._with(tree, None, {})
        scope = Scope("dml")
        main = tree.this
        target_nodes = tree.args.get("tables") or [main]
        items = [(main, None)] + [(j.this, j) for j in main.args.get("joins") or []]
        items += [(u, None) for u in tree.args.get("using") or []]
        self.add_sources(scope, items, ctes, record_reads=False)
        for node in target_nodes:
            name = fold_identifier(node.this, self.dialect) if isinstance(node, exp.Table) else ""
            alias = fold_identifier(node.args["alias"].this, self.dialect) \
                if isinstance(node.args.get("alias"), exp.TableAlias) else ""
            target = scope.by_alias(alias or name)
            if target is None or target.kind != "table":
                continue
            target.written = True
            self.facts.add_write(target.key, target.line)
        self.record_reads(scope)
        self.read_joins(scope, tree.args.get("where"), ctes)
        if tree.args.get("where") is not None:
            self.walk(tree.args["where"], scope, "filter", ctes)

    def _merge(self, tree):
        self.facts.type = "MERGE"
        ctes = self._with(tree, None, {})
        scope = Scope("dml")
        target = self._target(tree.this)
        scope.add(target)
        scope.join_items.append((target, None))
        self.facts.add_write(target.key, target.line)
        using = tree.args.get("using")
        source = self.make_source(using, scope, ctes)
        scope.add(source)
        scope.join_items.append((source, None))
        if source.kind == "table":
            self.facts.add_read(source.key, source.line)

        on = tree.args.get("on")
        if on is not None:
            links = []
            for part in J.split_and(on):
                self._condition(part, scope, "merge", "on", links, part)
            for link in links:
                link["type"] = "merge"
            self._make_joins(scope, links)
            self.walk(on, scope, "filter", ctes)

        whens = tree.args.get("whens")
        for when in (whens.expressions if whens is not None else []):
            condition = when.args.get("condition")
            if condition is not None:
                self.walk(condition, scope, "filter", ctes)
            then = when.args.get("then")
            if isinstance(then, exp.Update):
                for assignment in then.expressions:
                    if isinstance(assignment, exp.EQ) and isinstance(assignment.this, exp.Column):
                        self.use(target.key, fold_identifier(assignment.this.this, self.dialect), "set",
                                 assignment.this)
                        self.walk(assignment.expression, scope, "select", ctes)
            elif isinstance(then, exp.Insert):
                columns = then.this
                if isinstance(columns, (exp.Tuple, exp.Schema)):
                    for column in columns.expressions:
                        name = column.this if isinstance(column, exp.Column) else column
                        self.use(target.key, fold_identifier(name, self.dialect), "set", column)
                if then.expression is not None:
                    self.walk(then.expression, scope, "select", ctes)

    # -- helpers --------------------------------------------------------------

    def key_of(self, node):
        """Key of a table named in the statement: as written, or the schema's key for it
        (Schema.resolve). A name several schemas have stays as written, as an unknown."""
        key = table_key(node, self.dialect)
        if self.schema is None:
            return key
        written_with_schema = bool(node.args.get("db") or node.args.get("catalog"))
        key, reason = self.schema.resolve(key, written_with_schema)
        if reason:
            self.unknown("table", key, reason, node)
        return key

    def unknown(self, kind, name, reason, node):
        key = (kind, name, reason)
        if key in self.seen_unknowns:
            return
        self.seen_unknowns.add(key)
        self.facts.unknowns.append(Unknown(kind, name, reason, self.line(node)))

    def line(self, node):
        """File line of a node, from the token positions sqlglot keeps."""
        if self.raw.line_base is None or not isinstance(node, exp.Expression):
            return self.raw.first_line
        if isinstance(node, exp.Star) and node.parent is not None:
            node = node.parent
        for current in node.walk():
            if isinstance(current, exp.Identifier) and "line" in current.meta:
                return self.raw.line_base + current.meta["line"] - 1
        return self.raw.first_line


def _order(source):
    """Sources are ordered outer scope first, then by their place in the FROM clause."""
    return (0 if source.written else 1, source.position)


def _link_kind(scope):
    """The link kind of the nearest nested query around a scope (scalar when none says)."""
    while scope is not None:
        if scope.link_kind:
            return scope.link_kind
        scope = scope.parent
    return "scalar"


def subquery_kind(node):
    """How a query nested in an expression is used: in, not_in, exists, not_exists or scalar."""
    def above(current):
        current = current.parent
        while isinstance(current, exp.Paren):
            current = current.parent
        return current

    parent = above(node)
    if isinstance(parent, exp.In) and node is not parent.this:
        kind = "in"
    elif isinstance(parent, exp.Exists):
        kind = "exists"
    else:
        return "scalar"
    return "not_" + kind if isinstance(above(parent), exp.Not) else kind


def join_type_of(join):
    """inner, left, right, full, cross, or None for a comma join."""
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    if side in ("LEFT", "RIGHT", "FULL"):
        return side.lower()
    if kind == "CROSS":
        return "cross"
    if kind in ("INNER", "OUTER") or join.args.get("on") is not None or join.args.get("using"):
        return "inner"
    return None


def other_type(tree):
    """Type name for a statement with no lineage: CREATE INDEX, DROP TABLE, COMMIT ..."""
    if isinstance(tree, (exp.Drop, exp.Alter)):
        kind = (tree.args.get("kind") or "").upper()
        word = "DROP" if isinstance(tree, exp.Drop) else "ALTER"
        return f"{word} {kind}".strip()
    names = {
        "TruncateTable": "TRUNCATE", "Transaction": "BEGIN", "Commit": "COMMIT",
        "Rollback": "ROLLBACK", "Set": "SET", "Use": "USE", "Pragma": "PRAGMA",
        "Grant": "GRANT", "Revoke": "REVOKE", "Analyze": "ANALYZE", "Comment": "COMMENT",
        "Describe": "DESCRIBE", "Show": "SHOW",
    }
    return names.get(type(tree).__name__, type(tree).__name__.upper())
