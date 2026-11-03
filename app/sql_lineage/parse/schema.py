"""The optional schema: tables, their columns, and declared foreign keys.

load_schema(path, dialect) reads either
- a SQLite database file, opened read-only (mode=ro), through PRAGMA
  table_info and foreign_key_list; or
- a DDL .sql file, parsed with sqlglot: CREATE TABLE (columns, column and
  table level PRIMARY KEY / REFERENCES / FOREIGN KEY), CREATE VIEW (output
  names) and ALTER TABLE ... ADD [CONSTRAINT] FOREIGN KEY.
schema_from_tables(tables, dialect) builds the same Schema from plain Python
data (lists and dicts), for a caller that already knows the tables.
Names are folded with the dialect's rules (names.py). A foreign key without a
column list points at the referenced table's primary key.

Schema.resolve(key) is the one rule for a table name written in a script
(and in a foreign key): a name written with its schema is taken as written; a
name written without one is the schema's table of that name without a schema,
else the one table of that name in any schema (under its full key), else, when
several schemas have it, the one in the default schema. Otherwise it stays as
written and is not certain. Never a guess.
"""

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import exp

from sql_lineage import settings
from sql_lineage.extract.files import decode
from sql_lineage.extract.sql_split import split_sql
from sql_lineage.parse.names import fold, fold_identifier, join_parts, split_table_key, table_key

logging.getLogger("sqlglot").setLevel(logging.ERROR)

SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass
class SchemaTable:
    key: str
    kind: str                   # "table" or "view"
    columns: list | None        # folded names in order; None when not known (a view with *)
    primary_key: list = field(default_factory=list)


@dataclass
class ForeignKey:
    table: str
    columns: tuple
    ref_table: str
    ref_columns: tuple


@dataclass
class Schema:
    source: str
    tables: dict = field(default_factory=dict)          # key -> SchemaTable
    foreign_keys: list = field(default_factory=list)    # ForeignKey
    warnings: list = field(default_factory=list)        # statements of the DDL file not read
    default_schema: str = ""    # folded; decides a name that several schemas have (scan_folder sets it)
    _by_name: tuple = field(default=(-1, {}), init=False, repr=False, compare=False)  # (table count, index)

    def has_table(self, key):
        return key in self.tables

    def resolve(self, key, with_schema=False):
        """(table key, reason) for a table key as written in a script.

        with_schema: the name was written with a schema, even the dialect's
        default one (dbo.orders has the key ORDERS), so it is taken as written.
        The key is the schema's own when the rules in the module docstring find
        one; otherwise it is the key as written. reason is None, or why the name
        is not certain ("in 2 schemas: HR, SALES").
        """
        if with_schema or "." in key or key in self.tables:
            return key, None
        if self._by_name[0] != len(self.tables):   # every table with a schema, by its name alone
            index = {}
            for table in sorted(self.tables):
                schema_name, name = split_table_key(table)
                if schema_name:
                    index.setdefault(name, []).append(table)
            self._by_name = (len(self.tables), index)
        found = self._by_name[1].get(key, [])
        if len(found) == 1:
            return found[0], None
        if not found:
            return key, None
        for table in found:
            if self.default_schema and split_table_key(table)[0] == self.default_schema:
                return table, None
        schemas = [split_table_key(table)[0] for table in found]
        return key, f"in {len(found)} schemas: {', '.join(schemas)}"

    def columns_of(self, key):
        """Set of column names of a table, or None when the table or its columns are not known."""
        table = self.tables.get(key)
        if table is None or table.columns is None:
            return None
        return set(table.columns)

    def is_declared(self, table1, table2, pairs):
        """True when a declared foreign key covers this join.

        pairs are (column of table1, column of table2). A foreign key counts in
        either direction, and all of a composite key's columns must be in the join.
        """
        wanted = set(pairs)
        for fk in self.foreign_keys:
            candidates = []
            if fk.table == table1 and fk.ref_table == table2:
                candidates.append(set(zip(fk.columns, fk.ref_columns)))
            if fk.table == table2 and fk.ref_table == table1:
                candidates.append(set(zip(fk.ref_columns, fk.columns)))
            for fk_pairs in candidates:
                if fk_pairs and fk_pairs <= wanted:
                    return True
        return False


def load_schema(path, dialect):
    """Read a schema file (SQLite database or DDL .sql)."""
    path = Path(path)
    with open(path, "rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header == SQLITE_HEADER:
        schema = _load_sqlite(path, dialect)
    else:
        schema = _load_ddl(path, dialect)
    _fill_missing_reference_columns(schema)
    return schema


def schema_from_tables(tables, dialect, source="tables given as data"):
    """Build a Schema from plain data: a list with one dict per table or view.

        {"name": "ORDER_ITEM",                    required
         "schema": "SALES",                       optional: "" or None for no schema
         "kind": "table",                         optional: "table" (the default) or "view"
         "columns": ["ORDER_ID", "LINE_NO"],      optional: None or left out when not known
         "primary_key": ["ORDER_ID", "LINE_NO"],  optional
         "foreign_keys": [                        optional, one dict per declared foreign key
             {"columns": ["ORDER_ID"],
              "ref_table": "SALES_ORDER",
              "ref_schema": "SALES",              optional
              "ref_columns": ["ORDER_ID"]}]}      optional: the referenced table's primary key

    Names are written as the database stores them (as a catalog lists them), so
    they are folded like quoted names: they keep their case in postgres and are
    upper-cased in the other dialects. The dialect's default schema (dbo, main,
    public) is dropped, as it is in scripts. `source` is what the scan records
    as its schema. A wrong shape raises ValueError, naming the table.
    """
    if dialect not in settings.DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; use one of {', '.join(settings.DIALECTS)}")
    if isinstance(tables, (str, dict)):
        raise ValueError("tables must be a list of dicts, one per table")
    schema = Schema(source=source)
    for number, item in enumerate(tables, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError(f"table {number}: each table is a dict with a 'name'")
        name = item["name"]
        key = _data_key(item.get("schema"), name, dialect)
        kind = item.get("kind") or "table"
        if kind not in ("table", "view"):
            raise ValueError(f"table {name}: kind must be 'table' or 'view', not {kind!r}")
        columns = item.get("columns")
        if columns is not None:
            columns = [fold(c, True, dialect) for c in _names(columns, name, "columns")]
        primary = [fold(c, True, dialect) for c in _names(item.get("primary_key") or [], name, "primary_key")]
        schema.tables[key] = SchemaTable(key, kind, columns, primary)
        for fk in item.get("foreign_keys") or []:
            if not isinstance(fk, dict) or not isinstance(fk.get("ref_table"), str) or not fk.get("columns"):
                raise ValueError(f"table {name}: each foreign key is a dict with 'columns' and 'ref_table'")
            fk_columns = tuple(fold(c, True, dialect) for c in _names(fk["columns"], name, "foreign key columns"))
            ref_columns = tuple(fold(c, True, dialect) for c in _names(fk.get("ref_columns") or [], name,
                                                                         "foreign key ref_columns"))
            if ref_columns and len(ref_columns) != len(fk_columns):
                raise ValueError(f"table {name}: a foreign key has {len(fk_columns)} columns "
                                 f"and {len(ref_columns)} ref_columns")
            ref_key = _data_key(fk.get("ref_schema"), fk["ref_table"], dialect)
            schema.foreign_keys.append(ForeignKey(key, fk_columns, ref_key, ref_columns))
    _fill_missing_reference_columns(schema)
    return schema


def _data_key(schema_name, name, dialect):
    """Table key of a name given as data (folded like a quoted name)."""
    if schema_name is not None and not isinstance(schema_name, str):
        raise ValueError(f"table {name}: schema must be text")
    return join_parts("", fold(schema_name, True, dialect) if schema_name else "", fold(name, True, dialect), dialect)


def _names(values, table, what):
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) and v for v in values):
        raise ValueError(f"table {table}: {what} must be a list of names")
    return list(values)


def _load_sqlite(path, dialect):
    schema = Schema(source=str(path))
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for kind, name in rows:
            key = join_parts("", "", fold(name, False, dialect), dialect)
            info = connection.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
            columns = [fold(row[1], False, dialect) for row in info]
            primary = [fold(row[1], False, dialect) for row in sorted(info, key=lambda r: r[5]) if row[5]]
            schema.tables[key] = SchemaTable(key, kind, columns, primary)
            if kind != "table":
                continue
            by_id = {}
            for row in connection.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall():
                fk_id, _, ref_table, from_col, to_col = row[0], row[1], row[2], row[3], row[4]
                by_id.setdefault(fk_id, (ref_table, [], []))
                by_id[fk_id][1].append(fold(from_col, False, dialect))
                by_id[fk_id][2].append(fold(to_col, False, dialect) if to_col else None)
            for ref_table, from_cols, to_cols in by_id.values():
                ref_key = join_parts("", "", fold(ref_table, False, dialect), dialect)
                ref_cols = () if None in to_cols else tuple(to_cols)
                schema.foreign_keys.append(ForeignKey(key, tuple(from_cols), ref_key, ref_cols))
    finally:
        connection.close()
    return schema


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _load_ddl(path, dialect):
    schema = Schema(source=str(path))
    encoding, text = decode(Path(path).read_bytes())
    if text is None:
        schema.warnings.append("schema file could not be decoded")
        return schema
    for piece in split_sql(text, dialect):
        try:
            tree = sqlglot.parse_one(piece.text, read=dialect)
        except Exception as error:
            schema.warnings.append(f"line {piece.first_line}: not read ({str(error).splitlines()[0]})")
            continue
        if isinstance(tree, exp.Create):
            _read_create(tree, schema, dialect)
        elif isinstance(tree, exp.Alter):
            _read_alter(tree, schema, dialect)
    return schema


def _read_create(tree, schema, dialect):
    kind = (tree.args.get("kind") or "").upper()
    target = tree.this
    if kind == "TABLE" and isinstance(target, exp.Schema):
        key = table_key(target.this, dialect)
        columns, primary = [], []
        for item in target.expressions:
            if isinstance(item, exp.ColumnDef):
                name = fold_identifier(item.this, dialect)
                columns.append(name)
                for constraint in item.args.get("constraints") or []:
                    found = constraint.args.get("kind")
                    if isinstance(found, exp.PrimaryKeyColumnConstraint):
                        primary.append(name)
                    elif isinstance(found, exp.Reference):
                        _add_reference(schema, key, (name,), found, dialect)
            elif isinstance(item, exp.PrimaryKey):
                primary = [fold_identifier(e, dialect) for e in item.expressions]
            elif isinstance(item, exp.ForeignKey):
                _add_foreign_key(schema, key, item, dialect)
            elif isinstance(item, exp.Constraint):
                for inner in item.expressions:
                    if isinstance(inner, exp.ForeignKey):
                        _add_foreign_key(schema, key, inner, dialect)
                    elif isinstance(inner, exp.PrimaryKey):
                        primary = [fold_identifier(e, dialect) for e in inner.expressions]
        schema.tables[key] = SchemaTable(key, "table", columns, primary)
    elif kind == "TABLE" and isinstance(target, exp.Table):
        key = table_key(target, dialect)
        schema.tables[key] = SchemaTable(key, "table", _output_names(tree.expression, dialect))
    elif kind == "VIEW":
        table = target.this if isinstance(target, exp.Schema) else target
        key = table_key(table, dialect)
        if isinstance(target, exp.Schema):
            columns = [fold_identifier(e, dialect) for e in target.expressions]
        else:
            columns = _output_names(tree.expression, dialect)
        schema.tables[key] = SchemaTable(key, "view", columns)


def _read_alter(tree, schema, dialect):
    if not isinstance(tree.this, exp.Table):
        return
    key = table_key(tree.this, dialect)
    for action in tree.args.get("actions") or []:
        for node in action.find_all(exp.ForeignKey):
            _add_foreign_key(schema, key, node, dialect)
        for node in action.find_all(exp.PrimaryKey):
            if key in schema.tables:
                schema.tables[key].primary_key = [fold_identifier(e, dialect) for e in node.expressions]


def _add_foreign_key(schema, key, node, dialect):
    columns = tuple(fold_identifier(e, dialect) for e in node.expressions)
    reference = node.args.get("reference")
    if reference is not None:
        _add_reference(schema, key, columns, reference, dialect)


def _add_reference(schema, key, columns, reference, dialect):
    target = reference.this
    if isinstance(target, exp.Schema):
        ref_key = table_key(target.this, dialect)
        ref_columns = tuple(fold_identifier(e, dialect) for e in target.expressions)
    else:
        ref_key = table_key(target, dialect)
        ref_columns = ()
    schema.foreign_keys.append(ForeignKey(key, columns, ref_key, ref_columns))


def _output_names(query, dialect):
    """Output column names of a view's query, or None when one of them is * or unnamed."""
    if not isinstance(query, exp.Select):
        return None
    names = []
    for item in query.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
            return None
        if not item.alias_or_name:
            return None
        names.append(fold(item.alias_or_name, False, dialect))
    return names


def _fill_missing_reference_columns(schema):
    """A foreign key's table names follow Schema.resolve (REFERENCES orders is SALES.ORDERS when
    that is the one ORDERS), and a REFERENCES t without columns points at t's primary key."""
    for fk in schema.foreign_keys:
        fk.table = schema.resolve(fk.table)[0]
        fk.ref_table = schema.resolve(fk.ref_table)[0]
        if not fk.ref_columns:
            target = schema.tables.get(fk.ref_table)
            if target and len(target.primary_key) == len(fk.columns):
                fk.ref_columns = tuple(target.primary_key)
