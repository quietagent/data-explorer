"""Graph step: builds the graph of scripts, statements, tables, columns and
joins (build.py, model.py) and saves it to lineage.db (store.py).

scan_folder() runs extract, parse and build in one go; the command line, the
tests and Data Explorer use it.
"""

import dataclasses
import datetime
from pathlib import Path

import sqlglot

from sql_lineage import __version__, settings
from sql_lineage.extract import extract_file, find_scripts
from sql_lineage.graph.build import build_graph
from sql_lineage.graph.model import EDGE_ENDS, Graph
from sql_lineage.graph.store import load_graph, save_graph
from sql_lineage.parse import load_schema, parse_statements
from sql_lineage.parse.names import fold

__all__ = ["scan_folder", "build_graph", "load_graph", "save_graph", "Graph", "EDGE_ENDS"]


def scan_folder(folder, dialect, schema_path=None, schema=None, progress=None, default_schema=None):
    """Read every script in `folder` and return the Graph (not saved).

    The schema is optional: give schema_path (a DDL .sql file or a SQLite
    database) or schema, a ready Schema (for example from schema_from_tables()),
    not both.

    default_schema, when given, decides a table name written without its schema
    that several schemas have (Schema.resolve). It is folded like a name written
    in a script and kept in the graph's meta; without a schema it changes nothing.

    progress, when given, is called as progress(done, total, path) after each
    file is read: done files out of total, path relative to the folder. If it
    raises, the scan stops there and the error goes up to the caller, so
    nothing is returned. A folder with more than settings.MAX_FILES scripts is
    refused (ValueError) before any file is read.
    """
    if dialect not in settings.DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; use one of {', '.join(settings.DIALECTS)}")
    if schema_path and schema is not None:
        raise ValueError("give schema_path or schema, not both")
    folder = Path(folder).resolve()
    if schema_path:
        schema = load_schema(schema_path, dialect)
    default_schema = (default_schema or "").strip()
    default_schema = fold(default_schema, False, dialect) if default_schema else None
    if schema is not None:
        schema = dataclasses.replace(schema, default_schema=default_schema or "")   # the caller's stays as it is
    paths = find_scripts(folder)
    scripts, raws, facts = [], [], []
    for done, path in enumerate(paths, start=1):
        script, found = extract_file(folder, path, dialect)
        scripts.append(script)
        raws.extend(found)
        facts.extend(parse_statements(found, dialect, schema))
        if progress is not None:
            progress(done, len(paths), script.path)
    meta = {
        "folder": str(folder),
        "dialect": dialect,
        "schema": str(Path(schema_path).resolve()) if schema_path else (schema.source if schema else None),
        "default_schema": default_schema,
        "schema_warnings": schema.warnings if schema else [],
        "scanned_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "tool_version": __version__,
        "sqlglot_version": sqlglot.__version__,
        "settings": {
            "MAX_GROUP_JOINS": settings.MAX_GROUP_JOINS,
            "MIN_GROUP_STATEMENTS": settings.MIN_GROUP_STATEMENTS,
            "MIN_GROUP_SCRIPTS": settings.MIN_GROUP_SCRIPTS,
            "IMPACT_MAX_DEPTH": settings.IMPACT_MAX_DEPTH,
        },
    }
    return build_graph(scripts, raws, facts, schema, meta)
