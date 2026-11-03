"""Repeated join groups ("reduce"): sets of joins that appear together in many
statements, each with a draft shared view.

For each statement, take its joins (derived ones and subquery links left out) and find every
connected group of 2 to settings.MAX_GROUP_JOINS joins: joins are connected
when they share a table. A group is reported when it appears in at least
MIN_GROUP_STATEMENTS statements from at least MIN_GROUP_SCRIPTS scripts.
A group is dropped when a bigger reported group that contains it has exactly
the same statements. Groups are ranked by statements x joins.

The draft CREATE VIEW holds the join conditions and the columns those
statements use from the group's tables (any role but "set"; * left out).
It is text to read and copy; nothing is run.
"""

from sql_lineage import settings
from sql_lineage.analysis.uses import statement_ref
from sql_lineage.parse.names import split_table_key


def repeated_groups(graph):
    """List of group dicts, best first."""
    joins_by_statement = {}
    for edge in graph.of_kind("JOINS"):
        if edge.join_type != "subquery" and not graph.join_by_id[edge.dst].derived:
            joins_by_statement.setdefault(edge.src, set()).add(edge.dst)

    statements_by_group = {}
    for statement_id, join_ids in joins_by_statement.items():
        for group in connected_groups(graph, join_ids, settings.MAX_GROUP_JOINS):
            statements_by_group.setdefault(group, set()).add(statement_id)

    reported = {}
    for group, statement_ids in statements_by_group.items():
        scripts = {graph.statement_by_id[s].script_id for s in statement_ids}
        if len(statement_ids) >= settings.MIN_GROUP_STATEMENTS and len(scripts) >= settings.MIN_GROUP_SCRIPTS:
            reported[group] = statement_ids

    kept = [group for group in reported
            if not any(group < other and reported[other] == reported[group] for other in reported)]

    results = [_describe(graph, group, reported[group]) for group in kept]
    results.sort(key=lambda g: (-g["score"], g["name"], g["joins"]))
    return results


def connected_groups(graph, join_ids, max_size):
    """Every set of 2..max_size joins from join_ids that is connected through shared tables."""
    tables = {j: {graph.join_by_id[j].table1, graph.join_by_id[j].table2} for j in join_ids}
    found = set()
    frontier = {frozenset([j]) for j in join_ids}
    for _ in range(2, max_size + 1):
        bigger = set()
        for group in frontier:
            group_tables = set().union(*(tables[j] for j in group))
            for j in join_ids:
                if j not in group and tables[j] & group_tables:
                    bigger.add(group | {j})
        found |= bigger
        frontier = bigger
    return found


def _describe(graph, group, statement_ids):
    joins = sorted((graph.join_by_id[j] for j in group), key=lambda j: j.key)
    uses = sorted((statement_ref(graph, s) for s in statement_ids),
                  key=lambda r: (r["script"], r["number"]))
    types = sorted({e.join_type for s in statement_ids for e in graph.out("JOINS", s)
                    if e.dst in group and e.join_type != "subquery"})
    tables = sorted({t for j in joins for t in (j.table1, j.table2)})
    columns = _used_columns(graph, statement_ids, set(tables))
    order, sql = _draft_sql(joins, columns, types)
    name = "v_" + "_".join(split_table_key(t)[1].lower() for t in order)
    sql = sql.replace("CREATE VIEW ? AS", f"CREATE VIEW {name} AS", 1)
    return {
        "name": name,
        "joins": [j.key for j in joins],
        "tables": order,
        "types": types,
        "statements": len(statement_ids),
        "scripts": sorted({u["script"] for u in uses}),
        "score": len(statement_ids) * len(joins),
        "columns": columns,
        "uses": uses,
        "sql": sql,
    }


def _used_columns(graph, statement_ids, tables):
    """TABLE.COLUMN keys the statements use from these tables (no *, no "set")."""
    found = set()
    for statement_id in statement_ids:
        for edge in graph.out("USES", statement_id):
            column = graph.column_by_id[edge.dst]
            table = graph.table_by_id[column.table_id].key
            if table in tables and column.name != "*" and edge.role != "set":
                found.add((table, column.name))
    return [f"{t}.{c}" for t, c in sorted(found)]


def _draft_sql(joins, columns, types):
    """The draft view text and the order the tables were placed in.

    Tables are placed one join at a time, starting with the table most joins
    touch. A join between two tables already placed adds its condition to the
    last JOIN with AND. A table joined to itself gets a second alias, NAME_2.
    """
    count = {}
    for join in joins:
        for table in {join.table1, join.table2}:
            count[table] = count.get(table, 0) + 1
    first = min(count, key=lambda t: (-count[t], t))
    placed = [first]
    lines = [f"FROM {first}"]
    remaining = list(joins)
    self_joined = []
    while remaining:
        for join in remaining:
            a, b = join.table1, join.table2
            if a == b and a in placed:
                second = f"{split_table_key(a)[1]}_2"
                self_joined.append(a)
                lines.append(f"JOIN {a} {second} ON " + _condition(join, a, second))
            elif a in placed and b not in placed:
                placed.append(b)
                lines.append(f"JOIN {b} ON " + _condition(join, a, b))
            elif b in placed and a not in placed:
                placed.append(a)
                lines.append(f"JOIN {a} ON " + _condition(join, a, b))
            elif a in placed and b in placed:
                lines[-1] += " AND " + _condition(join, a, b)
            else:
                continue
            remaining.remove(join)
            break
        else:
            break   # not connected; cannot happen for a connected group

    names = [c.rsplit(".", 1)[1] for c in columns]
    select = []
    for column in columns:
        table, name = column.rsplit(".", 1)
        if names.count(name) > 1:
            select.append(f"{column} AS {split_table_key(table)[1]}_{name}")
        else:
            select.append(column)
    header = [f"-- Draft, to check before use. Join types seen: {', '.join(types)}."]
    for table in self_joined:
        header.append(f"-- {table} is joined to itself: check which side each column belongs to.")
    body = ["CREATE VIEW ? AS", "SELECT"]
    body += [f"    {item}," for item in select[:-1]] + ([f"    {select[-1]}"] if select else ["    *"])
    return placed, "\n".join(header + body + lines) + ";"


def _condition(join, left, right):
    """Join condition text; left and right name the two sides (an alias for a self-join)."""
    parts = [f"{left}.{a} = {right}.{b}" for a, b in join.pairs]
    if join.condition:
        parts.append(join.condition)
    return " AND ".join(parts) if parts else "1 = 1"
