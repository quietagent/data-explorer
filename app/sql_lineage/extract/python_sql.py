"""Finds the SQL inside a Python script by reading its syntax tree (ast).
The script is never imported or run.

A string is SQL when
  1. it is passed to execute(), executemany(), executescript(), read_sql(),
     read_sql_query(), exec_driver_sql(), spark.sql() or text()
     (settings.PYTHON_SQL_CALLS), directly or through a variable; or
  2. it starts with a SQL word (settings.SQL_START_WORDS) and sqlglot reads it
     as that kind of statement: a SELECT needs a table, an UPDATE needs a SET,
     an INSERT needs VALUES or a query, a DELETE needs FROM or WHERE, a MERGE
     needs ON.
Docstrings and strings passed straight to print() or a logging call are never
SQL. Comments are not in the syntax tree, so they are never SQL either.
A string holding several statements (executescript) is split like a .sql file.
A statement's first and last lines are those of its string literal (the key's
convention): a string that starts with a newline starts on the line of its
opening quote. With several statements in one string, the first starts on the
literal's first line, the last ends on its last line, and the others keep the
lines of their own text.

read_sql_table('<table>', ..., schema='<schema>') reads a whole table: it is
recorded as the statement SELECT * FROM that table, at the line of the name.
A table name that is not a string literal is unreadable.

When the SQL handed to one of those calls can't be known without running the
script (built in a loop, from a list, by .format() on a variable, passed in as
a parameter ...), the call is listed as unreadable with its file and line.
"""

import ast
import logging
import re

import sqlglot
from sqlglot import exp

from sql_lineage import settings
from sql_lineage.extract import holes
from sql_lineage.extract.model import RawStatement
from sql_lineage.extract.python_strings import HANDLED_ELSEWHERE, PythonStrings, RunTime, StrValue
from sql_lineage.extract.sql_split import split_sql
from sql_lineage.extract.sql_text import tokenize

logging.getLogger("sqlglot").setLevel(logging.ERROR)


def find_python_sql(path, source, dialect):
    """Return (statements, problem) for one Python script.

    statements is a list of RawStatement (numbered later by the caller, in line
    order); problem is set when the file is not valid Python.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [], f"not valid Python 3 (line {error.lineno}: {error.msg})"

    strings = PythonStrings(tree, source)
    skipped = _docstrings(tree) | _message_strings(tree)
    found = []          # (line, column, RawStatement)
    used = set()        # ids of literal nodes already turned into statements

    # Rule 1: arguments of the SQL calls.
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call) or not _named(call, settings.PYTHON_SQL_CALLS):
            continue
        argument = _sql_argument(call)
        if argument is None:
            continue
        value = strings.value_of(argument)
        if value is HANDLED_ELSEWHERE or value is None and isinstance(argument, ast.Constant):
            continue
        if isinstance(value, StrValue):
            if value.literals and all(id(n) in used for n in value.literals):
                continue
            used.update(id(n) for n in value.literals)
            node = value.literals[0] if value.literals else argument
            found += _statements_from(path, value, node, call, dialect, check_shape=False)
        else:
            reason = value.reason if isinstance(value, RunTime) else settings.REASON_SQL_AT_RUN_TIME
            found.append(_unreadable(path, source, call, reason))

    # read_sql_table('<table>'): a SELECT * of that table.
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and _named(call, settings.PYTHON_TABLE_CALLS):
            item = _table_read(path, source, call)
            used.update(id(n) for n in ast.walk(call))
            found.append(item)

    # Rule 2: any other string that reads as SQL.
    for node in ast.walk(tree):
        if not _is_string_top(node, strings) or id(node) in skipped:
            continue
        value = strings.value_of(node)
        if not isinstance(value, StrValue) or not value.literals:
            continue
        if any(id(n) in used or id(n) in skipped for n in value.literals):
            continue
        statements = _statements_from(path, value, node, node, dialect, check_shape=True)
        if statements:
            used.update(id(n) for n in value.literals)
            found += statements

    found.sort(key=lambda item: (item[0], item[1]))
    return [statement for _, _, statement in found], None


def _statements_from(path, value, node, anchor, dialect, check_shape):
    """Cut a StrValue into statements. With check_shape, return [] unless it reads as SQL."""
    text, labels = holes.render(value.parts)
    pieces = split_sql(text, dialect)
    if not pieces:
        return []
    if check_shape and not _reads_as_sql(pieces[0].text, dialect):
        return []
    results = []
    start = value.literals[0].lineno if value.literals else node.lineno
    end = max(value.literals[-1].end_lineno if value.literals else node.end_lineno, start)
    for index, piece in enumerate(pieces):
        sql, reason = holes.fill(piece.text, labels, dialect)
        if value.line_base is not None:
            first = value.line_base + piece.first_line - 1
            last = value.line_base + piece.last_line - 1
            line_base = value.line_base + piece.first_line - 1    # maps the SQL text's lines for the parser
        else:
            first, last, line_base = node.lineno, node.end_lineno, None
        if index == 0:
            first = start
        if index == len(pieces) - 1:
            last = end
        if reason:
            sql_text = holes.show_holes(piece.text, labels)
            line_base = None
        else:
            sql_text = sql
        if sql_text != piece.text and line_base is not None and sql_text.count("\n") != piece.text.count("\n"):
            line_base = None
        statement = RawStatement(script=path, number=0, first_line=first, last_line=last,
                                 text=sql_text, reason=reason, line_base=line_base)
        results.append((first, getattr(anchor, "col_offset", 0), statement))
    return results


def _reads_as_sql(text, dialect):
    """Rule 2 test: first word is a SQL word and sqlglot reads that kind of statement."""
    first = _first_word(text, dialect)
    if first not in settings.SQL_START_WORDS:
        return False
    plain = holes.normalize_python_binds(holes.without_markers(text), dialect)
    try:
        tree = sqlglot.parse_one(plain, read=dialect)
    except Exception:
        return False
    if first == "SELECT":
        return isinstance(tree, (exp.Select, exp.SetOperation)) and tree.find(exp.Table) is not None
    if first == "WITH":
        return tree.args.get("with_") is not None or tree.find(exp.With) is not None
    if first == "INSERT":
        return isinstance(tree, exp.Insert) and tree.expression is not None
    if first == "UPDATE":
        return isinstance(tree, exp.Update) and bool(tree.expressions)
    if first == "DELETE":
        return isinstance(tree, exp.Delete) and (
            tree.args.get("where") is not None or _second_word(text, dialect) == "FROM")
    if first == "MERGE":
        return isinstance(tree, exp.Merge) and tree.args.get("on") is not None
    if first == "CREATE":
        return isinstance(tree, exp.Create)
    return False


def _first_word(text, dialect):
    words = [t.text.upper() for t in tokenize(text, dialect) if t.kind == "word"][:1]
    return words[0] if words else ""


def _second_word(text, dialect):
    words = [t.text.upper() for t in tokenize(text, dialect) if t.kind == "word"][:2]
    return words[1] if len(words) > 1 else ""


def _unreadable(path, source, call, reason):
    segment = ast.get_source_segment(source, call) or ""
    statement = RawStatement(script=path, number=0, first_line=call.lineno, last_line=call.end_lineno,
                             text=segment, reason=reason)
    return (call.lineno, call.col_offset, statement)


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _named(call, names):
    """True when the call is one of `names`. A dotted name must match the end of the call's
    dotted name: spark.sql matches spark.sql(...) and self.spark.sql(...), not session.sql(...)."""
    func, parts = call.func, []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    dotted = ".".join(reversed(parts))
    for name in names:
        if "." in name and (dotted == name or dotted.endswith("." + name)):
            return True
        if "." not in name and _call_name(call) == name:
            return True
    return False


def _table_read(path, source, call):
    """read_sql_table(table_name, ..., schema=...): the statement SELECT * FROM [schema.]table,
    or an unreadable item when the table or schema name is not a string literal."""
    table = call.args[0] if call.args else _keyword(call, settings.PYTHON_TABLE_KEYWORD)
    schema = _keyword(call, settings.PYTHON_SCHEMA_KEYWORD)
    if not _is_literal(table) or (schema is not None and not _is_literal(schema)):
        return _unreadable(path, source, call, settings.REASON_NAME_AT_RUN_TIME)
    name = _sql_name(table.value)
    if schema is not None:
        name = _sql_name(schema.value) + "." + name
    statement = RawStatement(script=path, number=0, first_line=table.lineno, last_line=table.end_lineno,
                             text=f"SELECT * FROM {name}")
    return (table.lineno, table.col_offset, statement)


def _keyword(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_literal(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() != ""


def _sql_name(name):
    """A plain name as it is; any other name in double quotes."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _sql_argument(call):
    """The expression holding the SQL in a call to one of the SQL functions."""
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg in settings.PYTHON_SQL_KEYWORDS:
            return keyword.value
    return None


def _is_string_top(node, strings):
    """True for a string expression that is not part of a bigger string expression."""
    if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
        return False
    if isinstance(node, ast.Constant) and not isinstance(node.value, str):
        return False
    if isinstance(node, ast.BinOp) and not isinstance(node.op, (ast.Add, ast.Mod)):
        return False
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in ("format", "strip", "lstrip", "rstrip", "dedent"):
            return False
    parent = strings.parent(node)
    if isinstance(parent, ast.JoinedStr):
        return False
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod)) and node is parent.left:
        return False
    if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add):
        return False
    if isinstance(parent, ast.Attribute) and isinstance(strings.parent(parent), ast.Call):
        name = parent.attr
        if name in ("format", "strip", "lstrip", "rstrip"):
            return False
    if isinstance(parent, ast.Call) and _call_name(parent) == "dedent":
        return False
    return True


def _docstrings(tree):
    """ids of the docstring nodes of the module, its classes and functions."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def _message_strings(tree):
    """ids of every node inside the arguments of print() and logging calls."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in settings.PYTHON_MESSAGE_CALLS:
            for argument in node.args:
                for inner in ast.walk(argument):
                    found.add(id(inner))
    return found
