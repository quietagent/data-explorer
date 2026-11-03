"""The two guards that keep the app local and read-only.

1. LocalOnlyMiddleware - only requests made by this PC, to this PC, from this
   app's own pages are served.
2. check_read_only_sql - the only statements allowed to reach a database are
   SELECT / WITH queries and catalog reads.
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlsplit

from .er.tokenizer import tokenize

# ======================================================== localhost guard ====

LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}

# The browser may only load what this app serves itself: no CDN, no remote calls.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

SECURITY_HEADERS = [
    (b'content-security-policy', CONTENT_SECURITY_POLICY.encode()),
    (b'x-content-type-options', b'nosniff'),
    (b'x-frame-options', b'DENY'),
    (b'referrer-policy', b'no-referrer'),
    (b'cross-origin-resource-policy', b'same-origin'),
]


def host_name(value):
    """'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1'. None if it is not a plain host."""
    value = (value or '').strip().lower()
    if not value or any(ch in value for ch in '/@?#\\ '):
        return None
    if value.startswith('['):
        end = value.find(']')
        if end < 0 or (value[end + 1:] and not re.fullmatch(r':\d{1,5}', value[end + 1:])):
            return None
        return value[1:end]
    host, sep, port = value.partition(':')
    if sep and not re.fullmatch(r'\d{1,5}', port):
        return None
    return host


def is_local_host(value):
    return host_name(value) in LOCAL_HOSTS


def is_local_client(address):
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def is_same_local_origin(origin, host_header):
    """True when an Origin header names this very app (same scheme://host:port, on localhost)."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ('http', 'https') or parts.hostname not in LOCAL_HOSTS:
        return False
    return parts.netloc.lower() == (host_header or '').strip().lower()


def check_request(client, headers, path='/'):
    """Return None if the request may be served, or the reason it is refused.

    client  - address of the peer (scope['client'][0])
    headers - dict of lower-case header names
    """
    if not client or not is_local_client(client):
        return 'Only connections from this PC are accepted.'
    if not is_local_host(headers.get('host')):
        return 'Host header is not localhost.'
    origin = headers.get('origin')
    if origin is not None and not is_same_local_origin(origin, headers.get('host')):
        return 'Origin is not this app.'
    if path.startswith('/api') and headers.get('sec-fetch-site') == 'cross-site':
        return 'Cross-site requests are not accepted.'
    return None


class LocalOnlyMiddleware:
    """ASGI middleware: refuse anything that is not local, and add security headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return
        headers = {}
        for k, v in scope.get('headers') or []:
            headers.setdefault(k.decode('latin-1').lower(), v.decode('latin-1'))
        client = (scope.get('client') or (None,))[0]
        reason = check_request(client, headers, scope.get('path') or '/')
        if reason is not None:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1008})
                return
            body = json.dumps({'detail': reason}).encode()
            await send({'type': 'http.response.start', 'status': 403,
                        'headers': [(b'content-type', b'application/json'),
                                    (b'content-length', str(len(body)).encode())] + SECURITY_HEADERS})
            await send({'type': 'http.response.body', 'body': body})
            return

        api = (scope.get('path') or '').startswith('/api')

        async def send_with_headers(message):
            if message['type'] == 'http.response.start':
                present = {k.lower() for k, _ in message.get('headers') or []}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in present]
                if b'cache-control' not in present:
                    # API answers are never cached; pages and scripts are re-checked on every load,
                    # so an updated app is never mixed with stale files from the browser cache.
                    extra.append((b'cache-control', b'no-store' if api else b'no-cache'))
                message = {**message, 'headers': list(message.get('headers') or []) + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ============================================================ upload limit ====

class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """ASGI middleware: refuse a request whose body is larger than `limit()` bytes with a 413.

    The declared Content-Length is checked before anything is read, and the bytes
    are counted as they arrive, so an upload without a declared length is stopped
    at the limit too. `limit` is a function, so the setting can change at run time.
    """

    def __init__(self, app, limit, message):
        self.app, self.limit, self.message = app, limit, message

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') in ('GET', 'HEAD', 'OPTIONS'):
            await self.app(scope, receive, send)
            return
        limit = int(self.limit())
        declared = next((v for k, v in scope.get('headers') or [] if k == b'content-length'), b'')
        started = False
        if declared.isdigit() and int(declared) > limit:
            await self._refuse(send, limit)
            return
        seen, exceeded = 0, False

        async def counted_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body') or b'')
                if seen > limit:
                    exceeded = True
                    raise _BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if exceeded:                    # whatever the app makes of the cut-off body, the answer is 413
                if not started:
                    started = True
                    await self._refuse(send, limit)
                return
            started = started or message['type'] == 'http.response.start'
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            pass
        if exceeded and not started:
            await self._refuse(send, limit)

    async def _refuse(self, send, limit):
        body = json.dumps({'detail': self.message.format(mb=max(1, round(limit / 1024 / 1024)))}).encode()
        await send({'type': 'http.response.start', 'status': 413,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(body)).encode()), (b'connection', b'close')]})
        await send({'type': 'http.response.body', 'body': body})


# ============================================== no data in error messages ====

# Words that drivers and DuckDB put in quotes and that never come from the data.
SAFE_QUOTED = {
    'bigint', 'int', 'integer', 'int32', 'int64', 'int16', 'int8', 'smallint', 'tinyint', 'hugeint', 'double',
    'double precision', 'float', 'real', 'decimal', 'numeric', 'number', 'varchar', 'varchar2', 'char', 'text',
    'string', 'date', 'time', 'timestamp', 'datetime', 'timestamp with time zone', 'boolean', 'bool', 'uuid', 'json',
    'blob', 'clob', 'interval', 'null', 'true', 'false', 'field list', 'where clause', 'on clause', 'order clause',
    'group statement', 'having clause', 'from clause', 'select', 'from', 'where', 'utf8', 'utf-8',
}
_QUOTED = re.compile(r'"((?:[^"\n]|"")*)"|\'((?:[^\'\n]|\'\')*)\'|`([^`\n]*)`')
_EMAIL_LIKE = re.compile(r'[^\s@"\'<>()]+@[^\s@"\'<>()]+\.[A-Za-z]{2,}')
_LONG_DIGITS = re.compile(r'(?<![\w.])\+?\d[\d\s\-]{6,}\d(?![\w])')
# Lines of an error text that quote the data or the row itself: PostgreSQL's DETAIL and CONTEXT, and
# Oracle 23's "details" line (ORA-03302: (ORA-01722 details) invalid string value: <the value>).
_DATA_LINES = re.compile(r'^\s*((DETAIL|CONTEXT|Help)\s*:|ORA-\d+:\s*\(ORA-\d+ details\))', re.I)


def sanitize_error(message, keep=(), limit=600):
    """An error text with nothing in it that could have come from the data.

    Drivers and DuckDB like to quote the value they tripped over ("Could not convert string
    "Ahmed" ...", "Original Line: 17,Ahmed,0551234567", "Key (email)=(a@b.com)"). Every error is
    passed through here before it is shown, stored or put in a job's errors:

      * a line that repeats the row ("Original Line", DETAIL, CONTEXT) is dropped
      * whatever stands in quotes becomes "…", unless it is a name the app already knows
        (`keep`: schema, table and column names, or the words of a statement the user typed)
        or a fixed word such as a type name
      * e-mail addresses and long runs of digits outside quotes become …
    """
    known = {str(k).strip().lower() for k in keep if k is not None and str(k).strip()}
    known |= {part for k in list(known) for part in re.split(r'[.\s]+', k) if part}

    def quoted(match):
        text = match.group(0)
        inner = next(g for g in match.groups() if g is not None)
        plain = inner.strip().lower()
        if plain in SAFE_QUOTED or plain in known:
            return text
        parts = [p for p in re.split(r'[.\s(),]+', plain) if p]      # schema.table, or a signature: sum(VARCHAR)
        if parts and all(p.strip('"[]`') in known or p in SAFE_QUOTED for p in parts):
            return text
        return text[0] + '…' + text[-1]

    lines, skip = [], False
    for line in str(message).splitlines():
        if skip:                                        # the row itself, printed under "Original Line:"
            skip = not line.strip()
            continue
        original = re.match(r'^\s*Original Line\s*:\s*(.*)$', line, re.I)
        if original:
            skip = not original.group(1).strip()
            continue
        if _DATA_LINES.match(line):
            continue
        lines.append(line)
    text = _QUOTED.sub(quoted, '\n'.join(lines))
    text = _EMAIL_LIKE.sub('…', text)
    text = _LONG_DIGITS.sub('…', text)
    text = ' '.join(text.split())
    return text[:limit]


# ====================================================== read-only SQL guard ====

from .sqlguard import ReadOnlyViolation, check as check_user_sql      # noqa: E402 - the guard for typed SQL


# Session settings the app itself applies when it opens a connection. They only
# restrict the session (read-only mode, timeouts) and are matched exactly.
SETUP_STATEMENTS = [re.compile(p) for p in (
    r'PRAGMA query_only = ON',
    r'SET TRANSACTION READ ONLY',
    r'SET SESSION TRANSACTION READ ONLY',
    r'SET LOCAL statement_timeout = \d+',
    r'SET SESSION MAX_EXECUTION_TIME = \d+',
    r'SET SESSION max_statement_time = \d+',
)]

# SQLite catalog reads used by SQLAlchemy's inspector. PRAGMA can also write, so
# only these names are allowed, and never with "= value".
READ_PRAGMAS = {'TABLE_INFO', 'TABLE_XINFO', 'TABLE_LIST', 'FOREIGN_KEY_LIST', 'INDEX_LIST',
                'INDEX_INFO', 'INDEX_XINFO', 'DATABASE_LIST', 'COLLATION_LIST'}

# Words that turn a SELECT / WITH statement into a write, a lock or a call.
WRITE_WORDS = {'INSERT', 'UPDATE', 'DELETE', 'MERGE', 'UPSERT', 'INTO', 'DROP', 'ALTER', 'CREATE',
               'TRUNCATE', 'RENAME', 'GRANT', 'REVOKE', 'EXEC', 'EXECUTE', 'CALL', 'LOCK'}

def check_read_only_sql(sql, dialect=None, strict=False, names=None):
    """Raise ReadOnlyViolation unless `sql` is a single read-only statement.

    strict=False  the app's own statements: SELECT / WITH, catalog reads (SHOW, DESCRIBE, a few
                  read PRAGMAs) and the session settings above. Every statement on a guarded
                  connection passes here, whoever wrote it.
    strict=True   SQL typed by a person (the SQL tab): the dialect-aware guard of app/sqlguard.py -
                  SELECT / WITH only, allow-listed functions only, no locks, no tricks. Returns the
                  checked statement (`.sql` is the text to send: comments blanked out).
    """
    if strict:
        return check_user_sql(sql, dialect, names)
    text = (sql or '').strip()
    if not text:
        raise ReadOnlyViolation('Empty statement.')
    if '\x00' in text or '/*!' in text:
        raise ReadOnlyViolation('Statement contains an executable comment or a NUL character.')
    if any(p.fullmatch(text) for p in SETUP_STATEMENTS):
        return None

    toks = tokenize(text)
    while toks and toks[-1].kind == 'op' and toks[-1].text == ';':
        toks.pop()
    if not toks:
        raise ReadOnlyViolation('Empty statement.')
    if any(t.kind == 'op' and t.text == ';' for t in toks):
        raise ReadOnlyViolation('Only one statement at a time is allowed.')

    first = next((t for t in toks if not (t.kind == 'op' and t.text == '(')), None)
    word = first.up if first is not None and first.kind == 'ident' else ''

    if word in ('SELECT', 'WITH'):
        for i, t in enumerate(toks):
            if t.kind != 'ident':
                continue
            if t.up in WRITE_WORDS:
                raise ReadOnlyViolation(f'{t.up} is not allowed: only read-only queries can run.')
            if t.up == 'FOR' and i + 1 < len(toks) and toks[i + 1].up in ('UPDATE', 'SHARE', 'KEY', 'NO'):
                raise ReadOnlyViolation('Locking reads (FOR UPDATE / FOR SHARE) are not allowed.')
        return None

    if word in ('SHOW', 'DESCRIBE', 'DESC'):
        return None
    if (word == 'USE' and dialect == 'mssql' and len(toks) == 2 and toks[0] is first
            and toks[1].kind in ('ident', 'qident')):
        return None                   # SQL Server: SQLAlchemy switches database to read "db.schema"
    if word == 'PRAGMA' and toks[0] is first:
        # PRAGMA [schema.]name[(argument)] - the name must be one of the catalog reads
        i = 3 if len(toks) > 3 and toks[2].kind == 'op' and toks[2].text == '.' else 1
        name = toks[i].up if i < len(toks) and toks[i].kind == 'ident' else ''
        if name in READ_PRAGMAS and not any(t.kind == 'op' and t.text == '=' for t in toks):
            return None
        raise ReadOnlyViolation('This PRAGMA is not a catalog read.')

    start = word or (first.text if first is not None else '(')
    raise ReadOnlyViolation(f"Only read-only queries can run (statement starts with '{start}').")
