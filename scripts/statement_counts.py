"""How many statements profiling sends to a database, per table, on the Docker test databases.

    python -m scripts.statement_counts [engine ...] [--json FILE]

For every table of the demo schema the profile is run through the app's own path (the guarded
connection, read_table() and profile_copy()) and the statements the database was sent for it are
counted: the reads (SELECT / WITH) and, separately, the session settings such as
SET TRANSACTION READ ONLY. The catalog's row estimates are one statement for the whole schema.

BEFORE holds what session 2's profiler sent for the same tables (measured on 12 Oct 2026 with the
same containers, before the profiler was changed): one statement per group of columns for the
statistics, one per column for its top values, one per number or date column for its histogram,
and one for the first rows.
"""

from __future__ import annotations

import json
import sys
import time

from app.profiling import queries as q
from app.profiling.data import DatabaseData
from app.profiling.profiler import catalog_estimates, profile_copy, read_table
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from scripts import live_engines as le

# reads per table with session 2's profiler: the same on all four engines
BEFORE = {'accounts': 13, 'branches': 9, 'customers': 10, 'devices': 8, 'invoice_lines': 12, 'invoices': 12,
          'payments': 14, 'plans': 14, 'subscriptions': 11, 'tickets': 14, 'usage_events': 12}


def is_read(statement):
    return statement.lstrip().upper().startswith(('SELECT', 'WITH'))


def measure(engine):
    """{table: {...}} for one engine, and the statements of the row-count step."""
    spec = le.ENGINES[engine]
    db = ReadOnlyDatabase(ConnectionProfile.from_dict(le.reader_profile(engine)), le.READER_PASSWORD)
    try:
        model, _ = db.read_schema()
        mark = len(db.statements)
        estimates = catalog_estimates(db, spec['schema'])
        out = {'_row_counts': {'reads': sum(map(is_read, db.statements[mark:])), 'statements': len(db.statements) - mark}}
        for table in sorted(model.tables, key=lambda t: t.name.lower()):
            mark, started = len(db.statements), time.monotonic()
            data = DatabaseData(db, spec['schema'], table.name)
            plans = [q.ColumnPlan.of(c.name, c.type, engine) for c in table.columns]
            try:
                copy = read_table(data, plans, estimates.get(table.name), size=estimates.sizes.get(table.name))
            finally:
                data.close()
            try:
                profile_copy(copy)
                local = copy.queries
            finally:
                copy.close()
            sent = db.statements[mark:]
            out[table.name.lower()] = {
                'reads': sum(map(is_read, sent)), 'statements': len(sent), 'in_memory': local, 'columns': len(plans),
                'sampled': bool(copy.sampled), 'method': copy.method, 'rows_read': copy.loaded_rows,
                'row_count': copy.row_count, 'row_count_kind': copy.row_count_kind,
                'elapsed_s': round(time.monotonic() - started, 2),
                'read': ' '.join(next(s for s in sent if is_read(s)).split())}
        return out
    finally:
        db.dispose()


def main(argv):
    target = argv[argv.index('--json') + 1] if '--json' in argv else None
    engines = [e for e in argv if e in le.ENGINES] or list(le.ENGINES)
    results = {}
    for engine in engines:
        if not le.reachable(engine):
            print(f'{engine}: not running (scripts\\live_db.ps1 start)')
            continue
        results[engine] = rows = measure(engine)
        print(f'\n{engine}: the row-count step is {rows["_row_counts"]["reads"]} statement for the whole schema')
        print(f'  {"table":<16}{"before":>7}{"now":>5}{"in memory":>11}{"rows read":>11}   how')
        for name, r in rows.items():
            if name.startswith('_'):
                continue
            how = r['method'] or 'the whole table'
            print(f'  {name:<16}{BEFORE.get(name, ""):>7}{r["reads"]:>5}{r["in_memory"]:>11}{r["rows_read"]:>11,}   {how}')
        big = rows.get(le.BIG_TABLE)
        if big:
            print(f'  {le.BIG_TABLE}: {big["read"][:150]} ... ({big["elapsed_s"]} s)')
    if target:
        with open(target, 'w', encoding='utf-8') as f:
            json.dump({'before_reads_per_table': BEFORE, 'now': results}, f, indent=1)
    return results


if __name__ == '__main__':
    main(sys.argv[1:])
