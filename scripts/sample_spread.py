"""How even a sample of the 2,000,000-row test table is, per engine, through the app's own read.

    python -m scripts.sample_spread [oracle] [postgresql] [mssql] [--seeds 34]

usage_events was filled in event_id order, so its 2,000,000 rows are 200 stretches of 10,000
ids. For each seed the app's one read of the table (read_table() on the guarded connection, as the
read-only account, with the percent the app works out from the catalog's estimate) is taken with
that seed, and two things are counted in the in-memory copy: how many rows came back, and how
many of the 200 stretches they come from. An even sample of about 100,000 rows touches all 200.

Oracle is measured twice: as the app samples this table (rows: it is far below the size limit)
and as it samples a table above DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB (whole blocks).
"""

from __future__ import annotations

import statistics
import sys
import time

from app import config
from app.profiling import queries as q
from app.profiling.data import DatabaseData
from app.profiling.profiler import catalog_estimates, read_table
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from scripts import live_engines as le

STRETCH = 10_000


def measure(engine, seeds=34, log=print):
    """[{method, rows: [...], stretches: [...], seconds: [...]}] for one engine."""
    spec = le.ENGINES[engine]
    db = ReadOnlyDatabase(ConnectionProfile.from_dict(le.reader_profile(engine)), le.READER_PASSWORD)
    out = []
    try:
        model, _ = db.read_schema()
        table = next(t for t in model.tables if t.name.lower() == le.BIG_TABLE)
        key = next(c for c in table.columns if c.name.lower() == 'event_id')
        plans = [q.ColumnPlan.of(c.name, c.type, engine) for c in table.columns]
        estimates = catalog_estimates(db, spec['schema'])
        estimate, size = estimates.get(table.name), estimates.sizes.get(table.name)
        # what the app does with this table by itself: the method, and the percent every seed is read with
        data = DatabaseData(db, spec['schema'], table.name)
        try:
            own = read_table(data, plans, estimate, only={key.name}, size=size)
        finally:
            data.close()
        plan, own_method = dict(own.plan), own.method
        own.close()
        log(f'{engine}: estimate {estimate:,} rows' + (f', {size / 1024 ** 2:,.0f} MB by the catalog' if size else '')
            + f' -> the app reads it with {own_method}')
        variants = [(plan, own_method)]
        if engine == 'oracle':
            blocks = {**plan, 'block': not plan.get('block')}
            variants.append((blocks, q.sample_text(engine, plan['percent'], block=blocks['block'])))
        total = le.BIG_ROWS // STRETCH
        for variant, method in variants:
            rows, stretches, seconds = [], [], []
            for seed in range(1, seeds + 1):
                started = time.monotonic()
                data = DatabaseData(db, spec['schema'], table.name)
                try:
                    copy = read_table(data, plans, estimate, only={key.name}, plan={**variant, 'seed': seed})
                finally:
                    data.close()
                try:
                    name = q.quote_duck(copy.columns[0][0])
                    n, covered = copy._con.execute(
                        f'SELECT COUNT(*), COUNT(DISTINCT CAST(FLOOR(({name} - 1) / {STRETCH}) AS BIGINT)) FROM data').fetchone()
                finally:
                    copy.close()
                rows.append(int(n))
                stretches.append(int(covered))
                seconds.append(time.monotonic() - started)
            out.append({'engine': engine, 'method': method, 'rows': rows, 'stretches': stretches, 'seconds': seconds,
                        'of': total, 'cap': variant.get('limit')})
    finally:
        db.dispose()
    return out


def line(result):
    rows, stretches = sorted(result['rows']), sorted(result['stretches'])
    empty = sum(1 for n in rows if n == 0)
    cut = sum(1 for n in rows if result['cap'] and n >= result['cap'])
    return (f'| {result["engine"]} | {result["method"].split(":")[0]} | {rows[0]:,} | {int(statistics.median(rows)):,} | {rows[-1]:,} '
            f'| {stretches[0]} | {int(statistics.median(stretches))} | {stretches[-1]} | {empty} | {cut} '
            f'| {statistics.median(result["seconds"]):.1f} s |')


def main(argv):
    seeds = int(argv[argv.index('--seeds') + 1]) if '--seeds' in argv else 34
    engines = [e for e in argv if e in q.PERCENT_SAMPLING] or list(q.PERCENT_SAMPLING)
    results = []
    for engine in engines:
        if not le.reachable(engine):
            print(f'{engine}: not running (scripts\\live_db.ps1 start)')
            continue
        results += measure(engine, seeds)
    print(f'\n{seeds} seeds each; target {config.SAMPLE_TARGET_ROWS:,} rows; stretches of {STRETCH:,} ids, {le.BIG_ROWS // STRETCH} in the table\n')
    print('| Engine | Clause | Rows: least | median | most | Stretches covered: least | median | most | Empty samples '
          '| Cut at the cap | Time (median) |')
    print('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for result in results:
        print(line(result))
    return results


if __name__ == '__main__':
    main(sys.argv[1:])
