"""The two records the extract step produces: a script file and a raw statement."""

from dataclasses import dataclass


@dataclass
class ScriptFile:
    """One .sql or .py file of the scanned folder."""
    path: str               # relative to the scanned folder, with / between parts
    kind: str               # "sql" or "python"
    size: int               # bytes
    sha256: str
    encoding: str | None    # "utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp1256", "cp1252", or None
    problem: str | None = None   # why the whole file could not be read, or None


@dataclass
class RawStatement:
    """One statement as cut out of a script, before parsing.

    text is the SQL. For a Python script it is the glued string, with f-string
    values turned into binds (:name). When `reason` is set, the statement could
    not be read and text holds the source it came from.
    """
    script: str             # ScriptFile.path
    number: int             # 1, 2, 3 ... within the script
    first_line: int
    last_line: int
    text: str
    reason: str | None = None   # why it can't be read, or None
    kind: str = ""          # "PL/SQL BLOCK", "CREATE PROCEDURE", "CLIENT COMMAND" (EXEC) ..., or ""
    line_base: int | None = None
    # When set, line k of `text` (1-based) is line line_base + k - 1 of the file.
    # When None, only first_line and last_line are known.
