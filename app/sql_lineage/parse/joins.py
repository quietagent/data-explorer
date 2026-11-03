"""The canonical join key, and helpers to read join conditions.

The key (PLAN.md, "The same join"): the two tables in name order, then the
column pairs sorted, each written table1 column first:

    CUSTOMER JOIN SALES_ORDER ON CUSTOMER.CUSTOMER_ID = SALES_ORDER.CUSTOMER_ID

so a.x = b.y and b.y = a.x give one key, and the join type (inner, left ...)
is not part of it. A condition that is not plain column equality (BETWEEN,
<, a function) is added to the key as text, after the pairs. For a self-join
both tables are the same; the pairs are then turned the way that sorts first,
so e.boss = m.id and m.id = e.boss also give one key.
"""

from sqlglot import exp

from sql_lineage.parse.names import split_table_key


def canonical(table_a, table_b, pairs, conditions):
    """Return (key, table1, table2, sorted pairs) for a join between table_a and table_b.

    pairs are (column of table_a, column of table_b).
    """
    flipped = [(b, a) for a, b in pairs]
    if table_a > table_b:
        table_a, table_b, pairs = table_b, table_a, flipped
    elif table_a == table_b:
        pairs = min(sorted(pairs), sorted(flipped))
    pairs = sorted(pairs)
    items = [f"{table_a}.{a} = {table_b}.{b}" for a, b in pairs]
    items += sorted(conditions)
    key = f"{table_a} JOIN {table_b}"
    if items:
        key += " ON " + " AND ".join(items)
    return key, table_a, table_b, pairs


def split_and(node):
    """The parts of a condition joined by AND, with outer brackets removed."""
    node = unwrap(node)
    if isinstance(node, exp.And):
        return split_and(node.this) + split_and(node.expression)
    return [node]


def unwrap(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def own_columns(node):
    """Column nodes in `node`, not counting those inside nested queries."""
    found = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (exp.Select, exp.SetOperation, exp.Subquery)) and current is not node:
            continue
        if isinstance(current, exp.Column):
            found.append(current)
            continue
        stack.extend(current.iter_expressions())
    return found


def plain_equality(node):
    """(left column, right column) when node is col = col, else None."""
    node = unwrap(node)
    if not isinstance(node, exp.EQ):
        return None
    left, right = unwrap(node.this), unwrap(node.expression)
    if isinstance(left, exp.Column) and isinstance(right, exp.Column) \
            and not isinstance(left.this, exp.Star) and not isinstance(right.this, exp.Star):
        return left, right
    return None


def render(node, mapping, dialect):
    """SQL text of a condition with each column written as TABLE.COLUMN.

    mapping: {(qualifier, name) as written and folded: (table label, column)}.
    (+) marks are dropped, so an Oracle and an ANSI join read the same.
    """
    def replace(current):
        if isinstance(current, exp.Column) and not isinstance(current.this, exp.Star):
            written = (current.args.get("table").name.upper() if current.args.get("table") else "",
                       current.name.upper())
            target = mapping.get(written)
            if target is not None:
                label, column = target
                schema, table = split_table_key(label)
                return exp.Column(
                    this=exp.to_identifier(column),
                    table=exp.to_identifier(table),
                    db=exp.to_identifier(schema) if schema else None,
                )
        return current
    return unwrap(node).transform(replace).sql(dialect=dialect)
