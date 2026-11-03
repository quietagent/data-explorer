"""Splits the text of a .sql file (or a SQL string found in Python) into
statements, keeping exact line numbers.

Rules, in the order they are checked:
- A line holding only GO or / (settings.SEPARATOR_LINES) ends the statement.
- In mysql, a line "DELIMITER xx" changes the terminator from ; to xx.
- In oracle, a statement starting with a SQL*Plus command (SET, PROMPT, SPOOL,
  EXEC ...) runs to the end of its line, ; included.
- A statement starting with a block word (DECLARE, BEGIN, CREATE PROCEDURE ...,
  see settings.BLOCK_STARTS) is a procedural block. It is not split on ;.
  It ends at a separator line, at a non-; terminator, or, in a file with no
  "/" lines, at the ; that follows the END closing its outermost BEGIN.
- Anything else ends at ; outside strings, quoted names and comments.
- Text after the last terminator is a statement too, unless it is only comments.
"""

from dataclasses import dataclass

from sql_lineage import settings
from sql_lineage.extract.sql_text import line_of, line_starts, tokenize


@dataclass
class SqlPiece:
    """One statement cut out of a SQL text."""
    start: int          # offset of its first character in the text
    end: int            # offset just after its last character
    first_line: int
    last_line: int
    text: str
    block: str = ""     # block kind ("PL/SQL BLOCK", "CREATE PROCEDURE" ...) or ""
    command: str = ""   # the SQL*Plus command word, or ""


def split_sql(text, dialect):
    """Return the statements in `text` as a list of SqlPiece."""
    tokens = tokenize(text, dialect)
    starts = line_starts(text)
    separator_lines = _separator_lines(text, tokens, starts)
    slash_used = any(word == "/" for word in separator_lines.values())

    pieces = []
    terminator = ";"
    current = None      # index of the first token of the open statement
    mode = ""           # "", "block" or "command"
    block_kind = ""
    command_word = ""
    depth = 0
    opened = False
    skip_next_word = False

    def close(end_token_index, include_end=True):
        """Close the open statement, ending with token end_token_index."""
        nonlocal current, mode, depth, opened, block_kind, command_word
        if current is not None:
            last = end_token_index if include_end else end_token_index - 1
            piece = _make_piece(text, tokens, starts, current, last, block_kind, command_word)
            if piece:
                pieces.append(piece)
        current, mode, depth, opened, block_kind, command_word = None, "", 0, False, "", ""

    i = 0
    while i < len(tokens):
        token = tokens[i]
        line = line_of(starts, token.start)

        # A separator line ends whatever is open, and is not part of any statement.
        if line in separator_lines and token.kind != "space":
            close(i, include_end=False)
            i = _skip_line(tokens, i, starts, line)
            continue

        if current is None:
            if token.kind in ("space", "comment"):
                i += 1
                continue
            if token.kind == "punct" and text.startswith(terminator, token.start):
                i = _skip_terminator(tokens, i, terminator)
                continue
            if dialect == "mysql" and token.kind == "word" and token.text.upper() == "DELIMITER":
                new_terminator, i = _read_delimiter(text, tokens, i, starts)
                if new_terminator:
                    terminator = new_terminator
                continue
            current = i
            words = _leading_words(tokens, i, count=6)
            command_word = _sqlplus_command(dialect, words, token)
            if command_word:
                mode = "command"
            else:
                block_kind = _block_kind(dialect, words, tokens, i)
                if block_kind:
                    mode = "block"
                    opened = any(block_kind.startswith(w) for w in settings.BLOCKS_OPEN_AT_START)
                    depth = 1 if opened else 0

        if mode == "command":
            # SQL*Plus commands end at the end of their line; a ; in them is just text.
            if token.kind == "space" and "\n" in token.text:
                close(i, include_end=False)
            i += 1
            continue

        if mode == "block":
            if token.kind == "punct" and terminator != ";" and text.startswith(terminator, token.start):
                close(i, include_end=False)
                i = _skip_terminator(tokens, i, terminator)
                continue
            if token.kind == "word":
                word = token.text.upper()
                if skip_next_word:
                    skip_next_word = False
                elif word in ("BEGIN", "CASE"):
                    if not (word == "BEGIN" and _is_transaction_begin(tokens, i)):
                        depth += 1
                        opened = True
                elif word == "END":
                    depth -= 1
                    skip_next_word = True  # END IF / END LOOP / END CASE / END name
                    following = _next_word(tokens, i)
                    if following in ("IF", "LOOP", "WHILE", "REPEAT"):
                        depth += 1      # those were never counted as opened
            elif token.kind == "punct" and token.text == ";":
                skip_next_word = False
                if opened and depth <= 0 and not slash_used:
                    close(i, include_end=True)
            i += 1
            continue

        # Normal statement.
        if token.kind == "punct" and text.startswith(terminator, token.start):
            close(i, include_end=False)
            i = _skip_terminator(tokens, i, terminator)
            continue
        i += 1

    if current is not None:
        close(len(tokens) - 1, include_end=True)
    return pieces


def _make_piece(text, tokens, starts, first, last, block_kind, command_word):
    """Build a SqlPiece from tokens first..last, trimming trailing space and comments."""
    while last >= first and tokens[last].kind in ("space", "comment"):
        last -= 1
    if last < first:
        return None
    if all(t.kind in ("space", "comment") for t in tokens[first:last + 1]):
        return None
    start, end = tokens[first].start, tokens[last].end
    return SqlPiece(
        start=start,
        end=end,
        first_line=line_of(starts, start),
        last_line=line_of(starts, end - 1),
        text=text[start:end],
        block=block_kind,
        command=command_word,
    )


def _separator_lines(text, tokens, starts):
    """Map line number -> separator word, for lines that hold only GO or /."""
    by_line = {}
    for token in tokens:
        if token.kind == "space":
            continue
        line = line_of(starts, token.start)
        by_line.setdefault(line, []).append(token)
    found = {}
    for line, line_tokens in by_line.items():
        # A token that spans several lines (a block comment) makes its first line non-separator.
        if len(line_tokens) == 2 and line_tokens[0].text.upper() == "GO" and line_tokens[1].text.isdigit():
            line_tokens = line_tokens[:1]
        if len(line_tokens) != 1:
            continue
        token = line_tokens[0]
        if token.kind not in ("word", "punct"):
            continue
        if token.text.upper() in settings.SEPARATOR_LINES:
            found[line] = token.text.upper()
    # A line inside a multi-line string or comment is not a separator line.
    for token in tokens:
        if token.kind in ("string", "name", "comment") and "\n" in token.text:
            first = line_of(starts, token.start)
            last = line_of(starts, token.end - 1)
            for line in range(first, last + 1):
                found.pop(line, None)
    return found


def _skip_line(tokens, i, starts, line):
    """Index of the first token after the given line."""
    while i < len(tokens) and line_of(starts, tokens[i].start) == line:
        i += 1
    return i


def _skip_terminator(tokens, i, terminator):
    """Index of the first token after a terminator that starts at token i."""
    length = 0
    while i < len(tokens) and length < len(terminator):
        length += len(tokens[i].text)
        i += 1
    return i


def _leading_words(tokens, i, count):
    """The first `count` words (upper case) from token i on, skipping space and comments."""
    words = []
    while i < len(tokens) and len(words) < count:
        token = tokens[i]
        if token.kind == "word":
            words.append(token.text.upper())
        elif token.kind == "punct" and token.text in ";":
            break
        elif token.kind not in ("space", "comment"):
            words.append(token.text)
        i += 1
    return words


def _next_word(tokens, i):
    """The next word after token i (upper case), or ""."""
    i += 1
    while i < len(tokens):
        if tokens[i].kind == "word":
            return tokens[i].text.upper()
        if tokens[i].kind not in ("space", "comment"):
            return ""
        i += 1
    return ""


def _is_transaction_begin(tokens, i):
    """True when the BEGIN at token i starts a transaction (BEGIN TRANSACTION, BEGIN;)."""
    j = i + 1
    while j < len(tokens) and tokens[j].kind in ("space", "comment"):
        j += 1
    if j >= len(tokens):
        return True
    following = tokens[j]
    if following.kind == "punct" and following.text == ";":
        return True
    return following.kind == "word" and following.text.upper() in settings.BEGIN_TRANSACTION_WORDS


def _sqlplus_command(dialect, words, token):
    """The SQL*Plus command word that starts this statement, or ""."""
    if dialect != "oracle" or not words:
        return ""
    if token.kind == "punct" and token.text == "@":
        return "@"
    if words[0] in settings.SQLPLUS_COMMANDS:
        # SET is also SQL when it continues an UPDATE; here it starts a statement,
        # so it is a SQL*Plus SET unless it is SET TRANSACTION or SET ROLE.
        if words[0] == "SET" and len(words) > 1 and words[1] in ("TRANSACTION", "ROLE", "CONSTRAINT", "CONSTRAINTS"):
            return ""
        return words[0]
    return ""


def _block_kind(dialect, words, tokens, i):
    """The block kind ("PL/SQL BLOCK", "CREATE PROCEDURE" ...) this statement starts, or ""."""
    if not words:
        return ""
    if words[0] in ("CREATE", "ALTER"):
        rest = [w for w in words[1:] if w not in settings.CREATE_MODIFIERS]
        words = [words[0]] + rest
    for start in settings.BLOCK_STARTS.get(dialect, ()):
        start_words = start.split()
        if words[:len(start_words)] != start_words:
            continue
        if start == "BEGIN" and _is_transaction_begin(tokens, i):
            return ""
        if start in ("DECLARE", "BEGIN"):
            return "PL/SQL BLOCK"
        if start == "CREATE PACKAGE" and len(words) > 2 and words[2] == "BODY":
            return "CREATE PACKAGE BODY"
        return start
    return ""


def _read_delimiter(text, tokens, i, starts):
    """Read a mysql DELIMITER line at token i. Returns (new terminator, next token index)."""
    line = line_of(starts, tokens[i].start)
    j = _skip_line(tokens, i, starts, line)
    line_text = text[tokens[i].start:tokens[j - 1].end]
    parts = line_text.split()
    if len(parts) == 2:
        return parts[1], j
    return "", j
