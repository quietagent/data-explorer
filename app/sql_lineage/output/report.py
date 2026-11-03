"""report.html: one offline file with the summary, join catalog, conflicts,
suggested views, joins without a key, subquery links, the unreadable list, an
impact picker and a graph of tables and joins.

The page is report_template.html with two markers filled in:
    /*DATA*/       the report data as JSON (names and SQL text only, never data rows)
    /*CYTOSCAPE*/  vendor/cytoscape.min.js, inlined so nothing is loaded from the network
Impact is worked out here for every table and column (analysis/impact.py), so
the page and the impact command always agree.
"""

import json
from pathlib import Path

from sql_lineage.analysis import impact

HERE = Path(__file__).parent
TEMPLATE = HERE / "report_template.html"
CYTOSCAPE = HERE / "vendor" / "cytoscape.min.js"


def report_data(graph, result):
    """Everything the page shows, as a JSON-ready dict."""
    conflict_keys = {v["key"] for c in result["conflicts"] for v in c["variants"]}
    data = {
        "meta": graph.meta,
        "summary": result["summary"],
        "catalog": result["catalog"],
        "conflicts": result["conflicts"],
        "groups": result["groups"],
        "joins_without_key": result["joins_without_key"],
        "subquery_links": result["subquery_links"],
        "unreadable": result["unreadable"],
        "unknowns": result["unknowns"],
        "not_in_schema": _not_in_schema(graph),
        "impacts": _impacts(graph),
        "graph": {
            "tables": _graph_tables(graph),
            "joins": [{"key": row["key"], "table1": row["table1"], "table2": row["table2"],
                       "statements": row["statements"], "declared_fk": row["declared_fk"],
                       "non_key": row["non_key"], "conflict": row["key"] in conflict_keys}
                      for row in result["catalog"] if not row["derived"]],
            "feeds": sorted({(graph.table_by_id[e.src].key, graph.table_by_id[e.dst].key)
                             for e in graph.of_kind("FEEDS")}),
        },
    }
    return data


def write_report(graph, result, path):
    html = build_report(graph, result)
    Path(path).write_text(html, encoding="utf-8", newline="\n")


def build_report(graph, result):
    """The report as one HTML string."""
    data = json.dumps(report_data(graph, result), ensure_ascii=False, separators=(",", ":"))
    # Inside <script>, "</" could end the element early; "<\/" means the same in JSON.
    data = data.replace("</", "<\\/").replace("<!--", "<\\!--")
    template = TEMPLATE.read_text(encoding="utf-8")
    cytoscape = CYTOSCAPE.read_text(encoding="utf-8")
    return template.replace("/*DATA*/", data, 1).replace("/*CYTOSCAPE*/", cytoscape, 1)


def _impacts(graph):
    """Impact of every table and every column (TABLE.* left out), keyed by name."""
    found = {}
    keys = [t.key for t in graph.tables] + [c.key for c in graph.columns if c.name != "*"]
    for key in keys:
        result = impact(graph, key)
        found[key] = {
            "kind": result["kind"],
            "table": key if result["kind"] == "table" else key.rsplit(".", 1)[0],
            "direct": [{"script": r["script"], "number": r["number"], "line": r["line"] or r["first_line"],
                        "role": r["role"], "item": r["item"]} for r in result["direct"]],
            "levels": [{
                "level": level["level"],
                "tables": [{"table": t["table"],
                            "written_by": [f"{w['script']} #{w['number']}" for w in t["written_by"]]}
                           for t in level["tables"]],
                "statements": [{"ref": f"{s['script']} #{s['number']}, line {s['line'] or s['first_line']}",
                                "reads": s["reads"]} for s in level["statements"]],
            } for level in result["levels"]],
            "stopped": result["stopped"],
            "unreadable": [f"{r['script']} #{r['number']}, line {r['first_line']}: {r['reason']}"
                           for r in result["unreadable"]],
            "unreadable_elsewhere": result["unreadable_elsewhere"],
        }
    return found


def _graph_tables(graph):
    """Each table with the scripts (and statement numbers) that read, write, define or use it."""
    uses = {t.id: {} for t in graph.tables}

    def add(table_id, statement_id):
        statement = graph.statement_by_id[statement_id]
        path = graph.script_by_id[statement.script_id].path
        uses[table_id].setdefault(path, set()).add(statement.number)

    for edge in graph.of_kind("READS") + graph.of_kind("WRITES") + graph.of_kind("DDL"):
        add(edge.dst, edge.src)
    for edge in graph.of_kind("USES"):
        add(graph.column_by_id[edge.dst].table_id, edge.src)
    return [{"key": t.key, "kind": t.kind, "in_schema": t.in_schema,
             "scripts": [{"script": path, "numbers": sorted(numbers)}
                         for path, numbers in sorted(uses[t.id].items())]}
            for t in graph.tables]


def _not_in_schema(graph):
    if not graph.meta.get("schema"):
        return None
    return {
        "tables": [{"key": t.key, "kind": t.kind} for t in sorted(graph.tables, key=lambda t: t.key)
                   if t.in_schema is False],
        "columns": sorted(c.key for c in graph.columns if c.in_schema is False),
    }
