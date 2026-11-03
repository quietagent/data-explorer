"""Name folding: how a table or column name written in a script becomes a key.

Rules (settings.NAME_FOLDING, QUOTED_NAMES_KEEP_CASE, DEFAULT_SCHEMA):
- oracle and sqlite fold to upper case, postgres to lower case;
- tsql and mysql are case-blind, and fold to upper case so keys look the same;
- in postgres a quoted name keeps the case it was written in;
- a table qualified with the dialect's default schema (dbo, main, public) is
  the bare table: dbo.customer and customer are one key.

A table key is SCHEMA.TABLE (or just TABLE); a column key is TABLE_KEY.COLUMN.
"""

from sqlglot import exp

from sql_lineage import settings


def fold(name, quoted, dialect):
    """Fold one name part as the dialect does."""
    if quoted and dialect in settings.QUOTED_NAMES_KEEP_CASE:
        return name
    if settings.NAME_FOLDING[dialect] == "lower":
        return name.lower()
    return name.upper()


def fold_identifier(identifier, dialect):
    """Fold an sqlglot Identifier (or a plain string, taken as unquoted)."""
    if identifier is None:
        return ""
    if isinstance(identifier, str):
        return fold(identifier, False, dialect)
    return fold(identifier.name, bool(identifier.args.get("quoted")), dialect)


def table_key(table, dialect):
    """Key of an sqlglot Table node: [CATALOG.][SCHEMA.]TABLE, folded."""
    name = fold_identifier(table.this, dialect) if isinstance(table.this, exp.Identifier) else ""
    schema = fold_identifier(table.args.get("db"), dialect)
    catalog = fold_identifier(table.args.get("catalog"), dialect)
    return join_parts(catalog, schema, name, dialect)


def join_parts(catalog, schema, name, dialect):
    """Build a table key from folded parts, dropping the dialect's default schema."""
    default = settings.DEFAULT_SCHEMA.get(dialect)
    if default and not catalog and schema == fold(default, False, dialect):
        schema = ""
    return ".".join(part for part in (catalog, schema, name) if part)


def column_key(table, column):
    return f"{table}.{column}"


def split_table_key(key):
    """(schema part or "", table name) of a table key."""
    if "." in key:
        schema, name = key.rsplit(".", 1)
        return schema, name
    return "", key
