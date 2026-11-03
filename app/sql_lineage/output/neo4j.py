"""Neo4j export: neo4j/nodes.csv, neo4j/edges.csv, neo4j/load.cypher, neo4j/README.txt.

nodes.csv has one row per node, with a `label` column (Script, Statement,
Table, Column, Join) and a `key` that is unique within its label. Columns that
do not apply to a label are empty. edges.csv has one row per edge, with a
`label` column (the relationship type: CONTAINS, READS ...) and the keys of
its two ends; which labels those keys belong to follows from the type
(graph/model.py EDGE_ENDS).

load.cypher removes any earlier lineage nodes, creates a uniqueness constraint
on `key` for each label, then loads both files with LOAD CSV.
"""

import csv

from sql_lineage.graph.model import EDGE_ENDS

LABELS = {"script": "Script", "statement": "Statement", "table": "Table", "column": "Column", "join": "Join"}

# Properties of each label, with how load.cypher converts the CSV text.
PROPERTIES = {
    "Script": [("path", "text"), ("kind", "text"), ("size", "int"), ("sha256", "text"),
               ("encoding", "text"), ("problem", "text")],
    "Statement": [("script", "text"), ("number", "int"), ("type", "text"), ("first_line", "int"),
                  ("last_line", "int"), ("status", "text"), ("reason", "text"), ("text", "text")],
    "Table": [("schema_name", "text"), ("name", "text"), ("kind", "text"), ("in_schema", "bool")],
    "Column": [("table", "text"), ("name", "text"), ("in_schema", "bool")],
    "Join": [("table1", "text"), ("table2", "text"), ("pairs", "text"), ("condition", "text"),
             ("non_key", "bool"), ("derived", "bool"), ("declared_fk", "bool")],
}
EDGE_PROPERTIES = [("role", "text"), ("join_type", "text"), ("left_table", "text"), ("line", "int"),
                   ("via", "text"), ("via_cte", "bool")]

NODE_COLUMNS = ["label", "key"]
for _props in PROPERTIES.values():
    for _name, _ in _props:
        if _name not in NODE_COLUMNS:
            NODE_COLUMNS.append(_name)
EDGE_COLUMNS = ["label", "src_key", "dst_key"] + [name for name, _ in EDGE_PROPERTIES]


def node_rows(graph):
    """Rows of nodes.csv as dicts."""
    scripts = {s.id: s for s in graph.scripts}
    tables = {t.id: t for t in graph.tables}
    rows = []
    for s in graph.scripts:
        rows.append({"label": "Script", "key": s.path, "path": s.path, "kind": s.kind, "size": s.size,
                     "sha256": s.sha256, "encoding": s.encoding, "problem": s.problem})
    for s in graph.statements:
        path = scripts[s.script_id].path
        rows.append({"label": "Statement", "key": f"{path}#{s.number}", "script": path, "number": s.number,
                     "type": s.type, "first_line": s.first_line, "last_line": s.last_line,
                     "status": s.status, "reason": s.reason, "text": s.text})
    for t in graph.tables:
        rows.append({"label": "Table", "key": t.key, "schema_name": t.schema_name, "name": t.name,
                     "kind": t.kind, "in_schema": t.in_schema})
    for c in graph.columns:
        rows.append({"label": "Column", "key": c.key, "table": tables[c.table_id].key, "name": c.name,
                     "in_schema": c.in_schema})
    for j in graph.joins:
        rows.append({"label": "Join", "key": j.key, "table1": j.table1, "table2": j.table2,
                     "pairs": "; ".join(f"{a} = {b}" for a, b in j.pairs), "condition": j.condition,
                     "non_key": j.non_key, "derived": j.derived, "declared_fk": j.declared_fk})
    return rows


def edge_rows(graph):
    """Rows of edges.csv as dicts."""
    keys = {
        "script": {s.id: s.path for s in graph.scripts},
        "statement": {s.id: f"{graph.script_by_id[s.script_id].path}#{s.number}" for s in graph.statements},
        "table": {t.id: t.key for t in graph.tables},
        "column": {c.id: c.key for c in graph.columns},
        "join": {j.id: j.key for j in graph.joins},
    }
    rows = []
    for e in graph.edges:
        src_kind, dst_kind = EDGE_ENDS[e.kind]
        rows.append({"label": e.kind, "src_key": keys[src_kind][e.src], "dst_key": keys[dst_kind][e.dst],
                     "role": e.role, "join_type": e.join_type, "left_table": e.left_table, "line": e.line,
                     "via": keys["statement"][e.via] if e.via else None, "via_cte": e.via_cte})
    return rows


def write_neo4j(graph, folder):
    """Write the four files into `folder`. Returns their names."""
    folder.mkdir(parents=True, exist_ok=True)
    _write_csv(folder / "nodes.csv", NODE_COLUMNS, node_rows(graph))
    _write_csv(folder / "edges.csv", EDGE_COLUMNS, edge_rows(graph))
    (folder / "load.cypher").write_text(load_cypher(), encoding="utf-8", newline="\n")
    (folder / "README.txt").write_text(README, encoding="utf-8", newline="\n")
    return ["nodes.csv", "edges.csv", "load.cypher", "README.txt"]


def _write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _cell(row.get(name)) for name in columns})


def _cell(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def _convert(name, kind):
    value = f"row.{name}"
    if kind == "int":
        return f"toInteger({value})"
    if kind == "bool":
        return f"toBoolean({value})"
    return f"CASE {value} WHEN '' THEN null ELSE {value} END"


def load_cypher():
    """The text of load.cypher."""
    lines = [
        "// SQL Lineage graph for Neo4j 5. See README.txt for how to run it.",
        "// nodes.csv and edges.csv must be in the database's import folder.",
        "",
        "// Remove the nodes of an earlier load, so the file can be run again.",
        "MATCH (n) WHERE n:Script OR n:Statement OR n:Table OR n:Column OR n:Join DETACH DELETE n;",
        "",
    ]
    for label in PROPERTIES:
        lines.append(f"CREATE CONSTRAINT lineage_{label.lower()}_key IF NOT EXISTS "
                     f"FOR (n:{label}) REQUIRE n.key IS UNIQUE;")
    lines.append("")
    for label, props in PROPERTIES.items():
        values = ", ".join(["key: row.key"] + [f"{name}: {_convert(name, kind)}" for name, kind in props])
        lines += [
            "LOAD CSV WITH HEADERS FROM 'file:///nodes.csv' AS row",
            f"WITH row WHERE row.label = '{label}'",
            f"CREATE (:{label} {{{values}}});",
            "",
        ]
    for kind, (src_kind, dst_kind) in EDGE_ENDS.items():
        values = ", ".join(f"{name}: {_convert(name, typ)}" for name, typ in EDGE_PROPERTIES)
        lines += [
            "LOAD CSV WITH HEADERS FROM 'file:///edges.csv' AS row",
            f"WITH row WHERE row.label = '{kind}'",
            f"MATCH (a:{LABELS[src_kind]} {{key: row.src_key}})",
            f"MATCH (b:{LABELS[dst_kind]} {{key: row.dst_key}})",
            f"CREATE (a)-[:{kind} {{{values}}}]->(b);",
            "",
        ]
    return "\n".join(lines)


README = """SQL Lineage: Neo4j export
=========================

nodes.csv    one row per node: label (Script, Statement, Table, Column, Join), key, properties
edges.csv    one row per relationship: label (CONTAINS, READS, WRITES, DDL, USES, JOINS, ON, FEEDS),
             src_key, dst_key, properties. DDL is CREATE TABLE, CREATE INDEX, DROP, TRUNCATE or
             ALTER of a table; it is not a write. A JOINS with join_type 'subquery' is a link
             through IN, EXISTS or a correlated subquery; its role says which (in, not_in ...).
load.cypher  creates a uniqueness constraint on key for each label, then loads both files.
             It first deletes the nodes of an earlier load, so it can be run again.

Neo4j Desktop
  1. Create or start a local DBMS (Neo4j 5).
  2. In the DBMS menu choose Open folder > Import, and copy nodes.csv and edges.csv there.
  3. Open Neo4j Browser. In its settings turn on "Enable multi statement query editor",
     paste the whole of load.cypher and run it.

cypher-shell
  1. Copy nodes.csv and edges.csv into the database's import folder
     (server.directories.import in neo4j.conf; <neo4j home>/import by default).
  2. Run:  cypher-shell -u neo4j -p <password> -f load.cypher

Docker (from the SQL_Lineage folder)
  scripts\\neo4j.ps1 start      starts a Neo4j Community container named sql-lineage-neo4j
  then copy the files in and run load.cypher:
    docker cp nodes.csv sql-lineage-neo4j:/var/lib/neo4j/import/
    docker cp edges.csv sql-lineage-neo4j:/var/lib/neo4j/import/
    docker cp load.cypher sql-lineage-neo4j:/var/lib/neo4j/import/
    docker exec sql-lineage-neo4j cypher-shell -u neo4j -p <password> -f /var/lib/neo4j/import/load.cypher
  Browse it at http://localhost:7474 . scripts\\neo4j.ps1 stop / remove when done.

A first query:
  MATCH (s:Statement)-[:READS]->(t:Table {key: 'CUSTOMER'}) RETURN s.key, s.first_line
"""
