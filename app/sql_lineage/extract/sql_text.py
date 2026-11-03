"""Low-level reading of SQL text: cuts it into tokens so that later steps know
what is code and what is inside a string, a quoted name or a comment.

Token kinds:
    word     a bare word: SELECT, customer, END_DATE, v$session
    punct    any other single character of code: ; ( ) , . = / @ ...
    space    spaces, tabs and line breaks
    string   a string literal, quotes included: 'it''s', q'[x]', $$body$$
    name     a quoted name, quotes included: "Order", `order`, [order]
    comment  -- to the end of the line, or /* ... */

An unterminated string or comment runs to the end of the text.
"""

import re
from dataclasses import dataclass

_WORD = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z0-9_$#\u0080-￿]*")
_SPACE = re.compile(r"\s+")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_Q_CLOSERS = {"[": "]", "{": "}", "(": ")", "<": ">"}


@dataclass
class Token:
    kind: str
    start: int
    end: int
    text: str


def tokenize(text, dialect):
    """Return the list of tokens covering `text` from start to end."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        end, kind = _next_token(text, i, dialect)
        tokens.append(Token(kind, i, end, text[i:end]))
        i = end
    return tokens


def _next_token(text, i, dialect):
    """Find the token that starts at position i. Returns (end, kind)."""
    ch = text[i]
    nxt = text[i + 1] if i + 1 < len(text) else ""

    if ch.isspace():
        return _SPACE.match(text, i).end(), "space"
    if ch == "-" and nxt == "-":
        return _line_end(text, i), "comment"
    if ch == "#" and dialect == "mysql":
        return _line_end(text, i), "comment"
    if ch == "/" and nxt == "*":
        close = text.find("*/", i + 2)
        return (len(text) if close < 0 else close + 2), "comment"
    if dialect == "oracle" and ch in "qQ" and nxt == "'":
        return _oracle_q_string(text, i + 1), "string"
    if dialect == "oracle" and ch in "nN" and text[i + 1:i + 3].lower() == "q'":
        return _oracle_q_string(text, i + 2), "string"
    if ch == "'":
        return _quoted(text, i, "'", backslash=(dialect == "mysql")), "string"
    if ch == '"':
        kind = "string" if dialect == "mysql" else "name"
        return _quoted(text, i, '"', backslash=(dialect == "mysql")), kind
    if ch == "`":
        return _quoted(text, i, "`", backslash=False), "name"
    if ch == "[" and dialect in ("tsql", "sqlite"):
        close = text.find("]", i + 1)
        return (len(text) if close < 0 else close + 1), "name"
    if ch == "$" and dialect == "postgres":
        tag = _DOLLAR_TAG.match(text, i)
        if tag:
            close = text.find(tag.group(0), tag.end())
            return (len(text) if close < 0 else close + len(tag.group(0))), "string"
    word = _WORD.match(text, i)
    if word:
        return word.end(), "word"
    return i + 1, "punct"


def _line_end(text, i):
    """Position of the line break that ends the line holding position i."""
    close = text.find("\n", i)
    return len(text) if close < 0 else close


def _quoted(text, i, quote, backslash):
    """End of a quoted piece that starts at i. A doubled quote is an escaped quote."""
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if backslash and ch == "\\":
            j += 2
            continue
        if ch == quote:
            if j + 1 < n and text[j + 1] == quote:
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _oracle_q_string(text, quote_pos):
    """End of an Oracle q'[...]' string. quote_pos is the position of the first quote."""
    if quote_pos + 1 >= len(text):
        return len(text)
    opener = text[quote_pos + 1]
    closer = _Q_CLOSERS.get(opener, opener)
    close = text.find(closer + "'", quote_pos + 2)
    return len(text) if close < 0 else close + 2


def line_starts(text):
    """Offsets where each line starts. Line 1 starts at offset 0."""
    starts = [0]
    for match in re.finditer("\n", text):
        starts.append(match.end())
    return starts


def line_of(starts, offset):
    """Line number (1-based) of a character offset, given line_starts(text)."""
    low, high = 0, len(starts) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if starts[mid] <= offset:
            low = mid
        else:
            high = mid - 1
    return low + 1
