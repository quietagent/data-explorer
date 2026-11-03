"""Which statements really call a function without brackets - asked of the server itself.

    python -m scripts.call_proof oracle
    python -m scripts.call_proof postgresql

Oracle calls a function that takes no argument without brackets (SELECT x_ping FROM dual), and
PostgreSQL calls f(t) for t.f. The statements are tests/hostile.py's ORACLE_CALLS and
POSTGRES_CALLS; the functions they aim at write one row per call into dx_extra.x_calls
(scripts/live_engines.py, ensure_extras).

For each statement this prints, as one row of a Markdown table:

  guard     what app.sqlguard says today, with the names of the source as the app reads them
            (the read-only account, the demo schema and dx_extra)
  server    what the server does with it on the RAW driver, as the read-only account, inside the
            read-only transaction the app uses: rows, or the server's error
  calls     the rows the statement left in x_calls - the proof that the function ran

It needs the test containers (scripts\\live_db.ps1 start). Nothing here is used by the app.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))

import hostile                                                  # noqa: E402
from app.analysis.sqlrunner import SqlRunner                    # noqa: E402
from app.security import ReadOnlyViolation, check_read_only_sql  # noqa: E402
from app.sources.database import ConnectionProfile, ReadOnlyDatabase  # noqa: E402
from scripts import live_engines as le                          # noqa: E402


def source_names(engine):
    """What the guard is given for this source: read through the app's own path."""
    profile = ConnectionProfile.from_dict(le.reader_profile(engine, schema_filter=le.extra_filter(engine)))
    db = ReadOnlyDatabase(profile, le.READER_PASSWORD)
    try:
        model, _ = db.read_schema('proof', 'proof')
    finally:
        db.dispose()
    return SqlRunner._names({'model': model, 'kind': 'database', 'settings': profile.to_dict()})


def raw_connection(engine):
    """The driver's own connection as the read-only account: no app, no guard."""
    spec = le.ENGINES[engine]
    if engine == 'oracle':
        import oracledb
        return oracledb.connect(user=spec['reader'], password=le.READER_PASSWORD,
                                dsn=f'{le.HOST}:{spec["port"]}/{spec["database"]}')
    import psycopg
    return psycopg.connect(host=le.HOST, port=spec['port'], dbname=spec['database'], user=spec['reader'],
                           password=le.READER_PASSWORD)


def logged(engine, clear=True):
    """{function: calls} in x_calls, read (and emptied) with the admin account."""
    x = le.extra_schema(engine)
    eng = le.admin_engine(engine)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f'SELECT fn, COUNT(*) FROM {x}.{le.CALL_LOG} GROUP BY fn ORDER BY fn')).fetchall()
            if clear and rows:
                conn.execute(text(f'DELETE FROM {x}.{le.CALL_LOG}'))
                conn.commit()
        return {str(fn): int(n) for fn, n in rows}
    finally:
        eng.dispose()


def on_server(engine, sql, read_only=True):
    """Run one statement on the raw driver. Returns (what the server answered, {function: calls}).

    read_only: inside the read-only transaction the app uses, rolled back afterwards. Oracle's
    functions write through an autonomous transaction, so their rows stay whatever happens here.
    PostgreSQL has none: without read_only the transaction is committed, to keep what the function wrote."""
    logged(engine)
    conn = raw_connection(engine)
    try:
        cursor = conn.cursor()
        try:
            if read_only:
                cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute(sql)
            rows = cursor.fetchmany(3)
            answer = f'ran: {len(rows)} row{"" if len(rows) == 1 else "s"} fetched'
            if not read_only:
                conn.commit()
        except Exception as exc:
            answer = ' '.join(str(exc).split())
            answer = answer.split(' Help:')[0].split(' LINE ')[0].split(' CONTEXT:')[0][:110]
        finally:
            conn.rollback()
    finally:
        conn.close()
    return answer, logged(engine)


def verdict(engine, sql, names):
    try:
        check_read_only_sql(sql, engine, strict=True, names=names)
        return 'passes'
    except ReadOnlyViolation:
        return 'refused'


def proof(engine, log=print):
    le.ensure_extras(engine)
    spec = le.ENGINES[engine]
    names = source_names(engine)
    rows = []
    log(f'| # | {engine}: the statement | Guard | The server, on the raw driver in a read-only transaction | Calls logged |')
    log('| --- | --- | --- | --- | --- |')
    for what, sql in sorted(hostile.calls(engine, le.extra_schema(engine), spec['schema'], proof=True),
                            key=lambda item: int(''.join(ch for ch in item[0].split()[0] if ch.isdigit()))):
        guard = verdict(engine, sql, names)
        answer, calls = on_server(engine, sql)
        if engine == 'postgresql' and not calls:
            # a read-only transaction stops the write there: run it once more without, to see the call
            plain, calls = on_server(engine, sql, read_only=False)
            if calls:
                answer += f' - outside a read-only transaction: {plain}'
        called = ', '.join(f'{fn} x{n}' for fn, n in calls.items()) or '-'
        rows.append((what, sql, guard, answer, calls))
        log(f'| {what} | `{sql}` | {guard} | {answer} | {called} |')
    ran = sum(1 for r in rows if r[4])
    log(f'\n{engine}: {len(rows)} statements, {ran} called a function, '
        f'{sum(1 for r in rows if r[2] == "passes")} pass the guard, '
        f'{sum(1 for r in rows if r[2] == "passes" and r[4])} pass the guard AND call a function.')
    return rows


if __name__ == '__main__':
    for name in sys.argv[1:] or ['oracle']:
        proof(name)
