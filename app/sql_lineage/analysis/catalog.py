"""Join catalog: every distinct join, with its types, where it is used, and
whether it is a declared foreign key or not on a key at all. Subquery links
are not joins and are left out (analysis/subqueries.py lists them)."""

from sql_lineage.analysis.uses import join_uses


def join_catalog(graph):
    """List of dicts, most used first."""
    catalog = []
    for join in graph.joins:
        uses = join_uses(graph, join.id)
        if not uses:
            continue
        catalog.append({
            "key": join.key,
            "table1": join.table1,
            "table2": join.table2,
            "pairs": join.pairs,
            "condition": join.condition,
            "types": sorted({u["join_type"] for u in uses}),
            "statements": len({u["statement_id"] for u in uses}),
            "scripts": sorted({u["script"] for u in uses}),
            "declared_fk": join.declared_fk,
            "non_key": join.non_key,
            "derived": join.derived,
            "uses": uses,
        })
    catalog.sort(key=lambda row: (-row["statements"], row["key"]))
    return catalog
