"""Extract step: turns a folder of scripts into statements.

extract_folder(folder, dialect) walks the folder (files.py), decodes each
file, and cuts it into RawStatement records: .sql files with sql_split.py,
.py files with python_sql.py. Every statement carries its script, its number
in the script, its first and last line, and its text. Statements that can't
be read (PL/SQL blocks, SQL built at run time ...) carry a reason instead of
being dropped. SQL*Plus commands (SET, PROMPT ...) are not SQL and make no
statement, except EXEC / EXECUTE, which are unreadable procedure calls.
"""

from pathlib import Path

from sql_lineage import settings
from sql_lineage.extract.files import find_scripts, read_script
from sql_lineage.extract.model import RawStatement, ScriptFile
from sql_lineage.extract.python_sql import find_python_sql
from sql_lineage.extract.sql_split import split_sql

__all__ = ["extract_folder", "extract_file", "extract_sql_text", "find_scripts", "RawStatement", "ScriptFile"]


def extract_folder(folder, dialect):
    """Return (scripts, statements) for every script under `folder`."""
    folder = Path(folder)
    scripts = []
    statements = []
    for path in find_scripts(folder):
        script, found = extract_file(folder, path, dialect)
        scripts.append(script)
        statements.extend(found)
    return scripts, statements


def extract_file(folder, path, dialect):
    """Return (ScriptFile, its statements) for one script under `folder`."""
    script, text = read_script(folder, path)
    if text is None:
        return script, []
    if script.kind == "sql":
        found = extract_sql_text(script.path, text, dialect)
    else:
        found, problem = find_python_sql(script.path, text, dialect)
        if problem:
            script.problem = problem
    for number, statement in enumerate(found, start=1):
        statement.number = number
    return script, found


def extract_sql_text(path, text, dialect):
    """Cut the text of one .sql file into RawStatement records."""
    statements = []
    for piece in split_sql(text, dialect):
        reason = None
        kind = piece.block
        if piece.block:
            reason = settings.REASON_PLSQL
        elif piece.command:
            if piece.command not in settings.SQLPLUS_CALL_COMMANDS:
                continue
            kind = "CLIENT COMMAND"
            reason = settings.REASON_PROCEDURE_CALL
        statements.append(RawStatement(
            script=path,
            number=len(statements) + 1,
            first_line=piece.first_line,
            last_line=piece.last_line,
            text=piece.text,
            reason=reason,
            kind=kind,
            line_base=piece.first_line,
        ))
    return statements
