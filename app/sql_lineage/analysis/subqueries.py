"""Subquery links: a column of one table tied to a column of another through
IN, NOT IN, EXISTS, NOT EXISTS or a correlated scalar subquery.

They are recorded as joins of type "subquery" but are not joins: a shared
view would not replace them, so the catalog, the groups, the conflicts and the
joins without a key leave them out. They are listed here, apart, and their
columns still count for impact (they are USES edges like any other).
"""

from sql_lineage.analysis.uses import join_uses


def subquery_links(graph):
    """List of dicts, most used first."""
    rows = []
    for join in graph.joins:
        uses = join_uses(graph, join.id, subquery=True)
        if not uses:
            continue
        rows.append({
            "key": join.key,
            "table1": join.table1,
            "table2": join.table2,
            "pairs": join.pairs,
            "condition": join.condition,
            "kinds": sorted({u["kind"] for u in uses}),
            "statements": len({u["statement_id"] for u in uses}),
            "scripts": sorted({u["script"] for u in uses}),
            "declared_fk": join.declared_fk,
            "derived": join.derived,
            "uses": uses,
        })
    rows.sort(key=lambda row: (-row["statements"], row["key"]))
    return rows
