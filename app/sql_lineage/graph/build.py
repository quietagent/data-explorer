"""Builds the Graph from the extract and parse results.

Table kinds: what the schema says (table or view) when the table is in it;
otherwise "view" when a script creates it with CREATE VIEW, "written by a
script" when a statement writes it or a CREATE TABLE creates it, and "unknown"
for the rest.
FEEDS edges join each table a statement reads to each table it writes (a table
feeding itself is left out). DDL edges never feed anything.
"""

from sql_lineage.graph.model import (ColumnNode, Edge, Graph, JoinNode, ScriptNode, StatementNode,
                                     TableNode, UnknownItem)
from sql_lineage.parse.names import column_key, split_table_key


def build_graph(scripts, raw_statements, facts, schema=None, meta=None):
    """scripts: ScriptFile list; raw_statements and facts: lists in the same order."""
    graph = Graph(meta=dict(meta or {}))
    builder = _Builder(graph, schema)

    script_ids = {}
    for script in scripts:
        node = ScriptNode(len(graph.scripts) + 1, script.path, script.kind, script.size,
                          script.sha256, script.encoding, script.problem)
        graph.scripts.append(node)
        script_ids[script.path] = node.id

    written, views = set(), set()
    for item in facts:
        written.update(item.writes)
        if item.type == "CREATE TABLE":
            written.update(item.ddl)
        views.update(item.created_views)
    builder.written, builder.views = written, views

    for raw, item in zip(raw_statements, facts):
        statement = StatementNode(len(graph.statements) + 1, script_ids[raw.script], raw.number,
                                  item.type, raw.first_line, raw.last_line, raw.text,
                                  item.status, item.reason)
        graph.statements.append(statement)
        builder.edge("CONTAINS", statement.script_id, statement.id)
        builder.add_facts(statement.id, item)

    return graph.index()


class _Builder:
    def __init__(self, graph, schema):
        self.graph = graph
        self.schema = schema
        self.tables = {}
        self.columns = {}
        self.joins = {}
        self.seen_edges = set()
        self.written = set()
        self.views = set()

    def add_facts(self, statement_id, facts):
        for key in facts.reads:
            self.edge("READS", statement_id, self.table(key).id, line=facts.lines.get(("read", key)))
        for key in facts.writes:
            self.edge("WRITES", statement_id, self.table(key).id, line=facts.lines.get(("write", key)))
        for key in facts.ddl:
            self.edge("DDL", statement_id, self.table(key).id, line=facts.lines.get(("ddl", key)))
        for use in facts.columns:
            column = self.column(use.table, use.column)
            self.edge("USES", statement_id, column.id, role=use.role, line=use.line)
        for use in facts.joins:
            join = self.join(use)
            self.edge("JOINS", statement_id, join.id, join_type=use.join_type,
                      left_table=use.left_table, line=use.line, role=use.link_kind or None,
                      via_cte=use.via_cte)
            for table, name in use.on_columns:
                self.edge("ON", join.id, self.column(table, name).id)
        for source in facts.reads:
            for target in facts.writes:
                if source != target:
                    self.edge("FEEDS", self.table(source).id, self.table(target).id, via=statement_id)
        for unknown in facts.unknowns:
            self.graph.unknowns.append(UnknownItem(len(self.graph.unknowns) + 1, statement_id,
                                                   unknown.kind, unknown.name, unknown.reason, unknown.line))

    def table(self, key):
        node = self.tables.get(key)
        if node is None:
            schema_name, name = split_table_key(key)
            if self.schema is not None and self.schema.has_table(key):
                kind, in_schema = self.schema.tables[key].kind, True
            else:
                in_schema = False if self.schema is not None else None
                if key in self.views:
                    kind = "view"
                elif key in self.written:
                    kind = "written by a script"
                else:
                    kind = "unknown"
            node = TableNode(len(self.graph.tables) + 1, key, schema_name, name, kind, in_schema)
            self.graph.tables.append(node)
            self.tables[key] = node
        return node

    def column(self, table_key, name):
        key = column_key(table_key, name)
        node = self.columns.get(key)
        if node is None:
            table = self.table(table_key)
            in_schema = None
            if self.schema is not None and name != "*":
                columns = self.schema.columns_of(table_key)
                in_schema = columns is not None and name in columns
            node = ColumnNode(len(self.graph.columns) + 1, key, table.id, name, in_schema)
            self.graph.columns.append(node)
            self.columns[key] = node
        return node

    def join(self, use):
        node = self.joins.get(use.key)
        if node is None:
            declared = None
            if self.schema is not None and not use.derived:
                declared = bool(use.pairs) and self.schema.is_declared(use.table1, use.table2, use.pairs)
            node = JoinNode(len(self.graph.joins) + 1, use.key, use.table1, use.table2,
                            [list(p) for p in use.pairs], " AND ".join(use.conditions) or None,
                            use.non_key, use.derived, declared)
            self.graph.joins.append(node)
            self.joins[use.key] = node
            if not use.derived:
                self.table(use.table1)
                self.table(use.table2)
        return node

    def edge(self, kind, src, dst, **props):
        key = (kind, src, dst, tuple(sorted(props.items())))
        if key in self.seen_edges:
            return
        self.seen_edges.add(key)
        self.graph.edges.append(Edge(kind, src, dst, **props))
