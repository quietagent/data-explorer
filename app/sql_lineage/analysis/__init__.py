"""Analysis step: answers the questions of PLAN.md from the graph.

    catalog.py    join catalog
    subqueries.py links through IN, EXISTS or a correlated subquery (not joins)
    conflicts.py  the same two tables joined on different columns
    groups.py     repeated join groups, with a draft shared view
    nokey.py      joins without a declared foreign key (needs a schema)
    impact.py     what a change to a table or column touches

analyze(graph) runs all but impact (which needs a name) and adds a summary.
"""

from sql_lineage.analysis.catalog import join_catalog
from sql_lineage.analysis.conflicts import find_conflicts
from sql_lineage.analysis.groups import repeated_groups
from sql_lineage.analysis.impact import format_impact, impact
from sql_lineage.analysis.nokey import joins_without_key
from sql_lineage.analysis.subqueries import subquery_links

__all__ = ["analyze", "summary", "impact", "format_impact", "join_catalog", "find_conflicts",
           "repeated_groups", "joins_without_key", "subquery_links", "unreadable"]


def analyze(graph):
    result = {
        "catalog": join_catalog(graph),
        "subquery_links": subquery_links(graph),
        "conflicts": find_conflicts(graph),
        "groups": repeated_groups(graph),
        "joins_without_key": joins_without_key(graph),
        "unreadable": unreadable(graph),
        "unknowns": unknowns(graph),
    }
    result["summary"] = summary(graph, result)
    return result


def summary(graph, result):
    return {
        "files": len(graph.scripts),
        "statements": len(graph.statements),
        "unreadable": len(result["unreadable"]),
        "tables": len(graph.tables),
        "joins": len(result["catalog"]),
        "subquery_links": len(result["subquery_links"]),
        "conflicts": len(result["conflicts"]),
        "groups": len(result["groups"]),
        "unknown_names": len(result["unknowns"]),
        "joins_without_key": None if result["joins_without_key"] is None else len(result["joins_without_key"]),
    }


def unreadable(graph):
    """Files and statements that could not be read, with file, lines and reason."""
    rows = []
    for script in graph.scripts:
        if script.problem:
            rows.append({"script": script.path, "number": None, "first_line": None, "last_line": None,
                         "type": "FILE", "reason": script.problem, "text": ""})
    for statement in graph.statements:
        if statement.status == "failed":
            rows.append({"script": graph.script_by_id[statement.script_id].path, "number": statement.number,
                         "first_line": statement.first_line, "last_line": statement.last_line,
                         "type": statement.type, "reason": statement.reason, "text": statement.text})
    rows.sort(key=lambda r: (r["script"], r["number"] or 0))
    return rows


def unknowns(graph):
    """Names that could not be resolved (a column with no certain table ...)."""
    rows = []
    for item in graph.unknowns:
        statement = graph.statement_by_id[item.statement_id]
        rows.append({"script": graph.script_by_id[statement.script_id].path, "number": statement.number,
                     "line": item.line, "kind": item.kind, "name": item.name, "reason": item.reason})
    rows.sort(key=lambda r: (r["script"], r["number"], r["line"]))
    return rows
