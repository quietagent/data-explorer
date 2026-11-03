"""Turns SQL text that has run-time holes in it (f-string fields, .format() and
% fields) into SQL that can be read, or says why it can't be.

Each hole is first written as a marker word (__sqlhole0__, __sqlhole1__ ...)
so the SQL can be cut into statements and tokens. Then, for each hole:

- inside a '...' string it is a value: '{start}' becomes the bind :start, and a
  hole inside a longer string is left as {start} in the string;
- inside a quoted name ("...", `...`, [...]) or glued to a word (sales_{year})
  it is part of a name: the statement is unreadable, "name built at run time";
- in a comment it is left as text;
- anywhere else sqlglot decides: the hole becomes a bind and must land in a
  value position (compared with a column, in an IN list, BETWEEN bounds,
  LIMIT, VALUES). Anywhere else (FROM {table}, SELECT {cols}, WHERE {cond})
  the statement is unreadable.

Python DB-API binds (%s and %(name)s) are rewritten as ? and :name, which
every sqlglot dialect reads.
"""

import logging
import re

import sqlglot
from sqlglot import exp

from sql_lineage import settings
from sql_lineage.extract.python_strings import Hole
from sql_lineage.extract.sql_text import tokenize

logging.getLogger("sqlglot").setLevel(logging.ERROR)

MARKER = "__sqlhole{}__"
_MARKER_RE = re.compile(r"__sqlhole(\d+)__")

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.ILike,
                exp.NullSafeEQ, exp.NullSafeNEQ, exp.Glob, exp.SimilarTo, exp.RegexpLike, exp.Is)
_VALUE_WRAPPERS = (exp.Func, exp.Cast, exp.Paren, exp.Neg, exp.Add, exp.Sub, exp.Mul,
                   exp.Div, exp.Mod, exp.DPipe)
_CONDITION_PARENTS = (exp.Where, exp.Having, exp.Qualify, exp.And, exp.Or, exp.Not, exp.Join)


def render(parts):
    """Join StrValue parts into one text, with a marker word for each hole.

    Returns (text, labels) where labels[i] is the bind name for marker i.
    """
    pieces = []
    labels = []
    for part in parts:
        if isinstance(part, Hole):
            pieces.append(MARKER.format(len(labels)))
            labels.append(part.label)
        else:
            pieces.append(part)
    return "".join(pieces), labels


def fill(text, labels, dialect):
    """Replace the markers in one statement's text. Returns (sql, reason).

    sql is the readable text with binds, or None when reason says why it can't be read.
    """
    tokens = tokenize(text, dialect)
    out = []
    code_holes = []        # marker numbers that sit in code
    for token in tokens:
        markers = _MARKER_RE.findall(token.text)
        if not markers:
            out.append(token.text)
            continue
        if token.kind == "string":
            whole = re.fullmatch(r"'__sqlhole(\d+)__'", token.text)
            if whole:
                out.append(":" + _bind_name(labels, int(whole.group(1))))
            else:
                out.append(_MARKER_RE.sub(lambda m: "{" + labels[int(m.group(1))] + "}", token.text))
        elif token.kind == "comment":
            out.append(_MARKER_RE.sub(lambda m: "{" + labels[int(m.group(1))] + "}", token.text))
        elif token.kind == "word" and _MARKER_RE.fullmatch(token.text):
            code_holes.append(int(markers[0]))
            out.append(token.text)
        else:
            return None, settings.REASON_NAME_AT_RUN_TIME
    marked = "".join(out)
    marked = normalize_python_binds(marked, dialect)
    if not code_holes:
        return marked, None

    as_binds = _MARKER_RE.sub(lambda m: f":__hole{m.group(1)}", marked)
    try:
        tree = sqlglot.parse_one(as_binds, read=dialect)
    except Exception:
        return None, _reason_when_unparsable(marked, dialect)

    for placeholder in tree.find_all(exp.Placeholder):
        name = placeholder.name or ""
        if not name.startswith("__hole"):
            continue
        if not _in_value_position(placeholder):
            if isinstance(placeholder.parent, _CONDITION_PARENTS):
                return None, settings.REASON_SQL_AT_RUN_TIME
            return None, settings.REASON_NAME_AT_RUN_TIME
    return _MARKER_RE.sub(lambda m: ":" + _bind_name(labels, int(m.group(1))), marked), None


def normalize_python_binds(text, dialect):
    """Rewrite %s as ? and %(name)s as :name, outside strings and comments."""
    tokens = tokenize(text, dialect)
    out = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "punct" and token.text == "%":
            following = tokens[i + 1:i + 5]
            if following and following[0].kind == "word" and following[0].text == "s":
                out.append("?")
                i += 2
                continue
            if (len(following) == 4 and following[0].text == "(" and following[1].kind == "word"
                    and following[2].text == ")" and following[3].text == "s"):
                out.append(":" + following[1].text)
                i += 5
                continue
        out.append(token.text)
        i += 1
    return "".join(out)


def has_markers(text):
    return bool(_MARKER_RE.search(text))


def without_markers(text):
    """The text with each marker replaced by a plain name, for a first-word or shape test."""
    return _MARKER_RE.sub(lambda m: f"hole{m.group(1)}", text)


def show_holes(text, labels):
    """The text with each marker shown as {label}, as the Python code wrote it."""
    return _MARKER_RE.sub(lambda m: "{" + _bind_name(labels, int(m.group(1))) + "}", text)


def _bind_name(labels, number):
    return labels[number] if number < len(labels) else f"p{number}"


def _reason_when_unparsable(marked, dialect):
    """Why a statement with holes in code positions can't be parsed."""
    stripped = marked.strip()
    if _MARKER_RE.match(stripped) or re.search(r"__sqlhole\d+__\s*$", stripped):
        return settings.REASON_SQL_AT_RUN_TIME
    as_names = _MARKER_RE.sub(lambda m: f"hole{m.group(1)}", marked)
    try:
        sqlglot.parse_one(as_names, read=dialect)
    except Exception:
        return settings.REASON_SQL_AT_RUN_TIME
    return settings.REASON_NAME_AT_RUN_TIME


def _in_value_position(placeholder):
    """True when a bind placed here can only be a value (see the module docstring)."""
    node = placeholder
    parent = node.parent
    while isinstance(parent, _VALUE_WRAPPERS) and not isinstance(parent, exp.Column) \
            and parent.find(exp.Column) is None:
        node, parent = parent, parent.parent
    if isinstance(parent, _COMPARISONS):
        other = parent.expression if node is parent.this else parent.this
        return other is not None and other.find(exp.Column) is not None
    if isinstance(parent, exp.In):
        return node is not parent.this and parent.this.find(exp.Column) is not None
    if isinstance(parent, exp.Between):
        return node is not parent.this and parent.this.find(exp.Column) is not None
    if isinstance(parent, (exp.Limit, exp.Offset, exp.Fetch)):
        return True
    if isinstance(parent, exp.Tuple) and isinstance(parent.parent, exp.Values):
        return True
    if isinstance(parent, exp.Values):
        return True
    return False
