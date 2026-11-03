"""Joins without a declared key: joins the schema does not declare as a foreign
key, in either direction. Only when a schema was given; derived joins (not
followed back to base tables) can't be checked and are left out, and so are
subquery links, which are not joins."""

from sql_lineage.analysis.uses import join_uses


def joins_without_key(graph):
    """List of joins whose declared_fk is False, or None when the scan had no schema."""
    if not graph.meta.get("schema"):
        return None
    rows = []
    for join in graph.joins:
        if join.declared_fk is not False:
            continue
        uses = join_uses(graph, join.id)
        if not uses:
            continue
        rows.append({
            "key": join.key,
            "table1": join.table1,
            "table2": join.table2,
            "pairs": join.pairs,
            "condition": join.condition,
            "non_key": join.non_key,
            "types": sorted({u["join_type"] for u in uses}),
            "statements": len({u["statement_id"] for u in uses}),
            "scripts": sorted({u["script"] for u in uses}),
            "uses": uses,
        })
    rows.sort(key=lambda row: (-row["statements"], row["key"]))
    return rows
