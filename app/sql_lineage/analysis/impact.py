"""Impact: what is touched if table X or column X.Y changes.

Direct uses
    For a table: every statement that reads it, writes it, names it in DDL
    (CREATE TABLE, CREATE INDEX, DROP, TRUNCATE, ALTER), or uses one of its
    columns (with the column's role). For a column: every statement that uses
    it (select, join, filter, group, order, set, ddl; joins and subquery links
    are "join" uses), plus those using TABLE.*, shown with their role and (*):
    "select (*)", or "ddl (*)" for the CREATE TABLE that defines it.
Downstream, by script (PLAN.md: data often passes through Python between a
read and a write of the same job, with no SQL linking them)
    Level n takes the tables written by the scripts first reached at level
    n-1 (for level 1, the scripts of the direct uses), leaves out the tables
    already seen (the target's table first), and lists the statements not
    listed before that read one of them, each with its line and the level's
    tables it reads. Their scripts, if not reached before, are the ones level
    n+1 starts from. At most settings.IMPACT_MAX_DEPTH levels.
DDL never spreads impact
    A direct statement that is only DDL (it names tables in CREATE TABLE,
    CREATE INDEX, DROP, TRUNCATE or ALTER, and reads and writes none) is
    listed in the direct uses, but its script does not start level 1. A script
    that also has a direct statement that is not DDL does.
Unreadable statements
    They have no edges, so they are never in the lists. The ones in the
    scripts reached (and in the scripts of DDL-only direct uses) are named in
    "unreadable"; "unreadable_elsewhere" counts the rest of the scan.
"""

from sql_lineage import settings
from sql_lineage.analysis.uses import statement_ref
from sql_lineage.parse.names import fold


def impact(graph, name):
    """Return the impact of a table or column name as a dict (see the module docstring)."""
    target = find_target(graph, name)
    result = {"name": name, "target": None, "kind": None, "direct": [], "levels": [], "stopped": False,
              "unreadable": [], "unreadable_elsewhere": 0}
    if target is None:
        result["suggestions"] = _suggestions(graph, name)
        return result
    kind, key = target
    result["target"], result["kind"] = key, kind
    result["direct"] = _direct_uses(graph, kind, key)

    target_table = key if kind == "table" else graph.table_by_id[graph.column_by_key[key].table_id].key
    seen_tables = {target_table}
    listed = {row["statement_id"] for row in result["direct"]}
    reached = _scripts_of(graph, {s for s in listed if not _only_ddl(graph, s)})
    ddl_only_scripts = _scripts_of(graph, listed) - reached
    new_scripts = set(reached)
    level = 0
    while new_scripts:
        written = {}     # table key -> statements of the new scripts that write it
        for script_id in sorted(new_scripts):
            for contains in graph.out("CONTAINS", script_id):
                for edge in graph.out("WRITES", contains.dst):
                    table = graph.table_by_id[edge.dst].key
                    if table not in seen_tables:
                        written.setdefault(table, set()).add(contains.dst)
        if not written:
            break
        if level >= settings.IMPACT_MAX_DEPTH:
            result["stopped"] = True
            break
        level += 1
        seen_tables.update(written)
        tables = []
        reads_here = {}     # statement id -> {table of this level it reads: line}
        for table in sorted(written):
            table_id = graph.table_by_key[table].id
            readers = [statement_ref(graph, e.src, line=e.line) for e in graph.into("READS", table_id)
                       if e.src not in listed]
            readers.sort(key=lambda r: (r["script"], r["number"]))
            tables.append({
                "table": table,
                "written_by": sorted((statement_ref(graph, s) for s in written[table]),
                                     key=lambda r: (r["script"], r["number"])),
                "read_by": readers,
            })
            for reader in readers:
                reads_here.setdefault(reader["statement_id"], {})[table] = reader["line"]
        statements = []
        for statement_id, found in reads_here.items():
            lines = [line for line in found.values() if line]
            statements.append(statement_ref(graph, statement_id, line=min(lines) if lines else None,
                                            reads=sorted(found)))
        statements.sort(key=lambda r: (r["script"], r["number"]))
        result["levels"].append({"level": level, "tables": tables, "statements": statements})
        readers_here = set(reads_here)
        listed |= readers_here
        new_scripts = _scripts_of(graph, readers_here) - reached
        reached |= new_scripts

    failed = [s for s in graph.statements if s.status == "failed"]
    named = reached | ddl_only_scripts
    result["unreadable"] = sorted((statement_ref(graph, s.id, reason=s.reason) for s in failed
                                   if s.script_id in named), key=lambda r: (r["script"], r["number"]))
    result["unreadable_elsewhere"] = len(failed) - len(result["unreadable"])
    return result


def _scripts_of(graph, statement_ids):
    return {graph.statement_by_id[s].script_id for s in statement_ids}


def _only_ddl(graph, statement_id):
    """True for a statement with DDL edges and no READS or WRITES (CREATE TABLE, DROP ...)."""
    return bool(graph.out("DDL", statement_id)) and not graph.out("READS", statement_id) \
        and not graph.out("WRITES", statement_id)


def find_target(graph, name):
    """("table", key) or ("column", key) for a name as typed, or None."""
    dialect = graph.meta.get("dialect", "sqlite")
    candidates = [name.strip(), ".".join(fold(p, False, dialect) for p in name.strip().split("."))]
    for candidate in candidates:
        if candidate in graph.table_by_key:
            return "table", candidate
        if candidate in graph.column_by_key:
            return "column", candidate
    return None


def _direct_uses(graph, kind, key):
    rows = []
    if kind == "table":
        table = graph.table_by_key[key]
        for edge in graph.into("READS", table.id):
            rows.append(statement_ref(graph, edge.src, line=edge.line, role="read", item=key))
        for edge in graph.into("WRITES", table.id):
            rows.append(statement_ref(graph, edge.src, line=edge.line, role="write", item=key))
        for edge in graph.into("DDL", table.id):
            rows.append(statement_ref(graph, edge.src, line=edge.line, role="ddl", item=key))
        columns = [c for c in graph.columns if c.table_id == table.id]
    else:
        column = graph.column_by_key[key]
        star = graph.column_by_key.get(graph.table_by_id[column.table_id].key + ".*")
        columns = [column] + ([star] if star else [])
    for column in columns:
        for edge in graph.into("USES", column.id):
            role = edge.role
            if kind == "column" and column.name == "*":
                role = f"{edge.role} (*)"
            rows.append(statement_ref(graph, edge.src, line=edge.line, role=role, item=column.key))
    rows.sort(key=lambda r: (r["script"], r["number"], r["line"] or 0, r["role"], r["item"]))
    return rows


def _suggestions(graph, name):
    """Up to ten table or column keys that contain the name, for a 'not found' message."""
    text = name.strip().upper()
    keys = [t.key for t in graph.tables] + [c.key for c in graph.columns]
    return sorted(k for k in keys if text in k.upper())[:10]


def format_impact(result):
    """The impact as text for the command line: one heading per level, all at the same depth
    (levels go by script, so they are not a tree), as the report's impact picker shows it."""
    if result["target"] is None:
        lines = [f"'{result['name']}' is not a table or column in this lineage.db."]
        if result.get("suggestions"):
            lines.append("Did you mean: " + ", ".join(result["suggestions"]))
        return "\n".join(lines)

    def count(number, word):
        return f"{number} {word}{'' if number == 1 else 's'}"

    lines = [f"Impact of {result['kind']} {result['target']}", ""]
    lines.append(f"Direct ({count(len(result['direct']), 'use')})")
    if not result["direct"]:
        lines.append("  none")
    for row in result["direct"]:
        lines.append(f"  {row['script']} #{row['number']}, line {row['line'] or row['first_line']}: "
                     f"{row['role']} {row['item']}")
    if not result["levels"]:
        lines.append("")
        lines.append("Downstream: none, the scripts above write no other table.")
    for level in result["levels"]:
        lines.append("")
        lines.append(f"Level {level['level']} ({count(len(level['tables']), 'table')}, "
                     f"{count(len(level['statements']), 'statement')})")
        lines.append("  Tables:")
        for table in level["tables"]:
            writers = ", ".join(f"{w['script']} #{w['number']}" for w in table["written_by"])
            lines.append(f"    {table['table']}, written by {writers}")
        lines.append("  Statements that read them:")
        for statement in level["statements"]:
            lines.append(f"    {statement['script']} #{statement['number']}, "
                         f"line {statement['line'] or statement['first_line']}: reads {', '.join(statement['reads'])}")
    if result["stopped"]:
        lines.append("")
        lines.append(f"Stopped at IMPACT_MAX_DEPTH = {settings.IMPACT_MAX_DEPTH} levels.")
    if result["unreadable"] or result["unreadable_elsewhere"]:
        lines.append("")
        lines.append("Not read, so not in the lists above:")
        for row in result["unreadable"]:
            lines.append(f"  {row['script']} #{row['number']}, line {row['first_line']}: {row['reason']}")
        if result["unreadable_elsewhere"]:
            lines.append(f"  and {result['unreadable_elsewhere']} more statement(s) in other scripts "
                         "(see the unreadable list)")
    return "\n".join(lines)
