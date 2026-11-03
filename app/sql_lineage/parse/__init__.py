"""Parse step: each RawStatement is parsed with sqlglot in the chosen dialect
and read into StatementFacts (reader.py).

A statement is "failed" (unreadable, with a reason) when the extract step
already said so, when sqlglot can't parse it, when sqlglot only reads it as a
raw command, or when it calls a procedure. Its type is the extract step's kind
(PL/SQL BLOCK ...), else its first word when that is in
settings.FAILED_TYPE_WORDS, else UNKNOWN. A statement with no lineage (COMMIT,
SAVEPOINT ...) is "read", with no edges.
"""

import logging

import sqlglot
from sqlglot import exp

from sql_lineage import settings
from sql_lineage.extract.sql_text import tokenize
from sql_lineage.parse.facts import StatementFacts
from sql_lineage.parse.reader import StatementReader
from sql_lineage.parse.schema import Schema, load_schema, schema_from_tables

logging.getLogger("sqlglot").setLevel(logging.ERROR)

__all__ = ["parse_statement", "parse_statements", "load_schema", "schema_from_tables", "Schema"]

# What sqlglot returns when it reads a bare word or an expression, not a statement
# ("SAVEPOINT a" is read as the column SAVEPOINT with the alias a).
NOT_STATEMENTS = (exp.Alias, exp.Condition, exp.Identifier, exp.Star, exp.Paren, exp.Tuple)


def parse_statements(raw_statements, dialect, schema=None):
    return [parse_statement(raw, dialect, schema) for raw in raw_statements]


def parse_statement(raw, dialect, schema=None):
    """Read one RawStatement into StatementFacts."""
    first_word = _first_word(raw.text, dialect)

    def failed(reason, kind=None):
        word = first_word if first_word in settings.FAILED_TYPE_WORDS else ""
        return StatementFacts(raw.script, raw.number, kind or raw.kind or word or "UNKNOWN", "failed", reason)

    if raw.reason:
        return failed(raw.reason)
    if first_word in settings.PROCEDURE_CALL_WORDS:
        return failed(settings.REASON_PROCEDURE_CALL, "PROCEDURE CALL")
    try:
        trees = [t for t in sqlglot.parse(raw.text, read=dialect) if t is not None]
    except Exception as error:
        return failed("could not parse: " + _first_line(error))
    if len(trees) != 1:
        return failed(f"could not parse: read as {len(trees)} statements")
    tree = trees[0]

    if isinstance(tree, (exp.Command,) + NOT_STATEMENTS):
        if first_word in settings.COMMANDS_WITHOUT_LINEAGE:
            return StatementFacts(raw.script, raw.number, first_word, "read", None)
        return failed(f"sqlglot could not read this {first_word or 'statement'} statement")
    try:
        return StatementReader(raw, tree, dialect, schema).read()
    except Exception as error:      # a bug in the reader must not stop the scan
        return failed(f"could not be read ({type(error).__name__}: {_first_line(error)})")


def _first_word(text, dialect):
    for token in tokenize(text, dialect):
        if token.kind == "word":
            return token.text.upper()
        if token.kind not in ("space", "comment"):
            return ""
    return ""


def _first_line(error):
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__
