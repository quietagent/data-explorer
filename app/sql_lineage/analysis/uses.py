"""Small shared helper: describe the statements that use a join, as plain dicts."""


def join_uses(graph, join_id, subquery=False):
    """One dict per JOINS edge of a join: where it is used and how.
    Subquery links (join_type "subquery") are not joins: only subquery=True returns them,
    with their kind (in, not_in, exists, not_exists, scalar)."""
    rows = []
    for edge in graph.into("JOINS", join_id):
        if (edge.join_type == "subquery") != subquery:
            continue
        extra = {"kind": edge.role} if subquery else {}
        rows.append(statement_ref(graph, edge.src, line=edge.line, join_type=edge.join_type,
                                  left_table=edge.left_table, via_cte=bool(edge.via_cte), **extra))
    rows.sort(key=lambda r: (r["script"], r["number"], r["line"] or 0))
    return rows


def statement_ref(graph, statement_id, **extra):
    """A statement as {statement_id, script, number, first_line, ...extra}."""
    statement = graph.statement_by_id[statement_id]
    script = graph.script_by_id[statement.script_id]
    row = {
        "statement_id": statement.id,
        "script": script.path,
        "number": statement.number,
        "first_line": statement.first_line,
    }
    row.update(extra)
    return row
