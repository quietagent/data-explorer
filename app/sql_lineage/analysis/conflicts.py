"""Conflicts: the same two tables joined by more than one different set of
columns in the scanned scripts. One of the variants is usually a bug.

Left out: derived joins (a side is a CTE or subquery that could not be
followed back), cross joins (no condition at all), self-joins (one table
joined to itself on different columns is normal: manager, peer ...) and
subquery links.
"""

from sql_lineage.analysis.uses import join_uses


def find_conflicts(graph):
    """List of {table1, table2, variants: [...]}, by table names."""
    by_pair = {}
    for join in graph.joins:
        if join.derived or join.table1 == join.table2:
            continue
        if not join.pairs and not join.condition:
            continue
        if not join_uses(graph, join.id):
            continue    # only used as a subquery link
        by_pair.setdefault((join.table1, join.table2), []).append(join)

    conflicts = []
    for (table1, table2), joins in sorted(by_pair.items()):
        if len(joins) < 2:
            continue
        variants = []
        for join in sorted(joins, key=lambda j: j.key):
            uses = join_uses(graph, join.id)
            variants.append({
                "key": join.key,
                "pairs": join.pairs,
                "condition": join.condition,
                "declared_fk": join.declared_fk,
                "statements": len({u["statement_id"] for u in uses}),
                "scripts": sorted({u["script"] for u in uses}),
                "uses": uses,
            })
        conflicts.append({"table1": table1, "table2": table2, "variants": variants})
    return conflicts
