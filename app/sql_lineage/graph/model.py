"""The graph in memory: five kinds of node, eight kinds of edge (PLAN.md).

Nodes                         Edges (kind: from -> to)
    Script     path               CONTAINS  Script -> Statement
    Statement  script + number    READS     Statement -> Table
    Table      folded key         WRITES    Statement -> Table
    Column     TABLE.COLUMN       DDL       Statement -> Table   (CREATE TABLE, CREATE INDEX, DROP,
    Join       canonical key                                      TRUNCATE, ALTER; not a write)
                                  USES      Statement -> Column  (role, line)
                                  JOINS     Statement -> Join    (join_type, left_table, line,
                                                                  via_cte; for join_type subquery,
                                                                  role is the link kind: in, not_in,
                                                                  exists, not_exists or scalar)
                                  ON        Join -> Column
                                  FEEDS     Table -> Table       (via: the statement)

Every node has an integer id, unique within its kind. An Edge holds the ids of
its two ends; its kind says which node kinds those are.
"""

from dataclasses import dataclass, field

# Which node kinds each edge kind connects.
EDGE_ENDS = {
    "CONTAINS": ("script", "statement"),
    "READS": ("statement", "table"),
    "WRITES": ("statement", "table"),
    "DDL": ("statement", "table"),
    "USES": ("statement", "column"),
    "JOINS": ("statement", "join"),
    "ON": ("join", "column"),
    "FEEDS": ("table", "table"),
}


@dataclass
class ScriptNode:
    id: int
    path: str
    kind: str
    size: int
    sha256: str
    encoding: str | None
    problem: str | None


@dataclass
class StatementNode:
    id: int
    script_id: int
    number: int
    type: str
    first_line: int
    last_line: int
    text: str
    status: str             # "read" or "failed"
    reason: str | None


@dataclass
class TableNode:
    id: int
    key: str
    schema_name: str
    name: str
    kind: str               # table, view, written by a script, unknown
    in_schema: bool | None  # None when no schema was given


@dataclass
class ColumnNode:
    id: int
    key: str
    table_id: int
    name: str
    in_schema: bool | None


@dataclass
class JoinNode:
    id: int
    key: str
    table1: str
    table2: str
    pairs: list             # [[column of table1, column of table2]]
    condition: str | None   # the condition texts that are not plain equality, joined by AND
    non_key: bool
    derived: bool
    declared_fk: bool | None    # None without a schema, or for a derived join


@dataclass
class Edge:
    kind: str
    src: int
    dst: int
    role: str | None = None
    join_type: str | None = None
    left_table: str | None = None
    line: int | None = None
    via: int | None = None  # FEEDS: the statement that reads src and writes dst
    via_cte: bool | None = None     # JOINS: a side was followed back through a CTE or a subquery in FROM


@dataclass
class UnknownItem:
    id: int
    statement_id: int
    kind: str
    name: str
    reason: str
    line: int


@dataclass
class Graph:
    meta: dict = field(default_factory=dict)
    scripts: list = field(default_factory=list)
    statements: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)

    def index(self):
        """Build the lookups below. Call again after changing the lists."""
        self.script_by_id = {s.id: s for s in self.scripts}
        self.statement_by_id = {s.id: s for s in self.statements}
        self.table_by_id = {t.id: t for t in self.tables}
        self.table_by_key = {t.key: t for t in self.tables}
        self.column_by_id = {c.id: c for c in self.columns}
        self.column_by_key = {c.key: c for c in self.columns}
        self.join_by_id = {j.id: j for j in self.joins}
        self.join_by_key = {j.key: j for j in self.joins}
        self.edges_out = {}
        self.edges_in = {}
        for edge in self.edges:
            self.edges_out.setdefault((edge.kind, edge.src), []).append(edge)
            self.edges_in.setdefault((edge.kind, edge.dst), []).append(edge)
        return self

    def out(self, kind, src):
        return self.edges_out.get((kind, src), [])

    def into(self, kind, dst):
        return self.edges_in.get((kind, dst), [])

    def of_kind(self, kind):
        return [e for e in self.edges if e.kind == kind]

    def statement_label(self, statement_id):
        """'path #number' of a statement."""
        statement = self.statement_by_id[statement_id]
        return f"{self.script_by_id[statement.script_id].path} #{statement.number}"
