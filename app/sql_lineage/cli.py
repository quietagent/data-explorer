"""Command line.

    python -m sql_lineage scan <folder> --dialect <d> [--schema <file>] [--default-schema <name>] [--out <folder>]
        Reads every .sql and .py script under <folder> and writes lineage.db,
        report.html, lineage.json and neo4j/ into --out (default:
        out/<folder name>/ in this project). Prints a short summary.
        --default-schema decides a table name written without its schema that
        several schemas of --schema have.

    python -m sql_lineage impact <TABLE or TABLE.COLUMN> --db <lineage.db>
        Prints what a change to that table or column touches.

The scan never writes into the scanned folder: an --out inside it is refused.
"""

import argparse
import sys
from pathlib import Path

from sql_lineage import output, settings
from sql_lineage.analysis import analyze, format_impact, impact
from sql_lineage.graph import load_graph, save_graph, scan_folder

PROJECT_FOLDER = Path(__file__).resolve().parent.parent


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m sql_lineage",
                                     description="Table, column and join lineage of SQL and Python scripts.")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="read a folder of scripts and write the outputs")
    scan.add_argument("folder", help="folder of .sql and .py scripts (read only)")
    scan.add_argument("--dialect", required=True, choices=settings.DIALECTS)
    scan.add_argument("--schema", help="DDL .sql file or SQLite database with the tables and keys")
    scan.add_argument("--default-schema", help="schema that decides a table name written without its schema "
                                               "when several schemas have it")
    scan.add_argument("--out", help="output folder (default: out/<folder name>/ in this project)")

    show = commands.add_parser("impact", help="show what a change to a table or column touches")
    show.add_argument("name", help="TABLE or TABLE.COLUMN")
    show.add_argument("--db", required=True, help="lineage.db written by scan")

    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return run_scan(args.folder, args.dialect, args.schema, args.out, args.default_schema)
        return run_impact(args.name, args.db)
    except (FileNotFoundError, NotADirectoryError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def run_scan(folder, dialect, schema=None, out=None, default_schema=None):
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder} is not a folder")
    if schema and not Path(schema).is_file():
        raise FileNotFoundError(f"schema file {schema} not found")
    out = Path(out).resolve() if out else PROJECT_FOLDER / "out" / folder.name
    if out == folder or out.is_relative_to(folder):
        raise ValueError(f"--out {out} is inside the scanned folder; the scan never writes there")

    graph = scan_folder(folder, dialect, schema, default_schema=default_schema)
    result = analyze(graph)
    out.mkdir(parents=True, exist_ok=True)
    save_graph(graph, out / "lineage.db")
    written = ["lineage.db"] + output.write_all(graph, result, out)

    numbers = result["summary"]
    default = graph.meta.get("default_schema")
    print(f"Scanned {folder}  (dialect {dialect}{', schema ' + str(schema) if schema else ''}"
          f"{', default schema ' + default if default else ''})")
    for label, key in (("files", "files"), ("statements", "statements"), ("unreadable", "unreadable"),
                       ("tables", "tables"), ("joins", "joins"), ("subquery links", "subquery_links"),
                       ("conflicts", "conflicts"), ("groups", "groups")):
        print(f"  {label:<15} {numbers[key]}")
    for warning in graph.meta.get("schema_warnings") or []:
        print(f"  schema: {warning}")
    print(f"Wrote {', '.join(written)} to {out}")
    return 0


def run_impact(name, db):
    graph = load_graph(db)
    result = impact(graph, name)
    print(format_impact(result))
    return 0 if result["target"] else 1
