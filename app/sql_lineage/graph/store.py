"""Saves the graph to lineage.db (SQLite) and loads it back.

Layout, PRAGMA user_version = 2 (version 2 added DDL edges and subquery links). One table per node kind, one edges table:
the edge's kind says which node tables its src and dst ids point into
(graph/model.py, EDGE_ENDS), so every walk is one indexed lookup.

    meta        key TEXT PRIMARY KEY, value TEXT
                folder, dialect, schema, scanned_at, tool and sqlglot versions, settings
    scripts     id, path UNIQUE, kind, size, sha256, encoding, problem
    statements  id, script_id -> scripts, number, type, first_line, last_line,
                text, status ('read' or 'failed'), reason
    tables      id, key UNIQUE, schema_name, name, kind, in_schema (1, 0 or NULL)
    columns     id, key UNIQUE (TABLE.COLUMN), table_id -> tables, name, in_schema
    joins       id, key UNIQUE, table1, table2, pairs (JSON [[col1, col2], ...]),
                condition, non_key, derived, declared_fk (1, 0 or NULL)
    edges       id, kind, src, dst, role, join_type, left_table, line, via, via_cte
                CONTAINS script->statement, READS / WRITES / DDL statement->table,
                USES statement->column (role, line), JOINS statement->join
                (join_type, left_table, line; role = the link kind when
                join_type is subquery; via_cte = 1 when a side was followed
                back through a CTE or a subquery in FROM), ON join->column,
                FEEDS table->table (via = statement id)
    unknowns    id, statement_id -> statements, kind, name, reason, line
"""

import json
import sqlite3
from pathlib import Path

from sql_lineage.graph.model import (ColumnNode, Edge, Graph, JoinNode, ScriptNode, StatementNode,
                                     TableNode, UnknownItem)

USER_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE scripts (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, kind TEXT, size INTEGER,
    sha256 TEXT, encoding TEXT, problem TEXT);
CREATE TABLE statements (
    id INTEGER PRIMARY KEY, script_id INTEGER NOT NULL REFERENCES scripts(id), number INTEGER,
    type TEXT, first_line INTEGER, last_line INTEGER, text TEXT, status TEXT, reason TEXT,
    UNIQUE (script_id, number));
CREATE TABLE tables (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, schema_name TEXT, name TEXT, kind TEXT,
    in_schema INTEGER);
CREATE TABLE columns (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, table_id INTEGER REFERENCES tables(id),
    name TEXT, in_schema INTEGER);
CREATE TABLE joins (
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, table1 TEXT, table2 TEXT, pairs TEXT,
    condition TEXT, non_key INTEGER, derived INTEGER, declared_fk INTEGER);
CREATE TABLE edges (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, src INTEGER NOT NULL, dst INTEGER NOT NULL,
    role TEXT, join_type TEXT, left_table TEXT, line INTEGER, via INTEGER, via_cte INTEGER);
CREATE INDEX edges_src ON edges (kind, src);
CREATE INDEX edges_dst ON edges (kind, dst);
CREATE TABLE unknowns (
    id INTEGER PRIMARY KEY, statement_id INTEGER REFERENCES statements(id), kind TEXT,
    name TEXT, reason TEXT, line INTEGER);
"""


def save_graph(graph, path):
    """Write the graph to a new lineage.db at `path` (an old file is replaced)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {USER_VERSION}")
        connection.executemany("INSERT INTO meta VALUES (?, ?)",
                               [(k, json.dumps(v)) for k, v in graph.meta.items()])
        connection.executemany("INSERT INTO scripts VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (s.id, s.path, s.kind, s.size, s.sha256, s.encoding, s.problem) for s in graph.scripts])
        connection.executemany("INSERT INTO statements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (s.id, s.script_id, s.number, s.type, s.first_line, s.last_line, s.text, s.status, s.reason)
            for s in graph.statements])
        connection.executemany("INSERT INTO tables VALUES (?, ?, ?, ?, ?, ?)", [
            (t.id, t.key, t.schema_name, t.name, t.kind, _flag(t.in_schema)) for t in graph.tables])
        connection.executemany("INSERT INTO columns VALUES (?, ?, ?, ?, ?)", [
            (c.id, c.key, c.table_id, c.name, _flag(c.in_schema)) for c in graph.columns])
        connection.executemany("INSERT INTO joins VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (j.id, j.key, j.table1, j.table2, json.dumps(j.pairs), j.condition, _flag(j.non_key),
             _flag(j.derived), _flag(j.declared_fk)) for j in graph.joins])
        connection.executemany(
            "INSERT INTO edges (kind, src, dst, role, join_type, left_table, line, via, via_cte) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (e.kind, e.src, e.dst, e.role, e.join_type, e.left_table, e.line, e.via, _flag(e.via_cte))
                for e in graph.edges])
        connection.executemany("INSERT INTO unknowns VALUES (?, ?, ?, ?, ?, ?)", [
            (u.id, u.statement_id, u.kind, u.name, u.reason, u.line) for u in graph.unknowns])
        connection.commit()
    finally:
        connection.close()


def load_graph(path):
    """Read a lineage.db back into a Graph."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist; run a scan first")
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != USER_VERSION:
            raise ValueError(f"{path} has layout version {version}; this tool reads version {USER_VERSION}")
        graph = Graph()
        graph.meta = {k: json.loads(v) for k, v in connection.execute("SELECT key, value FROM meta")}
        graph.scripts = [ScriptNode(*row) for row in connection.execute(
            "SELECT id, path, kind, size, sha256, encoding, problem FROM scripts ORDER BY id")]
        graph.statements = [StatementNode(*row) for row in connection.execute(
            "SELECT id, script_id, number, type, first_line, last_line, text, status, reason "
            "FROM statements ORDER BY id")]
        graph.tables = [TableNode(r[0], r[1], r[2], r[3], r[4], _bool(r[5])) for r in connection.execute(
            "SELECT id, key, schema_name, name, kind, in_schema FROM tables ORDER BY id")]
        graph.columns = [ColumnNode(r[0], r[1], r[2], r[3], _bool(r[4])) for r in connection.execute(
            "SELECT id, key, table_id, name, in_schema FROM columns ORDER BY id")]
        graph.joins = [JoinNode(r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], bool(r[6]), bool(r[7]),
                                _bool(r[8])) for r in connection.execute(
            "SELECT id, key, table1, table2, pairs, condition, non_key, derived, declared_fk "
            "FROM joins ORDER BY id")]
        graph.edges = [Edge(*row[:8], via_cte=_bool(row[8])) for row in connection.execute(
            "SELECT kind, src, dst, role, join_type, left_table, line, via, via_cte FROM edges ORDER BY id")]
        graph.unknowns = [UnknownItem(*row) for row in connection.execute(
            "SELECT id, statement_id, kind, name, reason, line FROM unknowns ORDER BY id")]
    finally:
        connection.close()
    return graph.index()


def _flag(value):
    return None if value is None else int(bool(value))


def _bool(value):
    return None if value is None else bool(value)
