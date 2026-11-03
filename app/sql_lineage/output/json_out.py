"""lineage.json: the whole graph plus the analysis results, in one JSON file.

Shape (also in README.md):
    {
      "format": "sql-lineage/2",
      "meta": {...},                      # folder, dialect, schema, scanned_at, versions, settings
      "nodes": {
        "scripts":    [{id, path, kind, size, sha256, encoding, problem}],
        "statements": [{id, script_id, number, type, first_line, last_line, text, status, reason}],
        "tables":     [{id, key, schema_name, name, kind, in_schema}],
        "columns":    [{id, key, table_id, name, in_schema}],
        "joins":      [{id, key, table1, table2, pairs, condition, non_key, derived, declared_fk}]
      },
      "edges": [{kind, src, dst, role, join_type, left_table, line, via, via_cte}],
      "unknowns": [{id, statement_id, kind, name, reason, line}],
      "analysis": {summary, catalog, subquery_links, conflicts, groups, joins_without_key,
                   unreadable, unknowns}
    }
Edge src and dst are node ids of the kinds given by graph/model.py EDGE_ENDS.
Format 2 added DDL edges, subquery links (JOINS edges of join_type "subquery",
their kind in role) and analysis.subquery_links.
"""

import dataclasses
import json

FORMAT = "sql-lineage/2"


def lineage_document(graph, result):
    """The JSON document as a Python dict."""
    def rows(items):
        return [dataclasses.asdict(item) for item in items]

    return {
        "format": FORMAT,
        "meta": graph.meta,
        "nodes": {
            "scripts": rows(graph.scripts),
            "statements": rows(graph.statements),
            "tables": rows(graph.tables),
            "columns": rows(graph.columns),
            "joins": rows(graph.joins),
        },
        "edges": rows(graph.edges),
        "unknowns": rows(graph.unknowns),
        "analysis": result,
    }


def write_json(graph, result, path):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(lineage_document(graph, result), handle, ensure_ascii=False, indent=1)
        handle.write("\n")
