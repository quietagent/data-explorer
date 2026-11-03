"""The round after the demo dry run (13 Oct 2026): what the app showed on the Rimal Telecom demo database, fixed.

That dry run went through the demo script on C:\\Projects\\Demo_Database\\out\\rimal_telecom_medium.db and
listed twelve problems (its HANDOFF.md, "App problems found"). Each has tests where its code is tested:

    1  a date key never matched                      test_relations.py, test_live_engines.py
    2  orphan counts lower than the real ones         test_relations.py, test_live_engines.py
    3  the report without the limit, "100%"          test_report.py
    5  a profile throws away an exact count          here
    6  row counts on SQLite; the list sorts           here, test_session3_fixes.py
    7  the first rows of a table in time order        here
    8  two notes that contradict each other           here
    9  links inferred from names                      here, test_model.py
    4  links found by values                          here, test_relations.py, test_live_engines.py
   10  one-to-one in the exported diagrams            here
   11  a line as long as its label                    here (the page's layout, run with Node.js)
   12  the loop of a table that points at itself      here

The first half uses small tables made on the spot. The second half reads the demo databases themselves
(read-only, like every source) and skips itself where they are not on the PC; DRY_RUN_DEMO_DIR names another
folder. "medium" is 523 MB with 4.49 M rows, "large" 1.2 GB with 10.07 M.
"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect

from app import config
from app.analysis.relations import DatabaseParent, WholeTable, measure
from app.profiling import queries as q
from app.profiling import service as service_module
from app.profiling.data import DatabaseData, QueryFailed
from app.profiling.profiler import read_table, spread_plan
from app.security import check_read_only_sql
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from conftest import ROOT

DEMO_DIR = Path(os.environ.get('DRY_RUN_DEMO_DIR') or r'C:\Projects\Demo_Database') / 'out'
MEDIUM, LARGE = DEMO_DIR / 'rimal_telecom_medium.db', DEMO_DIR / 'rimal_telecom_large.db'
medium = pytest.mark.skipif(not MEDIUM.is_file(), reason=f'the demo database is not on this PC: {MEDIUM}')
large = pytest.mark.skipif(not LARGE.is_file(), reason=f'the large demo database is not on this PC: {LARGE}')


def wait(client, job, seconds=600):
    assert 'id' in job, job
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def profile(client, ws, tables='all', **body):
    started = client.post(f'/api/workspaces/{ws}/profile', json={'tables': tables, **body})
    assert started.status_code == 200, started.text
    return wait(client, started.json())


def page(client, ws, table):
    response = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': 'false'})
    assert response.status_code == 200, response.text
    return response.json()


def stats(profiled):
    return {c['name']: c['stats'] for c in profiled['columns']}


def overview(client, ws):
    return {t['id']: t for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Dry run'}).json()['id']


class Recorder(ReadOnlyDatabase):
    opened = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        Recorder.opened.append(self)

    @classmethod
    def reads(cls):
        return [s for db in cls.opened for s in db.statements if s.lstrip().upper().startswith(('SELECT', 'WITH'))]


@pytest.fixture
def recorder(monkeypatch):
    Recorder.opened = []
    monkeypatch.setattr(service_module, 'ReadOnlyDatabase', Recorder)
    return Recorder


# ------------------------------------------------------- a table filled in time order

SIMS = 600          # the first 420 are plastic cards issued in 2024; the eSIMs came later, all in 2025


def sims_db(tmp_path, name='sims.db'):
    """SIM cards in the order they were issued, like the demo's SUB_SIM_CARD: the first 70% are 'PHYSICAL', and
    only the last 180 rows are 'ESIM'. The first rows of this table say that there is one kind of SIM."""
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE sim_cards (sim_id INTEGER PRIMARY KEY, sim_type VARCHAR(10), issued DATE, batch INTEGER);
        CREATE TABLE plans (plan_code VARCHAR(8) PRIMARY KEY, label VARCHAR(20)) WITHOUT ROWID;
        CREATE TABLE notes (rowid INTEGER, note VARCHAR(20));
        CREATE VIEW v_sims AS SELECT sim_id, sim_type, issued FROM sim_cards;''')
    con.executemany('INSERT INTO sim_cards VALUES (?, ?, ?, ?)', [
        (i, 'PHYSICAL' if i <= 420 else 'ESIM', (date(2024, 1, 1) + timedelta(days=i - 1)).isoformat(), i // 50)
        for i in range(1, SIMS + 1)])                                   # one a day, from 1 January 2024 to 22 August 2025
    con.executemany('INSERT INTO plans VALUES (?, ?)', [(f'P{i:04d}', 'early' if i <= 250 else 'late') for i in range(1, 301)])
    con.executemany('INSERT INTO notes VALUES (?, ?)', [(i * 7, 'old' if i <= 200 else 'new') for i in range(1, 301)])
    con.commit()
    con.close()
    return path


@pytest.fixture
def sims(client, ws, tmp_path):
    added = client.post(f'/api/workspaces/{ws}/sources/db',
                        json={'engine': 'sqlite', 'database': str(sims_db(tmp_path)), 'include_views': True})
    assert added.status_code == 200, added.text
    return ws


def test_a_sqlite_table_is_read_on_a_sample_spread_over_it(client, sims, recorder, monkeypatch, tmp_path):
    """Problem 7. The first 100,000 rows of USG_CDR were six weeks of two years, and SUB_SIM_CARD.SIM_TYPE was reported
    as a constant column although the table has 17,510 eSIMs. A sample spread over the table's row numbers reads the
    same number of rows and shows all of the table."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    recorder.opened = []
    job = profile(client, sims, ['sim_cards'])
    assert (job['state'], job['errors'], job['notes']) == ('done', [], [])
    p = page(client, sims, 'sim_cards')
    assert (p['sampled'], p['sample_random'], p['rows_profiled']) == (True, False, 100)
    assert p['sample_method'] == '20 stretches of 5 rows spread over the table, not random'
    assert (p['row_count'], p['row_count_kind'], p['notes']) == (SIMS, 'exact', [])
    st = stats(p)
    assert (st['sim_id']['min'], st['sim_id']['max']) == (1, 575)                  # rows 1-5, 31-35 ... 571-575
    assert st['sim_type']['distinct'] == 2 and dict(map(tuple, st['sim_type']['top'])) == {'PHYSICAL': 70, 'ESIM': 30}
    assert (st['issued']['min'], st['issued']['max']) == ('2024-01-01', '2025-07-28')       # both years
    assert [b['label'][:4] for b in st['issued']['histogram']['bins']].count('2025') == 7
    findings = client.get(f'/api/workspaces/{sims}/quality').json()['findings']
    assert not [f for f in findings if f['rule'] == 'constant']                    # no "Constant column" for sim_type

    # what it cost the database: a count, the two ends of the row numbers, and ONE read that goes by row number
    reads = recorder.reads()
    assert reads[0] == 'SELECT count(*) AS n \nFROM "sim_cards"'
    assert reads[1] == 'SELECT min(rowid) AS min_1, max(rowid) AS max_1 \nFROM "sim_cards"'
    assert len(reads) == 3 and p['queries'] == 2
    read = ' '.join(reads[2].split())
    assert read == ('WITH RECURSIVE s(i) AS (SELECT 0 AS i UNION ALL SELECT s.i + 1 AS anon_1 FROM s WHERE s.i + 1 < ?) '
                    'SELECT t."sim_id" AS c0, t."sim_type" AS c1, t."issued" AS c2, t."batch" AS c3 '
                    'FROM s JOIN "sim_cards" AS t ON t.rowid IN (SELECT x.rowid FROM "sim_cards" AS x '
                    'WHERE x.rowid >= ? + s.i * ? + (s.i * ?) / ? AND x.rowid < ? + (s.i + 1) * ? + ((s.i + 1) * ?) / ? '
                    'ORDER BY x.rowid LIMIT ? OFFSET ?) LIMIT ? OFFSET ?')
    for database in recorder.opened:
        assert database.blocked == []
        for statement in database.statements:
            check_read_only_sql(statement, dialect='sqlite')
    # the table is entered by row number for every stretch - it is never scanned, and nothing is sorted
    con = sqlite3.connect(tmp_path / 'sims.db')
    parameters = (20, 1, 30, 0, 20, 1, 30, 0, 20, 5, 0, 100, 0)
    assert len(con.execute(reads[2], parameters).fetchall()) == 100
    plan = [row[3] for row in con.execute('EXPLAIN QUERY PLAN ' + reads[2], parameters)]
    con.close()
    assert 'SEARCH t USING INTEGER PRIMARY KEY (rowid=?)' in plan and 'SEARCH x USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)' in plan
    assert not any(step.startswith(('SCAN t', 'SCAN x')) or 'TEMP B-TREE' in step for step in plan), plan

    # the first rows, for comparison: what the app read before, and still reads when it has not counted the table
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(tmp_path / 'sims.db')))
    plans = [q.ColumnPlan.of(name, kind, 'sqlite') for name, kind in (('sim_id', 'INTEGER'), ('sim_type', 'VARCHAR(10)'))]
    data = DatabaseData(db, None, 'sim_cards')
    first = read_table(data, plans, settings={'sample_rows': 100})
    assert (first.method, first.row_count_kind) == ('first 100 rows, not random', 'more_than')
    assert first.run(q.top_values_query(q.table_source(q.DUCKDB, None, 'data'), 'sim_type'))[0] == ('PHYSICAL', 100)
    first.close()
    data.close()
    db.dispose()


def test_a_sample_is_labelled_wherever_it_is_shown(client, sims, monkeypatch):
    """ "spread over the table, not random", like the other samples: on the table's page, in the list of the Profile
    tab, with each finding of the Quality tab, and in the report's notes on sampling."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    profile(client, sims)
    label = '20 stretches of 5 rows spread over the table, not random'
    listed = overview(client, sims)['sim_cards']
    assert (listed['sampled'], listed['rows_profiled'], listed['sample_method']) == (True, 100, label)
    findings = client.get(f'/api/workspaces/{sims}/quality').json()['findings']
    sampled = [f for f in findings if f['sampled']]
    assert sampled and {f['sample_method'] for f in sampled if f['table'] == 'sim_cards'} <= {label}
    assert all(f['sample_method'] for f in sampled) and all(f['sample_method'] is None for f in findings if not f['sampled'])
    html = client.get(f'/api/workspaces/{sims}/report?format=html').text
    assert f'profiled on 100 rows of 600 - {label}' in html and f'sampled: 100 rows, {label}' in html
    assert 'SQLite: short stretches of rows spread over the whole table' in html
    assert all(f'title="{f["sample_method"]}">sample</span>' in html for f in sampled)
    script = (ROOT / 'static' / 'profile.js').read_text(encoding='utf-8')
    assert "'Found on a sample of the table' + (f.sample_method ? `: ${f.sample_method}` : '')" in script
    assert "'Profiled on a sample' + (t.sample_method ? `: ${t.sample_method}` : '')" in script
    # the relationship checks read the same kind of sample and name it the same way
    from app.analysis.relations import sample_tag
    assert sample_tag(label, 100) == '100 rows spread over the table' and sample_tag('first 100 rows, not random', 100) == 'first 100 rows'


def test_tables_without_row_numbers_keep_their_first_rows(client, sims, recorder, monkeypatch):
    """A view has no row numbers and a table made WITHOUT ROWID has none either: the first rows, labelled as before."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    recorder.opened = []
    job = profile(client, sims)
    assert (job['state'], job['errors']) == ('done', [])
    for table, rows in (('v_sims', SIMS), ('plans', 300)):
        p = page(client, sims, table)
        assert (p['sampled'], p['sample_method'], p['rows_profiled']) == (True, 'first 100 rows, not random', 100), table
        assert (p['row_count'], p['row_count_kind'], p['notes']) == (rows, 'exact', []), table      # counted all the same
    assert stats(page(client, sims, 'plans'))['label']['distinct'] == 1               # the first rows: only "early"
    assert not any('rowid' in r and 'v_sims' in r for r in recorder.reads())           # a view is not even asked
    # a table whose own column is called rowid: the hidden row number is read under its other name
    p = page(client, sims, 'notes')
    assert p['sample_method'] == '20 stretches of 5 rows spread over the table, not random'
    assert dict(map(tuple, stats(p)['note']['top'])) == {'old': 70, 'new': 30}
    assert any('min(_rowid_)' in r for r in recorder.reads()) and any('t._rowid_ IN (SELECT x._rowid_' in r for r in recorder.reads())


def numbers_db(tmp_path, script):
    path = tmp_path / 'numbers.db'
    con = sqlite3.connect(path)
    con.executescript(script)
    con.commit()
    con.close()
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(path)))
    return db


SERIES = 'WITH RECURSIVE n (i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < {n}) '


def test_row_numbers_with_gaps_and_row_numbers_in_a_bunch(tmp_path):
    plans = [q.ColumnPlan('k', 'INTEGER', 'number'), q.ColumnPlan('v', 'INTEGER', 'number')]
    settings = {'sample_rows': 200}
    db = numbers_db(tmp_path, f'''
        CREATE TABLE gaps (k INTEGER PRIMARY KEY, v INTEGER);
        {SERIES.format(n=3000)} INSERT INTO gaps SELECT i * 10, i FROM n;
        CREATE TABLE bunch (k INTEGER PRIMARY KEY, v INTEGER);
        {SERIES.format(n=3000)} INSERT INTO bunch SELECT i, i FROM n;
        INSERT INTO bunch VALUES (1000000000000, 0);
        CREATE TABLE taken (rowid INTEGER, _rowid_ INTEGER, oid INTEGER, k INTEGER, v INTEGER);
        {SERIES.format(n=3000)} INSERT INTO taken SELECT i, i, i, i, i FROM n;''')
    # every tenth row number is used: the row numbers are cut into 20 equal parts all the same, and each gives its first 10 rows
    data = DatabaseData(db, None, 'gaps')
    plan = spread_plan(data, plans, 200, lambda: None, {'stretch_rows': 100, 'min_stretches': 20})
    assert plan == {'kind': 'spread', 'percent': None, 'seed': None, 'rowid': 'rowid', 'low': 10, 'span': 29991, 'stretches': 20,
                    'stretch_rows': 10, 'limit': 200}
    copy = read_table(data, plans, settings=settings, exact=3000)
    assert (copy.method, copy.loaded_rows, copy.row_count, copy.row_count_kind) == (
        '20 stretches of 10 rows spread over the table, not random', 200, 3000, 'exact')
    low, high = copy.run(q.minmax_query(q.table_source(q.DUCKDB, None, 'data'), ['k']))[0]
    assert low == 10 and high > 28000 and copy.notes == [] and copy.plan == plan
    # ... and the same plan gives the same rows again ("show personal values" reads the sample a second time)
    again = read_table(data, plans, settings=settings, plan=copy.plan)
    assert again.run(q.minmax_query(q.table_source(q.DUCKDB, None, 'data'), ['k', 'v'])) == copy.run(
        q.minmax_query(q.table_source(q.DUCKDB, None, 'data'), ['k', 'v'])) and again.loaded_rows == 200
    for c in (copy, again):
        c.close()
    data.close()

    # 3,000 row numbers in a bunch and one far away: 18 of the 20 parts are empty, and a part gives 10 rows at most.
    # The sample does not pass the first rows off as "spread": one more read, of the first rows, labelled as that.
    data = DatabaseData(db, None, 'bunch')
    copy = read_table(data, plans, settings=settings, exact=3001)
    assert (copy.method, copy.loaded_rows, copy.row_count, copy.row_count_kind) == ('first 200 rows, not random', 200, 3001, 'exact')
    assert copy.notes == ['A sample spread over the table gave only 11 rows: its row numbers are bunched together. '
                          'The first rows were read instead.'] and data.queries == 3      # the two ends, the sample, the first rows
    copy.close()
    data.close()

    # rowid, _rowid_ and oid are all columns of the table: there is no name left for its row numbers
    data = DatabaseData(db, None, 'taken')
    every = [q.ColumnPlan(name, 'INTEGER', 'number') for name in ('rowid', '_rowid_', 'oid', 'k', 'v')]
    assert q.rowid_name(p.name for p in every) is None and q.rowid_name(['RowID', 'x']) == '_rowid_' and q.rowid_name(['x']) == 'rowid'
    copy = read_table(data, every, settings=settings, exact=3000)
    assert (copy.method, copy.row_count_kind, data.queries) == ('first 200 rows, not random', 'exact', 1)
    copy.close()
    data.close()
    assert db.blocked == []
    db.dispose()


def test_only_a_counted_sqlite_table_is_read_spread(tmp_path):
    """The sample needs the table's rows to space its stretches. Up to the sample size the table is read whole, as
    ever; and no other engine is asked for row numbers."""
    plans = [q.ColumnPlan('k', 'INTEGER', 'number'), q.ColumnPlan('v', 'INTEGER', 'number')]
    db = numbers_db(tmp_path, f'CREATE TABLE t (k INTEGER PRIMARY KEY, v INTEGER); {SERIES.format(n=500)} INSERT INTO t SELECT i, i FROM n;')
    data = DatabaseData(db, None, 't')
    whole = read_table(data, plans, settings={'sample_rows': 500}, exact=500)
    assert (whole.sampled, whole.loaded_rows, whole.row_count_kind, whole.plan['kind'], data.queries) == (False, 500, 'exact', 'table', 1)
    whole.close()
    statement = ' '.join(str(q.spread_query(None, 't', plans, 'rowid', 1, 500, 20, 5, 100).compile(
        dialect=sqlite_dialect.dialect(), compile_kwargs={'literal_binds': True})).split())
    assert 'x.rowid >= 1 + s.i * 25 + (s.i * 0) / 20 AND x.rowid < 1 + (s.i + 1) * 25 + ((s.i + 1) * 0) / 20 ORDER BY x.rowid LIMIT 5' in statement
    assert "'" not in statement
    # a span that is not a multiple of the number of parts, and one as large as a row number gets: whole numbers throughout
    odd = ' '.join(str(q.spread_query(None, 't', plans, 'rowid', -5, 2 ** 63 - 1, 1000, 100, 100_000).compile(
        dialect=sqlite_dialect.dialect(), compile_kwargs={'literal_binds': True})).split())
    assert f'x.rowid >= -5 + s.i * {(2 ** 63 - 1) // 1000} + (s.i * {(2 ** 63 - 1) % 1000}) / 1000' in odd and '.0' not in odd
    large_column = q.spread_query(None, 't', [q.ColumnPlan('doc', 'BLOB', 'lob')], 'rowid', 1, 500, 20, 5, 100)
    assert 'CASE WHEN (t."doc" IS NOT NULL) THEN 1 ELSE NULL END AS c0' in ' '.join(str(large_column).split())     # never fetched
    data.close()
    db.dispose()
    source = (ROOT / 'app' / 'profiling' / 'profiler.py').read_text(encoding='utf-8')
    assert "dialect == 'sqlite' and not data.is_view and exact is not None and exact > target" in source


def test_the_same_spread_sample_is_read_again_for_show_personal_values(client, ws, tmp_path, recorder, monkeypatch):
    path = tmp_path / 'people.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE people (person_id INTEGER PRIMARY KEY, full_name VARCHAR(40), city VARCHAR(20));')
    con.executemany('INSERT INTO people VALUES (?, ?, ?)', [(i, f'Person Number{i:04d}', f'City {i % 3}') for i in range(1, 401)])
    con.commit()
    con.close()
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    profile(client, ws)
    masked = stats(page(client, ws, 'people'))['full_name']
    assert masked['masked'] is True and '*' in masked['max']
    client.put('/api/session', json={'show_personal': True})
    recorder.opened = []
    shown = stats(page(client, ws, 'people'))['full_name']
    assert (shown['min'], shown['max'], shown['live']) == ('Person Number0001', 'Person Number0385', True)     # rows 1-5 ... 381-385
    (again,) = [r for r in recorder.reads() if r.startswith('WITH RECURSIVE')]
    assert 't."full_name" AS c0' in again and 'city' not in again                     # the personal column only, the same stretches
    client.put('/api/session', json={'show_personal': False})


# ------------------------------------------------------------------------ row counts

def test_a_profile_never_throws_an_exact_count_away(client, sims, app, monkeypatch):
    """Problems 5 and 8. After "Exact count" the page said "102,999 rows"; after "Profile again" it said "more than
    100,000 rows" again, and the report with it. And after "Exact count" the page said "100,000 rows of 2,000,373"
    and, under it, "how many is not known".

    SQLite is counted exactly now, so "more than" is only left where a count does not finish in time: that is the
    case this test makes, to show what is kept."""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 50)
    real = service_module.exact_count

    def slow(data):
        if data.table == 'sim_cards':
            raise QueryFailed('timed out after 30 s', timeout=True)
        return real(data)

    # 1. the count does not finish: "more than 50", read on the first rows, and the page and the report say why
    monkeypatch.setattr(service_module, 'exact_count', slow)
    job = profile(client, sims)
    assert job['errors'] == [{'table': 'sim_cards', 'timeout': True,
                              'message': 'the exact count did not finish within 30 s: the table is listed with more than 50 rows'}]
    p = page(client, sims, 'sim_cards')
    assert (p['row_count'], p['row_count_kind'], p['sample_method']) == (50, 'more_than', 'first 50 rows, not random')
    assert p['notes'] == ['The table has more than 50 rows; an exact count did not finish within 30 s.']
    html = client.get(f'/api/workspaces/{sims}/report?format=html').text
    assert 'The table has more than 50 rows; an exact count did not finish within 30 s.' in html and 'of &gt;50 - first 50 rows' in html

    # 2. "Exact count": the count is there, and the note that it is not known is gone - page and report
    monkeypatch.setattr(service_module, 'exact_count', real)
    counted = client.post(f'/api/workspaces/{sims}/tables/sim_cards/count').json()
    assert (counted['row_count'], counted['row_count_kind']) == (SIMS, 'exact')
    p = page(client, sims, 'sim_cards')
    assert (p['row_count'], p['row_count_kind'], p['notes'], p['rows_profiled']) == (SIMS, 'exact', [], 50)
    html = client.get(f'/api/workspaces/{sims}/report?format=html').text
    assert 'The table has more than' not in html and 'how many is not known' not in html
    assert 'profiled on 50 rows of 600 - first 50 rows, not random' in html
    with app.state.workspaces._db() as con:                             # as if it had been counted a while ago
        con.execute("UPDATE table_stats SET counted_at = '2026-10-01T09:30:00' WHERE table_key = 'SIM_CARDS'")

    # 3. later profiles that only learn "more than" keep it, with the time it was taken
    monkeypatch.setattr(service_module, 'exact_count', slow)
    for body in ({'tables': ['sim_cards']}, {'tables': 'all'}, {'tables': 'all', 'counts_only': True}):
        wait(client, client.post(f'/api/workspaces/{sims}/profile', json=body).json())
        p = page(client, sims, 'sim_cards')
        assert (p['row_count'], p['row_count_kind'], p['counted_at'], p['notes']) == (SIMS, 'exact', '2026-10-01T09:30:00', []), body
        assert overview(client, sims)['sim_cards']['row_count'] == SIMS
    assert p['profiled_at'] > '2026-10-13' and p['sample_method'] == 'first 50 rows, not random'
    assert '<td class="num">600</td>' in client.get(f'/api/workspaces/{sims}/report?format=html').text

    # 4. a newer exact count replaces it
    monkeypatch.setattr(service_module, 'exact_count', real)
    profile(client, sims, ['sim_cards'])
    p = page(client, sims, 'sim_cards')
    assert (p['row_count'], p['row_count_kind'], p['notes']) == (SIMS, 'exact', []) and p['counted_at'] > '2026-10-13'
    assert p['sample_method'] == '25 stretches of 2 rows spread over the table, not random'


def test_what_may_replace_an_exact_count(app, ws):
    """Only a newer exact count, or the catalog's own figure - never "more than"."""
    store = app.state.profiling.store
    kinds = lambda: (store.tables(ws)['T']['row_count'], store.tables(ws)['T']['row_count_kind'])
    store.set_count(ws, 'T', 's1', 102_999, 'exact')
    counted = store.tables(ws)['T']['counted_at']
    store.save_profile(ws, 'T', 's1', {'row_count': 100_000, 'row_count_kind': 'more_than', 'sampled': True}, [])
    assert kinds() == (102_999, 'exact') and store.tables(ws)['T']['counted_at'] == counted and store.tables(ws)['T']['profiled_at']
    store.set_count(ws, 'T', 's1', 100_000, 'more_than')
    store.set_count(ws, 'T', 's1', 99_000, 'estimate')                  # "Row counts only" never puts an estimate over a count
    assert kinds() == (102_999, 'exact')
    store.save_profile(ws, 'T', 's1', {'row_count': 103_500, 'row_count_kind': 'exact'}, [])
    assert kinds() == (103_500, 'exact')                                # a newer exact count
    store.save_profile(ws, 'T', 's1', {'row_count': 104_000, 'row_count_kind': 'estimate'}, [])
    assert kinds() == (104_000, 'estimate')                             # the catalog's own figure, with a profile
    store.set_count(ws, 'T', 's1', 103_600, 'exact')
    store.save_profile(ws, 'T', 's1', {'row_count': 200_000, 'row_count_kind': 'more_than'}, [])
    assert kinds() == (200_000, 'more_than')                            # the count is too small now: the table has grown
    store.set_count(ws, 'T', 's1', 250_000, 'exact')
    store.save_profile(ws, 'T', 'another-source', {'row_count': 100_000, 'row_count_kind': 'more_than'}, [])
    assert kinds() == (100_000, 'more_than')                            # other data: the old count says nothing about it


def test_notes_saved_before_this_round_are_shown_by_the_same_rule(app, ws):
    """A profile saved before 13 Oct 2026 has "how many is not known" among its notes."""
    old = 'The table has more than 100,000 rows; how many is not known (the database has no row estimate for it). "Exact count" counts them.'
    other = 'born: its values are not all dates, so it was read as text.'
    prof, store = app.state.profiling, app.state.profiling.store
    store.save_profile(ws, 'T', 's1', {'row_count': 100_000, 'row_count_kind': 'more_than', 'notes': [old, other]}, [])
    assert prof.notes(store.tables(ws)['T']) == [old, other]
    store.set_count(ws, 'T', 's1', 2_000_373, 'exact')
    assert prof.notes(store.tables(ws)['T']) == [other]
    assert prof.notes(None) == [] and prof.notes({}) == []


def test_profile_all_counts_every_sqlite_table_first(client, sims, recorder, monkeypatch):
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 100)
    recorder.opened = []
    job = profile(client, sims)
    assert (job['state'], job['tables_done'], job['errors']) == ('done', 4, [])
    counts = [r for r in recorder.reads() if r.startswith('SELECT count(*) AS n \nFROM "')]
    assert sorted(counts) == [f'SELECT count(*) AS n \nFROM "{t}"' for t in ('notes', 'plans', 'sim_cards', 'v_sims')]
    listed = overview(client, sims)
    assert {t: (row['row_count'], row['row_count_kind']) for t, row in listed.items()} == {
        'sim_cards': (SIMS, 'exact'), 'plans': (300, 'exact'), 'notes': (300, 'exact'), 'v_sims': (SIMS, 'exact')}
    assert all(row['counted_at'] for row in listed.values())
    # an engine that has a catalog of estimates is not counted: one catalog statement a schema, as before
    source = (ROOT / 'app' / 'profiling' / 'service.py').read_text(encoding='utf-8')
    assert 'elif not has_estimates:' in source and "has_estimates = q.estimate_query(db.profile.engine, 'x') is not None" in source


def test_the_profile_list_sorts_by_name_and_by_rows():
    """Problem 6: the list was in name order and could not be sorted by rows. The page's own function, run with Node.js."""
    script = (ROOT / 'static' / 'profile.js').read_text(encoding='utf-8')
    function = re.search(r'\nfunction sortOverview\(rows, sort\) \{\n.*?\n\}\n', script, re.S)
    assert function, 'sortOverview is not in profile.js'
    assert "heading('name', 'Table', false), heading('rows', 'Rows', true)" in script and "'aria-sort': on ?" in script
    assert 'const sorted = sortOverview(rows, prof.sort);' in script and "sort: { by: 'name', dir: 1 }" in script
    assert 'dir: on ? -prof.sort.dir : (num ? -1 : 1)' in script        # a second click turns it round; rows start with the biggest
    assert 'counted ${when(' in script                                  # when an exact count was taken
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    rows = [{'id': 'USG_CDR', 'row_count': 2000373}, {'id': 'BIL_INVOICE', 'row_count': 184027}, {'id': 'LKP_CITY', 'row_count': 12},
            {'id': 'NEW_TABLE', 'row_count': None}, {'id': 'CRM_CUSTOMER', 'row_count': 150000}, {'id': 'A_SAME', 'row_count': 12},
            {'id': 'EMPTY', 'row_count': 0}]
    run = function.group(0) + ('const rows = JSON.parse(process.argv[1]);'
                               'const ids = (by, dir) => sortOverview(rows, { by, dir }).map((r) => r.id);'
                               "console.log(JSON.stringify([ids('name', 1), ids('name', -1), ids('rows', -1), ids('rows', 1)]));")
    out = subprocess.run([node, '-e', run, json.dumps(rows)], capture_output=True, text=True, timeout=60, check=True).stdout
    by_name, by_name_down, biggest, smallest = json.loads(out)
    assert by_name == ['A_SAME', 'BIL_INVOICE', 'CRM_CUSTOMER', 'EMPTY', 'LKP_CITY', 'NEW_TABLE', 'USG_CDR'] == by_name_down[::-1]
    assert biggest == ['USG_CDR', 'BIL_INVOICE', 'CRM_CUSTOMER', 'A_SAME', 'LKP_CITY', 'EMPTY', 'NEW_TABLE']
    assert smallest == ['EMPTY', 'A_SAME', 'LKP_CITY', 'CRM_CUSTOMER', 'BIL_INVOICE', 'USG_CDR', 'NEW_TABLE']      # not counted: last


# ============================================================= the demo databases ====

def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


@pytest.fixture(scope='module')
def demo(tmp_path_factory):
    """The app on the medium demo database, in a data folder of its own. The database's SHA-256 is taken before the
    first test and compared after the last: it is opened read-only, and must be the same file afterwards."""
    from app.main import create_app
    from conftest import local_client
    before = sha256(MEDIUM)
    app = create_app(tmp_path_factory.mktemp('demo') / 'data')
    with local_client(app) as client:
        ws = client.post('/api/workspaces', json={'name': 'Rimal Telecom'}).json()['id']
        added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(MEDIUM)})
        assert added.status_code == 200, added.text
        yield client, ws
    assert sha256(MEDIUM) == before


def lite(table):
    """What sqlite3 itself answers, on a read-only connection."""
    con = sqlite3.connect(MEDIUM.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return con.execute(table).fetchall()
    finally:
        con.close()


@medium
def test_demo_a_date_key_is_matched(tmp_path):
    """Problem 1, on the columns it was seen on. PRD_RATE_CARD.EFFECTIVE_FROM against PRD_PLAN_PRICE.EFFECTIVE_FROM:
    the app listed 48 orphan rows of 48. 24 of them have a parent. (The check is made directly: the two columns are
    no key of each other, and part C no longer infers a link between them.)"""
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(MEDIUM)))
    data = DatabaseData(db, None, 'PRD_RATE_CARD')
    copy = read_table(data, [q.ColumnPlan('EFFECTIVE_FROM', 'DATE', 'text')])        # a key is read as SQLite stores it
    data.close()
    conn = DatabaseData(db, None, 'check')
    parent = DatabaseParent(conn, None, 'PRD_PLAN_PRICE', {'EFFECTIVE_FROM': 'text'}, [])
    looked_up = measure(copy, ['EFFECTIVE_FROM'], parent, ['EFFECTIVE_FROM'], lambda: None)
    assert (looked_up['method'], looked_up['rows'], looked_up['orphan_rows'], looked_up['orphan_keys']) == ('lookup', 48, 24, 1)
    assert looked_up['coverage'] == 0.5 and looked_up['orphan_keys_raw'] == [(['2023-01-01'], 24)]
    whole = WholeTable(conn, (None, 'PRD_RATE_CARD'), ['EFFECTIVE_FROM'], (None, 'PRD_PLAN_PRICE'), ['EFFECTIVE_FROM'])
    counted = measure(copy, ['EFFECTIVE_FROM'], parent, ['EFFECTIVE_FROM'], lambda: None, whole=whole)
    assert (counted['method'], counted['rows'], counted['orphan_rows'], counted['orphan_keys']) == ('database', 48, 24, 1)
    assert lite('SELECT COUNT(*) FROM PRD_RATE_CARD r WHERE EXISTS (SELECT 1 FROM PRD_PLAN_PRICE p '
                'WHERE p.EFFECTIVE_FROM = r.EFFECTIVE_FROM)') == [(24,)]
    copy.close()
    conn.close()
    db.dispose()


@medium
def test_demo_check_all_gives_the_real_numbers(demo):
    """Problem 2: 310 orphan tickets (the list said 40), 420 orphan usage records (it said 4), the orders without a
    shop as empty keys - and no other declared key with an orphan."""
    client, ws = demo
    started = time.monotonic()
    job = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': 'all'}).json())
    seconds = time.monotonic() - started
    assert (job['state'], job['errors'], job['notes']) == ('done', [], [])
    view = client.get(f'/api/workspaces/{ws}/relations/check').json()
    by_key = {(r['from'], tuple(r['from_columns'])): r for r in view['relationships']}
    assert view['summary']['checked'] == len(view['relationships']) and view['summary']['not_exact'] == 0
    assert {r['check']['method'] for r in view['relationships']} == {'database'}
    tickets = by_key[('SUP_TICKET', ('SUBSCRIPTION_ID',))]['check']
    assert (tickets['orphan_rows'], tickets['rows'], tickets['null_rows'], tickets['shown']['orphans']) == (310, 55406, 0, '310')
    assert tickets['shown']['coverage'] == '99.4%' and tickets['label'] == 'measured on all 55,406 rows'
    usage = by_key[('USG_CDR', ('SUBSCRIPTION_ID',))]['check']
    assert (usage['orphan_rows'], usage['rows'], usage['shown']['orphans'], usage['shown']['coverage']) == (420, 2000373, '420', '>99.9%')
    assert len(usage['orphan_examples']) == 20 and usage['orphan_keys'] == 60
    shop = by_key[('SAL_ORDER', ('SHOP_ID',))]['check']
    assert (shop['orphan_rows'], shop['null_rows'], shop['rows'], shop['shown']['empty_keys']) == (0, 32155, 48979, '65.7%')
    declared = [r for r in view['relationships'] if r['kind'] == 'declared']
    assert len(declared) == 89
    assert sorted(r['from'] for r in declared if r['check']['orphan_rows']) == ['SUP_TICKET', 'USG_CDR']
    assert lite('SELECT COUNT(*) FROM USG_CDR u WHERE NOT EXISTS (SELECT 1 FROM SUB_SUBSCRIPTION s '
                'WHERE s.SUBSCRIPTION_ID = u.SUBSCRIPTION_ID)') == [(420,)]
    assert seconds < 60, seconds                                        # about 12 s on the PC it was written on
    # the report: the same rows, with the same texts
    html = client.get(f'/api/workspaces/{ws}/report?format=html').text
    section = html.split('<h2 id="relations">')[1].split('<h3>Links accepted')[0]
    for child, column in (('SUP_TICKET', 'SUBSCRIPTION_ID'), ('USG_CDR', 'SUBSCRIPTION_ID'), ('SAL_ORDER', 'SHOP_ID')):
        row = next(r for r in re.findall(r'<tr>(.*?)</tr>', section, re.S)
                   if f'<span class="strong">{child}</span> <span class="mono">({column})</span>' in r)
        cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        printed = by_key[(child, (column,))]['check']['shown']
        assert cells[2:5] == [printed['coverage'].replace('>', '&gt;'), printed['orphans'], printed['empty_keys']], child


@medium
def test_demo_row_counts_are_exact(demo):
    """Problem 6: ten tables stood at ">100,000". All 67 are counted now, and the counts are the tables' own."""
    client, ws = demo
    started = time.monotonic()
    job = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all', 'counts_only': True}).json())
    seconds = time.monotonic() - started
    assert (job['state'], job['tables_done'], job['errors']) == ('done', 67, [])
    listed = overview(client, ws)
    assert {row['row_count_kind'] for row in listed.values()} == {'exact'}
    big = {t: row['row_count'] for t, row in listed.items() if row['row_count'] > 100_000}
    assert len(big) == 10 and big['USG_CDR'] == 2_000_373 and big['SUB_STATUS_HISTORY'] == 102_999
    for table, rows in big.items():
        assert lite(f'SELECT COUNT(*) FROM "{table}"') == [(rows,)], table
    assert sum(row['row_count'] for row in listed.values()) == 4_487_441 and seconds < 30, seconds


@medium
def test_demo_usage_records_are_profiled_over_both_years(demo):
    """Problem 7: the profile of USG_CDR covered 13 October to 23 November 2024 of two years."""
    client, ws = demo
    started = time.monotonic()
    job = profile(client, ws, ['USG_CDR'])
    seconds = time.monotonic() - started
    assert (job['state'], job['errors']) == ('done', [])
    p = page(client, ws, 'USG_CDR')
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled'], p['notes']) == (2_000_373, 'exact', 100_000, [])
    assert p['sample_method'] == '1,000 stretches of 100 rows spread over the table, not random'
    st = stats(p)
    assert st['EVENT_START']['min'][:7] == '2024-10' and st['EVENT_START']['max'][:7] == '2026-10'
    months = st['EVENT_START']['histogram']['bins']
    assert len(months) == 25 and all(month['count'] > 1000 for month in months)        # every month of the two years
    shares = {value: n / 100_000 for value, n in st['EVENT_TYPE']['top']}
    # the whole table: DAT 57.9%, VOC 33.1%, SMS 8.7%, MMS 0.3%
    assert abs(shares['DAT'] - 0.579) < 0.02 and abs(shares['VOC'] - 0.331) < 0.02 and abs(shares['SMS'] - 0.087) < 0.01
    assert abs(st['PARTNER_ID']['nulls'] / 100_000 - 0.979) < 0.006                    # roaming is seasonal: 97.9% empty
    assert seconds < 20, seconds                                        # about 2 s, as with the first rows


@medium
def test_demo_sim_type_is_not_a_constant_column(demo):
    """Problem 7: "Constant column" for SUB_SIM_CARD.SIM_TYPE ('PHYSICAL' in the first 100,000 rows; 17,510 eSIMs)."""
    client, ws = demo
    assert profile(client, ws, ['SUB_SIM_CARD'])['errors'] == []
    st = stats(page(client, ws, 'SUB_SIM_CARD'))['SIM_TYPE']
    assert st['distinct'] == 2 and {value for value, _ in st['top']} == {'PHYSICAL', 'ESIM'}
    findings = [f for f in client.get(f'/api/workspaces/{ws}/quality').json()['findings'] if f['table'] == 'SUB_SIM_CARD']
    assert not [f for f in findings if f['column'] == 'SIM_TYPE']
    assert lite("SELECT COUNT(*) FROM SUB_SIM_CARD WHERE SIM_TYPE = 'ESIM'") == [(17510,)]


@large
def test_large_row_counts_and_the_usage_profile():
    """The large database: 16 tables stood at ">100,000". Times only, as the dry run took them."""
    from app.main import create_app
    from conftest import local_client
    import tempfile
    before = sha256(LARGE)
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(Path(folder) / 'data')
        with local_client(app) as client:
            ws = client.post('/api/workspaces', json={'name': 'Rimal Telecom (large)'}).json()['id']
            assert client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(LARGE)}).status_code == 200
            started = time.monotonic()
            job = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all', 'counts_only': True}).json())
            counted = time.monotonic() - started
            assert (job['state'], job['errors']) == ('done', [])
            listed = overview(client, ws)
            assert {row['row_count_kind'] for row in listed.values()} == {'exact'} and listed['USG_CDR']['row_count'] == 4_501_027
            assert sum(row['row_count'] for row in listed.values()) == 10_067_876
            started = time.monotonic()
            assert profile(client, ws, ['USG_CDR'])['errors'] == []
            profiled = time.monotonic() - started
            p = page(client, ws, 'USG_CDR')
            assert (p['rows_profiled'], p['row_count']) == (100_000, 4_501_027)
            assert stats(p)['EVENT_START']['min'][:7] == '2024-10' and stats(p)['EVENT_START']['max'][:7] == '2026-10'
            assert counted < 30 and profiled < 20, (counted, profiled)
    assert sha256(LARGE) == before

# ------------------------------------------------------------------- links from names

LINKS_DDL = '''
CREATE TABLE dealers (dealer_id INTEGER NOT NULL PRIMARY KEY, dealer_code VARCHAR(10) NOT NULL UNIQUE, city VARCHAR(20));
CREATE TABLE plans (plan_id INTEGER PRIMARY KEY, plan_code VARCHAR(12) NOT NULL, label VARCHAR(20));
CREATE UNIQUE INDEX ux_plans_code ON plans (plan_code);
CREATE TABLE tickets (ticket_id INTEGER PRIMARY KEY, ticket_no VARCHAR(14) NOT NULL, opened DATE,
                      CONSTRAINT uq_ticket_no UNIQUE (ticket_no));
CREATE TABLE subscriptions (sub_id INTEGER PRIMARY KEY, dealer_code VARCHAR(10), plan_code VARCHAR(12), batch_code VARCHAR(8),
                            email VARCHAR(60), label VARCHAR(20), city VARCHAR(20));
CREATE TABLE disputes (dispute_id INTEGER PRIMARY KEY, ticket_no VARCHAR(14), region_code VARCHAR(4), zone VARCHAR(4));
CREATE TABLE stage_rows (dealer_code TEXT, plan_code TEXT, note TEXT);
-- the same name is unique in TWO other tables: no telling which is meant
CREATE TABLE batches_a (a_id INTEGER PRIMARY KEY, batch_code VARCHAR(8) UNIQUE);
CREATE TABLE batches_b (b_id INTEGER PRIMARY KEY, batch_code VARCHAR(8) UNIQUE);
-- a personal column: customers and staff both have an e-mail address, and it is unique for the staff
CREATE TABLE staff (staff_id INTEGER PRIMARY KEY, email VARCHAR(60) UNIQUE);
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, email VARCHAR(60));
-- not unique by itself: a key of two columns, an index of two columns, a partial index
CREATE TABLE plan_prices (plan_id INTEGER NOT NULL REFERENCES plans (plan_id), effective_from DATE NOT NULL, fee NUMERIC(8, 2),
                          PRIMARY KEY (plan_id, effective_from));
CREATE TABLE rate_cards (rate_id INTEGER PRIMARY KEY, effective_from DATE NOT NULL, zone VARCHAR(4), UNIQUE (zone, effective_from));
CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_code VARCHAR(4), zone VARCHAR(4), label VARCHAR(20) CHECK (label <> 'UNIQUE'));
CREATE UNIQUE INDEX ux_regions_two ON regions (region_code, zone);
CREATE UNIQUE INDEX ux_regions_part ON regions (zone) WHERE region_code IS NOT NULL;
'''
INFERRED = ['disputes.ticket_no -> tickets.ticket_no', 'stage_rows.dealer_code -> dealers.dealer_code',
            'stage_rows.plan_code -> plans.plan_code', 'subscriptions.dealer_code -> dealers.dealer_code',
            'subscriptions.plan_code -> plans.plan_code']


def links_db(tmp_path):
    path = tmp_path / 'links.db'
    con = sqlite3.connect(path)
    con.executescript(LINKS_DDL)
    con.commit()
    con.close()
    return path


def inferred(client, ws):
    model = client.get(f'/api/workspaces/{ws}/model').json()
    return sorted(f'{r["from"]}.{r["from_columns"][0]} -> {r["to"]}.{r["to_columns"][0]}' for r in model['relationships']
                  if r['source'] == 'names'), model


@pytest.mark.parametrize('source', ['database', 'ddl'])
def test_a_link_is_inferred_to_a_column_that_is_unique_by_itself(client, ws, tmp_path, source):
    """Problem 9. A column that has the name of a unique column of exactly one other table points at it:
    SUB_SUBSCRIPTION.DEALER_CODE at SAL_DEALER.DEALER_CODE. And a link between two columns that are both not unique is
    never made: PRD_RATE_CARD.EFFECTIVE_FROM does not point at one column of the two-column key of PRD_PLAN_PRICE.
    The catalog of a database and the DDL in a .sql file say the same."""
    if source == 'database':
        added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(links_db(tmp_path))})
    else:
        added = client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('links.sql', LINKS_DDL.encode(), 'text/plain'))])
    assert added.status_code == 200, added.text
    found, model = inferred(client, ws)
    assert found == INFERRED
    by_pair = {(r['from'], r['from_columns'][0]): r for r in model['relationships']}
    link = by_pair[('subscriptions', 'dealer_code')]
    assert (link['source'], link['confidence'], link['cardinality']) == ('names', 0.85, '1-N')
    assert link['evidence'] == ['column name matches a unique column of this table']
    assert by_pair[('plan_prices', 'plan_id')]['source'] == 'declared'            # the declared key is still there
    # ... and what is unique by itself is told by the table's page
    unique = {t: sorted(c['name'] for c in client.get(f'/api/workspaces/{ws}/tables/{t}').json()['columns'] if c['unique'])
              for t in ('dealers', 'plans', 'tickets', 'plan_prices', 'rate_cards', 'regions', 'staff', 'subscriptions')}
    assert unique == {'dealers': ['dealer_code', 'dealer_id'], 'plans': ['plan_code', 'plan_id'], 'tickets': ['ticket_id', 'ticket_no'],
                      'plan_prices': [], 'rate_cards': ['rate_id'], 'regions': ['region_id'], 'staff': ['email', 'staff_id'],
                      'subscriptions': ['sub_id']}


def test_sqlite_tells_every_unique_column(tmp_path):
    """SQLAlchemy's reader of SQLite's CREATE TABLE text does not see "dealer_code VARCHAR(10) NOT NULL UNIQUE" (the
    brackets of the type stop it), and it leaves out the index SQLite makes for such a constraint. The demo database
    is written that way throughout: BIL_INVOICE.INVOICE_NO was no key to the value search because of it."""
    from app.analysis.relations import catalog_keys, proof
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(links_db(tmp_path))))
    model, notes = db.read_schema()
    assert notes == [] and db.blocked == []
    assert model.find('dealers').column('dealer_code').unique and model.find('dealers').column('dealer_id').unique
    assert not model.find('dealers').column('city').unique and not model.find('regions').column('zone').unique
    keys = catalog_keys(db, None)
    assert proof(keys['DEALERS'], ['dealer_code']) == 'unique index sqlite_autoindex_dealers_1'
    assert proof(keys['PLANS'], ['plan_code']) == 'unique index ux_plans_code'
    assert proof(keys['TICKETS'], ['ticket_no']) in ('unique constraint uq_ticket_no', 'unique index sqlite_autoindex_tickets_1')
    assert proof(keys['REGIONS'], ['zone']) is None and proof(keys['REGIONS'], ['region_code']) is None       # partial; two columns
    assert proof(keys['REGIONS'], ['region_code', 'zone']) == 'unique index ux_regions_two'
    assert proof(keys['PLAN_PRICES'], ['effective_from']) is None and proof(keys['PLAN_PRICES'], ['plan_id', 'effective_from'])
    assert all(s.startswith(('SELECT', 'PRAGMA')) for s in db.statements)
    db.dispose()


def test_the_parser_reads_what_makes_a_column_unique():
    from app import er
    model = er.Model()
    er.Parser(model, er.tokenize('''
        CREATE TABLE a (id INT PRIMARY KEY, code VARCHAR(10) NOT NULL UNIQUE, name VARCHAR(20) DEFAULT ('UNIQUE'),
                        kind CHAR(1) CHECK (kind IN ('U', 'N')), x INT, y INT, z INT, w INT, v INT,
                        CONSTRAINT uq_a_x UNIQUE (x), UNIQUE KEY uk_two (y, z), UNIQUE (w));
        ALTER TABLE a ADD CONSTRAINT uq_a_v UNIQUE (v);
        CREATE TABLE b (id INT, p INT, q INT, r INT, s INT);
        CREATE UNIQUE INDEX ux_b_p ON b (p);
        CREATE UNIQUE INDEX IF NOT EXISTS ux_b_qr ON b (q, r);
        CREATE UNIQUE INDEX ux_b_s ON b (s) WHERE s > 0;
        CREATE UNIQUE INDEX ux_b_low ON b (lower(q));
        CREATE INDEX ix_b_r ON b (r);
        CREATE UNIQUE INDEX ux_nowhere ON no_such_table (p);''')).run()
    er.finalize(model)
    unique = {t.name: sorted(c.name for c in t.columns.values() if c.unique) for t in model.tables}
    assert unique == {'a': ['code', 'v', 'w', 'x'], 'b': ['p']}
    assert [t.name for t in model.tables] == ['a', 'b']                  # an index on a table nobody made adds no table


@medium
def test_demo_links_inferred_from_names(demo):
    """The demo database: five links were inferred from names and are kept; the wrong one between two dates is gone;
    five are new, each to a column a UNIQUE constraint makes unique. These are 10 of the 12 "findable by column name"
    of the answer key (section 5); the other two are found by their values (below)."""
    client, ws = demo
    found, model = inferred(client, ws)
    assert found == [
        'BIL_ADJUSTMENT.APPROVED_BY_EMP_ID -> HR_EMPLOYEE.EMP_ID', 'BIL_DISPUTE.TICKET_NO -> SUP_TICKET.TICKET_NO',
        'CRM_MARKETING_CONSENT.CUSTOMER_ID -> CRM_CUSTOMER.CUST_ID', 'SAL_ORDER.PLAN_CODE -> PRD_TARIFF_PLAN.PLAN_CODE',
        'SAL_STG_DEALER_ACTIVATION.DEALER_CODE -> SAL_DEALER.DEALER_CODE',
        'SAL_STG_DEALER_ACTIVATION.PLAN_CODE -> PRD_TARIFF_PLAN.PLAN_CODE', 'SUB_SUBSCRIPTION.DEALER_CODE -> SAL_DEALER.DEALER_CODE',
        'SUP_TICKET.ASSIGNED_EMP_ID -> HR_EMPLOYEE.EMP_ID', 'USG_CDR.CELL_ID -> NET_CELL.CELL_ID',
        'USG_CDR.ROAM_ZONE_CODE -> PRD_ROAMING_ZONE.ZONE_CODE']
    assert model['stats'] == {'tables': 67, 'columns': 515, 'relationships': 99, 'declared': 89, 'joins': 0, 'values': 0, 'names': 10}
    # two unique columns that are NOT linked: a customer's e-mail is not an employee's, a complaint's service number
    # is not a fibre service's (both would be "the same name as a unique column of exactly one other table")
    assert not [r for r in model['relationships'] if 'EMAIL' in r['from_columns'] or 'SERVICE_NO' in r['from_columns']]


@medium
def test_demo_links_found_by_values(demo):
    """Problem 4: BIL_PAYMENT.REF_NO holds invoice numbers and nothing in its name says so. And none of the five
    accidents: a count and a sort order that "matched" keys which count up from 1."""
    client, ws = demo
    wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all', 'counts_only': True}).json())
    started = time.monotonic()
    job = wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'relationships': [], 'suggest': True}).json())
    seconds = time.monotonic() - started
    assert (job['state'], job['errors']) == ('done', []) and len(job['notes']) == 1
    pairs = int(job['notes'][0].split(' column pairs')[0].replace(',', ''))
    assert pairs > 6655 and 'A first look at 30 values of each column found' in job['notes'][0]       # more keys are known now
    found = {(s['from'], s['from_columns'][0], s['to'], s['to_columns'][0]): s for s in
             client.get(f'/api/workspaces/{ws}/relations/check').json()['suggestions']}
    assert set(found) == {
        ('BIL_PAYMENT', 'REF_NO', 'BIL_INVOICE', 'INVOICE_NO'),
        ('SAL_STG_DEALER_ACTIVATION', 'MSISDN_ACTIVATED', 'SUB_SUBSCRIPTION', 'MSISDN'),
        ('SUB_SUBSCRIPTION', 'SALES_CHANNEL', 'SAL_CHANNEL', 'CHANNEL_CODE'),
        ('SUP_CALL_LOG', 'SUBSCRIBER_REF', 'SUB_SUBSCRIPTION', 'SUBSCRIPTION_ID'),
        ('CRM_CUSTOMER', 'CONTACT_MSISDN', 'SUB_SUBSCRIPTION', 'MSISDN')}
    payment = found[('BIL_PAYMENT', 'REF_NO', 'BIL_INVOICE', 'INVOICE_NO')]
    assert 0.97 < payment['confidence'] < 0.995 and payment['evidence']['name_hint'] is None            # 98.4% of the rows
    assert payment['evidence']['parent_unique_proven'] is True and payment['cardinality'] == '1-N'
    assert not [pair for pair in found if pair[1] in ('ACTIVE_SUBS', 'SORT_ORDER', 'GROSS_PAY')]
    assert seconds < 60, seconds                                        # about 6 s
    # no value of a personal column is saved with a suggestion: counts and names only
    saved = json.dumps([s['evidence'] for s in found.values()])
    assert '9665' not in saved and 'INV2' not in saved


# ------------------------------------------------------------ diagrams and the graph

ONE_TO_ONE_DDL = '''
CREATE TABLE customers (cust_id INTEGER PRIMARY KEY, segment VARCHAR(10));
-- its key IS its foreign key: one row for a customer at most
CREATE TABLE business_profiles (cust_id INTEGER PRIMARY KEY REFERENCES customers (cust_id), trade_name VARCHAR(40));
CREATE TABLE orders (order_id INTEGER PRIMARY KEY, cust_id INTEGER REFERENCES customers (cust_id), total NUMERIC(10, 2));
'''


def drawio_edges(xml):
    """[(the table on the "one" side, the other table, the edge's style)] of a .drawio file."""
    import xml.etree.ElementTree as ET
    cells = {c.get('id'): c for c in ET.fromstring(xml).iter('mxCell')}

    def table(row):
        return re.sub(r'<[^>]+>', '', cells[cells[row].get('parent')].get('value')).split(' (')[0]
    return [(table(c.get('source')), table(c.get('target')), c.get('style')) for c in cells.values() if c.get('edge') == '1']


def png_texts(client, url, monkeypatch):
    """Every text the PNG renderer writes into the picture, in the order it writes them."""
    from matplotlib.axes import Axes
    written, real = [], Axes.text

    def text(self, x, y, s, *args, **kwargs):
        written.append(str(s))
        return real(self, x, y, s, *args, **kwargs)
    monkeypatch.setattr(Axes, 'text', text)
    png = client.get(url)
    monkeypatch.setattr(Axes, 'text', real)
    assert png.status_code == 200 and png.content[:8] == b'\x89PNG\r\n\x1a\n', png.text[:200]
    return written


def test_a_one_to_one_relationship_is_one_to_one_in_both_exports(client, ws, monkeypatch):
    """Problem 10. CRM_BUSINESS_PROFILE.CUST_ID -> CRM_CUSTOMER is "1-1" in the graph and in the Relations tab; the
    .drawio file ended it in a crow's foot (endArrow=ERmany) and the .png wrote "N" there."""
    added = client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('one.sql', ONE_TO_ONE_DDL.encode(), 'text/plain'))])
    assert added.status_code == 200, added.text
    model = client.get(f'/api/workspaces/{ws}/model').json()
    assert {(r['from'], r['cardinality']) for r in model['relationships']} == {('business_profiles', '1-1'), ('orders', '1-N')}

    edges = {(one, many): style for one, many, style in drawio_edges(client.get(f'/api/workspaces/{ws}/export?format=drawio').text)}
    assert set(edges) == {('customers', 'business_profiles'), ('customers', 'orders')}
    one, many = edges[('customers', 'business_profiles')], edges[('customers', 'orders')]
    assert 'startArrow=ERmandOne;' in one and 'endArrow=ERone;' in one and 'ERmany' not in one
    assert 'startArrow=ERmandOne;' in many and 'endArrow=ERmany;' in many and 'ERone;' not in many
    assert one.replace('endArrow=ERone;', 'endArrow=ERmany;') == many          # nothing else differs

    texts = png_texts(client, f'/api/workspaces/{ws}/export?format=png', monkeypatch)
    ends = [t for t in texts if t in ('1', 'N')]
    assert sorted(ends) == ['1', '1', '1', 'N']                 # 1 - 1 for the profiles, 1 - N for the orders
    assert ends in (['1', '1', '1', 'N'], ['1', 'N', '1', '1'])  # (in the order of the two lines)
    assert '1 - 1 = one-to-one' in texts[1] and '1 - N = one-to-many' in texts[1]
    # a view without a one-to-one relationship says nothing about one: the picture of before
    texts = png_texts(client, f'/api/workspaces/{ws}/export?format=png&focus=orders&hops=1', monkeypatch)
    assert [t for t in texts if t in ('1', 'N')] == ['1', 'N'] and '1 - 1' not in texts[1]


def test_a_link_accepted_from_values_keeps_its_one_to_one():
    """A link by values whose key repeats on neither side is 1-1 in the model, and so in the exports - dotted still."""
    from app.er import render_drawio
    from app.model import SchemaModel, VALUES, to_er
    model = SchemaModel()
    parent, child = model.table('sims'), model.table('sim_details')
    parent.add_column('iccid', 'db', type='VARCHAR(20)', pk=True)
    child.add_column('iccid', 'db', type='VARCHAR(20)')
    child.add_column('note', 'db', type='VARCHAR(20)')
    model.relate(child, ['iccid'], parent, ['iccid'], VALUES, 1.0, '1-1')
    em, _ = to_er(model, flags=True)
    (rel,) = em.rels.values()
    assert rel.one_to_one and rel.by_values and rel.one[0].name == 'sims'
    ((one, many, style),) = drawio_edges(render_drawio.render(em, 'x', dash_inferred=True))
    assert (one, many) == ('sims', 'sim_details') and 'endArrow=ERone;' in style and 'dashPattern=1 3' in style
    model.relationships[0].cardinality = '1-N'
    em, _ = to_er(model, flags=True)
    assert not next(iter(em.rels.values())).one_to_one


@medium
def test_demo_the_business_profile_is_one_to_one_in_the_exports(demo, monkeypatch):
    """Problem 10 on the tables it was seen on: the view of the dry run, focus on CRM_CUSTOMER."""
    client, ws = demo
    edges = drawio_edges(client.get(f'/api/workspaces/{ws}/export?format=drawio&focus=CRM_CUSTOMER&hops=1').text)
    assert len(edges) >= 8
    one_to_one = [(one, many) for one, many, style in edges if 'endArrow=ERone;' in style]
    assert one_to_one == [('CRM_CUSTOMER', 'CRM_BUSINESS_PROFILE')]
    assert all('endArrow=ERmany;' in style for one, many, style in edges if (one, many) != one_to_one[0])
    texts = png_texts(client, f'/api/workspaces/{ws}/export?format=png&focus=CRM_CUSTOMER&hops=1', monkeypatch)
    ends = [t for t in texts if t in ('1', 'N')]
    assert ends.count('N') == len(edges) - 1 and ends.count('1') == len(edges) + 1 and '1 - 1 = one-to-one' in texts[1]


def graph_style(app, selector):
    """The values of one selector of the graph's style sheet in app.js, as text."""
    block = app.split('function graphStyle()')[1].split('\n}\n')[0]
    found = re.search(r"\{ selector: '" + re.escape(selector) + r"', style: \{(.*?)\} \},", block, re.S)
    assert found, selector
    return found.group(1)


def test_a_table_that_points_at_itself_gets_its_loop_whatever_its_width():
    """Problem 12. "Edge `r22` has invalid endpoints and so it is impossible to draw": the loop of HR_DEPARTMENT
    (PARENT_DEPT_ID) was not drawn at all. Cytoscape puts the two control points of a loop 1.4 x the step size from
    the middle of the table, half the sweep on either side of the loop's direction, and takes the loop's ends from
    where the lines to them leave the box - a control point INSIDE the box has no such place. With the direction of
    before (-45 degrees) one of them was 11 px above the middle and 63 px to its left: inside every table wider than
    127 px. Both must be above the box, which is 34 px high whatever the table is called."""
    import math
    app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
    edge, node = graph_style(app, 'edge'), graph_style(app, 'node')
    height = float(re.search(r"\bheight: (\d+)", node).group(1))
    direction = float(re.search(r"'loop-direction': '(-?\d+)deg'", edge).group(1))
    sweep = float(re.search(r"'loop-sweep': '(-?\d+)deg'", edge).group(1))
    step = float(re.search(r"'control-point-step-size': (\d+)", edge).group(1))

    def control_points(direction):
        middle = math.radians(direction) - math.pi / 2
        return [(1.4 * step * math.cos(angle), 1.4 * step * math.sin(angle))
                for angle in (middle - math.radians(sweep) / 2, middle + math.radians(sweep) / 2)]
    assert height == 34
    for x, y in control_points(direction):
        assert y < -(height / 2 + 8), (x, y)                    # above the top edge, with room for the border
    # ... and the check knows the direction of before when it sees it
    inside = [(x, y) for x, y in control_points(-45) if abs(y) < height / 2]
    assert len(inside) == 1 and 126 < 2 * abs(inside[0][0]) < 128


def test_a_line_is_at_least_as_long_as_its_label():
    """Problem 11. Two tables alone in the view stood 96 px apart, one above the other, and the label of their line
    ("MSISDN_ACTIVATED -> MSISDN", written along the line) covered both. The page's own layoutOptions(), labelRoom()
    and makeRoomForLabels() are run with the bundled Cytoscape and dagre (with Node.js, when this PC has it): the gap
    between two linked tables is at least what the label of their line needs, in both directions of the hierarchy
    layout - and a gap grows by what its own lines lack, not by what a line elsewhere needs."""
    app = (ROOT / 'static' / 'app.js').read_text(encoding='utf-8')
    run = app.split('\nfunction runLayout() {')[1].split('\n}\n')[0]
    assert 'const labelled = visible.edges().length <= LABELS_UP_TO;' in run        # only where every line shows its label
    assert 'if (labelled) makeRoomForLabels(connected, edges, direction);' in run
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the layout is only looked at, not run')
    script = r"""(function () {             // (a function of its own: node -e runs a script in the global scope)
const fs = require('fs'), path = require('path');
const root = process.argv[1];
const cytoscape = require(path.join(root, 'static/vendor/cytoscape.min.js'));
cytoscape.use(require(path.join(root, 'static/vendor/cytoscape-dagre.min.js')));
const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const take = (pattern) => { const m = app.match(pattern); if (!m) throw new Error('not in app.js: ' + pattern); return m[0]; };
// the page measures a label on a canvas; here a letter is 6 px wide
globalThis.document = { createElement: () => ({ getContext: () => ({ measureText: (t) => ({ width: 6 * t.length }) }) }) };
globalThis.FONT = 'sans-serif';
(0, eval)([take(/^const LABEL_ENDS = \d+;/m), take(/^const labelRooms = new Map\(\);/m),
           take(/^function labelRoom\(label\) \{\n[\s\S]*?\n\}\n/m),
           take(/^function layoutOptions\(name, count, direction = 'LR'\) \{\n[\s\S]*?\n\}\n/m),
           take(/^function makeRoomForLabels\(nodes, edges, direction\) \{\n[\s\S]*?\n\}\n/m),
           'globalThis.page = { LABEL_ENDS, labelRoom, layoutOptions, makeRoomForLabels };'].join('\n'));
const { LABEL_ENDS, labelRoom, layoutOptions, makeRoomForLabels } = globalThis.page;

function gaps(tables, lines, direction, labelled) {
  const cy = cytoscape({ headless: true, styleEnabled: true,
    style: [{ selector: 'node', style: { shape: 'round-rectangle', width: 'data(w)', height: 34 } }],
    elements: tables.map(([id, w]) => ({ group: 'nodes', data: { id, w } }))
      .concat(lines.map(([source, target, label], i) => ({ group: 'edges', data: { id: 'r' + i, source, target, label } }))) });
  cy.layout(layoutOptions('dagre', tables.length, direction)).run();
  if (labelled) makeRoomForLabels(cy.nodes(), cy.edges(), direction);
  const boxes = cy.nodes().map((n) => [n.position('x') - n.width() / 2, n.position('y') - 17, n.position('x') + n.width() / 2, n.position('y') + 17]);
  const overlap = boxes.some((a, i) => boxes.some((b, j) => j > i && a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]));
  return { overlap, lines: cy.edges().map((e) => {
    const a = e.source(), b = e.target();
    const gap = direction === 'LR' ? Math.abs(a.position('x') - b.position('x')) - (a.width() + b.width()) / 2
      : Math.abs(a.position('y') - b.position('y')) - 34;
    return [e.data('label'), Math.round(gap), labelRoom(e.data('label'))];
  }) };
}
const input = JSON.parse(process.argv[2]);
const out = { ends: LABEL_ENDS };
for (const [name, view] of Object.entries(input)) {
  for (const direction of ['TB', 'LR']) {
    out[name + ' ' + direction] = gaps(view.tables, view.lines, direction, true);
    out[name + ' ' + direction + ' without labels'] = gaps(view.tables, view.lines, direction, false);
  }
}
console.log(JSON.stringify(out));
process.exit(0);                                    // (a headless graph with styles keeps a timer running)
})();"""
    long_label = 'A_RATHER_LONG_COLUMN_NAME → ANOTHER_LONG_COLUMN_NAME_OF_ITS_PARENT'
    views = {
        'two': {'tables': [['SUB_SUBSCRIPTION', 170], ['SAL_STG_DEALER_ACTIVATION', 250]],
                'lines': [['SUB_SUBSCRIPTION', 'SAL_STG_DEALER_ACTIVATION', 'MSISDN_ACTIVATED → MSISDN']]},
        # a -> b -> c, and a -> d beside b: three ranks. The long label is on the line from b to c only
        'chain': {'tables': [['a', 100], ['b', 120], ['c', 90], ['d', 200], ['e', 100]],
                  'lines': [['a', 'b', 'ID'], ['b', 'c', long_label], ['a', 'd', 'CUSTOMER_ID → CUST_ID'], ['a', 'c', 'CODE'],
                            ['c', 'e', 'ID']]},
    }
    done = subprocess.run([node, '-e', script, str(ROOT), json.dumps(views)], capture_output=True, text=True, timeout=120,
                          encoding='utf-8')
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    ends = out['ends']
    assert 30 <= ends <= 41                                     # the "1" or "N" at 13 px and two dashes of 10 px beside it;
                                                                # (up to 41, a label of two letters needs no more than 96 px)
    for name, view in views.items():
        for direction, before in (('TB', 96), ('LR', 130)):
            lines = out[f'{name} {direction}']['lines']
            assert len(lines) == len(view['lines']) and not out[f'{name} {direction}']['overlap']
            for label, gap, room in lines:
                assert room == 6 * len(label) + 2 * ends and gap >= room, (name, direction, label, gap, room)
            # where the labels do not show (more than 45 lines in the view), the layout is the one of before
            assert min(gap for _, gap, _ in out[f'{name} {direction} without labels']['lines']) == before
    # two tables alone: exactly the room their label needs, and not 96 px
    assert [gap for _, gap, _ in out['two TB']['lines']] == [6 * 25 + 2 * ends] == [gap for _, gap, _ in out['two LR']['lines']]
    # only the gap that the long label crosses grows for it: b -> c. The first gap grows to what ITS longest label needs
    # (a -> d), and the last one (c -> e, a short label) stays as the layout made it
    first, long, beside, across, last = (gap for _, gap, _ in out['chain TB']['lines'])
    assert long == 6 * len(long_label) + 2 * ends and first == beside == 6 * 21 + 2 * ends and last == 96
    assert across == first + 34 + long                          # a line over two ranks is as long as the two gaps it crosses
