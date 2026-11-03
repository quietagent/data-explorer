"""What the parse step learns about one statement. The graph is built from these."""

from dataclasses import dataclass, field


@dataclass
class ColumnUse:
    table: str          # table key
    column: str         # folded column name, or "*"
    role: str           # select, join, filter, group, order, set or ddl
    line: int


@dataclass
class JoinUse:
    key: str            # canonical join key (see joins.py)
    table1: str         # first table in name order (or a derived label)
    table2: str
    pairs: list         # [(column of table1, column of table2)], sorted
    conditions: list    # condition texts that are not plain column equality
    join_type: str      # inner, left, right, full, cross, where, merge or subquery
    left_table: str     # the table written on the left of the join, for left/right
    derived: bool       # a side could not be followed back to a base table
    non_key: bool       # joined by something other than column equality (or nothing)
    line: int
    on_columns: list = field(default_factory=list)  # [(table key, column)] of the condition, base sides only
    link_kind: str = ""  # for a subquery link: in, not_in, exists, not_exists or scalar
    via_cte: bool = False   # a side was followed back to its base table through a CTE or a subquery in FROM


@dataclass
class Unknown:
    kind: str           # column, join, source, table
    name: str
    reason: str
    line: int


@dataclass
class StatementFacts:
    script: str
    number: int
    type: str                       # SELECT, INSERT, ... or "UNKNOWN" when not read
    status: str                     # "read" or "failed"
    reason: str | None
    reads: list = field(default_factory=list)       # table keys, in first-seen order
    writes: list = field(default_factory=list)
    ddl: list = field(default_factory=list)         # tables named by CREATE TABLE, CREATE INDEX, DROP, TRUNCATE, ALTER
    created_views: list = field(default_factory=list)   # table keys written by CREATE VIEW
    columns: list = field(default_factory=list)     # ColumnUse
    joins: list = field(default_factory=list)       # JoinUse
    unknowns: list = field(default_factory=list)    # Unknown
    lines: dict = field(default_factory=dict)       # ("read", "write" or "ddl", table key) -> first line

    def add_read(self, key, line=None):
        if key and key not in self.reads:
            self.reads.append(key)
            self.lines[("read", key)] = line

    def add_write(self, key, line=None):
        if key and key not in self.writes:
            self.writes.append(key)
            self.lines[("write", key)] = line

    def add_ddl(self, key, line=None):
        if key and key not in self.ddl:
            self.ddl.append(key)
            self.lines[("ddl", key)] = line
