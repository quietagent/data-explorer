"""Session 3, part A: the fixes from the session 2 audit. One group of tests per finding."""

import json
import re
import sqlite3
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook

from app import config, jobs as jobs_module
from app.jobs import Cancelled, Jobs
from app.profiling import personal, queries as q
from app.profiling import service as service_module
from app.profiling.data import DatabaseData, FileData, QueryFailed, SampleData
from app.profiling.personal import (RULES_FILE, RulesError, load_rules, mask, normalise, personal_by_name,
                                    personal_by_values, use_rules, value_kind)
from app.profiling.profiler import bounded_count, catalog_estimates, known, read_table
from app.security import sanitize_error
from app.sources import duck
from app.sources.database import ConnectionProfile, ReadOnlyDatabase
from app.sources.files import read_files
from conftest import ROOT


def wait(client, job):
    assert 'id' in job, job
    for _ in range(4000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


def profile_all(client, ws, **body):
    started = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all', **body})
    assert started.status_code == 200, started.text
    return wait(client, started.json())


def table_profile(client, ws, table, **params):
    response = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def stats_of(profile):
    return {c['name']: c['stats'] for c in profile['columns']}


def data_files(tmp_path):
    return [p for p in (tmp_path / 'data').rglob('*') if p.is_file()]


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Session 3'}).json()['id']


def sqlite_file(tmp_path, name, script, rows=None):
    path = tmp_path / name
    con = sqlite3.connect(path)
    con.executescript(script)
    for statement, values in (rows or {}).items():
        con.executemany(statement, values)
    con.commit()
    con.close()
    return path


def add_db(client, ws, path):
    added = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    assert added.status_code == 200, added.text
    return added.json()['id']


# ======================================================================== A1 ====
# Telecom personal data: more names, the rules as a data file, wider value rules, stricter masks.

TELECOM_NAMES = [
    ('a_number', 'phone'), ('b_number', 'phone'), ('anumber', 'phone'), ('bnumber', 'phone'), ('A_NUMBER', 'phone'),
    ('ANumber', 'phone'), ('BNumber', 'phone'), ('a_party', 'phone'),
    ('calling_number', 'phone'), ('called_number', 'phone'), ('caller_number', 'phone'), ('callee_number', 'phone'),
    ('other_number', 'phone'), ('dialled_number', 'phone'), ('dialed_number', 'phone'),
    ('originating_number', 'phone'), ('terminating_number', 'phone'), ('CallingNumber', 'phone'),
    ('CALLED_NO', 'phone'), ('other_party_number', 'phone'),
    ('service_number', 'phone'), ('subscriber_no', 'phone'), ('subscriber_number', 'phone'), ('contact_no', 'phone'),
    ('contact_number', 'phone'), ('line_number', 'phone'), ('SERVICE_NUMBER', 'phone'),
    ('imsi', 'subscriber_id'), ('imei', 'subscriber_id'), ('iccid', 'subscriber_id'), ('sim_serial', 'subscriber_id'),
    ('IMSI', 'subscriber_id'), ('subscriber_imsi', 'subscriber_id'), ('sim_serial_no', 'subscriber_id'),
    ('cust_nm', 'name'), ('nm', 'name'), ('fname', 'name'), ('lname', 'name'), ('CUST_NM', 'name'),
    ('mob', 'phone'), ('mob_no', 'phone'), ('MOB_NO', 'phone'), ('cust_mob', 'phone'),
]
STILL_NOT_PERSONAL = [
    ('line_number', 'invoice_lines'), ('line_number', 'billing.order_items'), ('line_number', 'log_files'),
    ('plan_nm', None), ('prod_nm', None), ('nm', 'plans'), ('calling_number_type', None), ('service_id', None),
    ('subscriber_id', None), ('number_of_lines', None), ('a', None), ('b', None), ('number', None), ('amount_b', None),
    ('other_reason', None), ('service_name', None), ('contact_type', None), ('mobility_class', None),
    ('imsi_count', None), ('normal', None), ('enumber_of_things', None),
]


@pytest.mark.parametrize('column, category', TELECOM_NAMES)
def test_a1_telecom_columns_are_flagged_by_name(column, category):
    assert personal_by_name(column, 'customers') == category


@pytest.mark.parametrize('column, table', STILL_NOT_PERSONAL)
def test_a1_names_that_must_stay_unflagged(column, table):
    assert personal_by_name(column, table) is None


def test_a1_line_number_is_a_phone_line_except_in_tables_of_lines():
    assert personal_by_name('line_number', 'subscriptions') == 'phone'
    assert personal_by_name('line_number') == 'phone'
    assert personal_by_name('line_number', 'invoice_lines') is None          # there it is the row's position


def test_a1_every_rule_lives_in_one_data_file():
    data = json.loads(RULES_FILE.read_text(encoding='utf-8'))
    assert RULES_FILE.name == 'personal_rules.json' and RULES_FILE.parent.name == 'profiling'
    assert set(data) == {'about', 'categories', 'names', 'values', 'masking'}
    assert 'msisdn' in data['names']['words']['phone'] and 'imsi' in data['names']['words']['subscriber_id']
    assert [r['category'] for r in data['values']['rules']] == ['email', 'iban', 'phone', 'national_id']
    assert data['values']['rules'][2]['share'] == 0.5 and set(data['values']['rules'][2]['patterns']) == {
        'Saudi mobile', 'Saudi landline', 'international number'}
    # no name list, no value pattern is left in the code
    code = (RULES_FILE.parent / 'personal.py').read_text(encoding='utf-8')
    for word in ('msisdn', 'iqama', 'passport', 'whatsapp', 'tariff', '9665', r'SA\d{22}'):
        assert word not in code, word


@pytest.fixture
def rules_copy(tmp_path):
    """A copy of the rules file that a test may edit; the real rules are back afterwards."""
    path = tmp_path / 'my_rules.json'
    path.write_text(RULES_FILE.read_text(encoding='utf-8'), encoding='utf-8')
    yield path
    use_rules()


def test_a1_the_team_extends_the_file_without_code(rules_copy, monkeypatch):
    assert personal_by_name('raqam_jawwal') is None and personal_by_values(['KSA-0001-22', 'KSA-0002-22', 'KSA-0003-41']) is None
    data = json.loads(rules_copy.read_text(encoding='utf-8'))
    data['names']['words']['phone'].append('jawwal')                             # a new word for an old category
    data['categories']['contract'] = 'contract number'                           # a new category with a value rule
    data['values']['rules'].append({'category': 'contract', 'share': 0.9, 'normalise': 'compact',
                                    'patterns': {'contract number': '^KSA\\d{6}$'}})
    data['values']['normalisers']['phone']['replace'].append(['^00971', '+971'])
    rules_copy.write_text(json.dumps(data), encoding='utf-8')
    use_rules(rules_copy)
    assert personal_by_name('raqam_jawwal') == 'phone'
    assert personal_by_values(['KSA-0001-22', 'KSA-0002-22', 'KSA-0003-41']) == 'contract'
    assert personal.CATEGORY_TEXT['contract'] == 'contract number'
    assert normalise('00971 50 123 4567', 'phone') == '+971501234567'
    # the app can be pointed at another copy of the file
    use_rules()
    assert personal_by_name('raqam_jawwal') is None
    monkeypatch.setenv('DATA_EXPLORER_PERSONAL_RULES', str(rules_copy))
    use_rules()
    assert personal_by_name('raqam_jawwal') == 'phone'


@pytest.mark.parametrize('edit, message', [
    (lambda d: d['names']['words']['phone'].append('two words'), 'not one word of letters and digits'),
    (lambda d: d['values']['rules'][0]['patterns'].update(bad='(unclosed'), 'bad regular expression'),
    (lambda d: d['values']['rules'][0].update(normalise='squash'), 'unknown normaliser "squash"'),
    (lambda d: d['values']['rules'][0].update(share=1.5), 'share between 0 and 1'),
    (lambda d: d['names']['words'].update(pets=['dog']), 'category "pets" has no text'),
    (lambda d: d.pop('values'), "KeyError: 'values'"),
])
def test_a1_a_wrong_edit_is_refused_with_a_reason(rules_copy, edit, message):
    data = json.loads(rules_copy.read_text(encoding='utf-8'))
    edit(data)
    rules_copy.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(RulesError) as failure:
        load_rules(rules_copy)
    assert message in str(failure.value) and 'my_rules.json' in str(failure.value)
    rules_copy.write_text('{not json', encoding='utf-8')
    with pytest.raises(RulesError, match='not valid JSON'):
        load_rules(rules_copy)
    with pytest.raises(RulesError, match='cannot be read'):
        load_rules(rules_copy.with_name('missing.json'))
    assert personal_by_name('msisdn') == 'phone'                                 # the rules in use were not touched


@pytest.mark.parametrize('written, seen', [
    ('055 123 4567', '0551234567'), ('055-123-4567', '0551234567'), ('055.123.4567', '0551234567'),
    ('(055) 123 4567', '0551234567'), ('+966 55 123 4567', '+966551234567'), ('+966 0 55 123 4567', '+966551234567'),
    ('+966 (0) 55 123 4567', '+966551234567'), ('+9660551234567', '+966551234567'),
    ('00966 55 123 4567', '+966551234567'), ('009660551234567', '+966551234567'), ('0096655-123-4567', '+966551234567'),
    ('+1 (415) 555-0100', '+14155550100'),
])
def test_a1_numbers_are_normalised_before_matching(written, seen):
    assert normalise(written, 'phone') == seen
    assert value_kind(written) in ('Saudi mobile', 'international number')


def test_a1_landlines_and_international_numbers_are_phone_numbers():
    assert personal_by_values(['0112345678', '0118765432', '0125000111']) == 'phone'            # 01x: Riyadh, Makkah
    assert personal_by_values(['011 234 5678', '(013) 876-5432', '017.500.0111', '026543210']) == 'phone'
    assert {value_kind(v) for v in ('0112345678', '+966112345678', '966138765432', '0745678901')} == {'Saudi landline'}
    assert personal_by_values(['+14155550100', '+442071234567', '0033142278186', '+971 4 123 4567']) == 'phone'
    assert {value_kind(v) for v in ('+14155550100', '0033142278186')} == {'international number'}
    assert value_kind('0812345678') is None and value_kind('0001234567') is None     # 08x; "00" then a zero
    assert value_kind('+1234567') is None and value_kind('+1234567890123456') is None   # 7 and 16 digits
    assert value_kind('112345678') is None                                      # nine digits with no 0 or 966: any number
    assert personal_by_values([112345678, 213456789, 398765432]) is None


def test_a1_a_column_is_phone_like_from_half_its_values_of_any_kind():
    mobiles, landlines, abroad = ['0551234567', '0559876543'], ['0112345678', '0138765432'], ['+14155550100']
    other = ['n/a', 'unknown', 'see notes', '-', 'none']
    assert personal_by_values(mobiles + landlines + abroad + other) == 'phone'      # 5 of 10: three kinds together
    assert personal_by_values(mobiles + landlines + other + ['x']) is None          # 4 of 10
    assert personal_by_values(mobiles + other[:2]) is None                          # half, but fewer than 3 numbers
    assert personal_by_values(['a@b.com'] * 5 + ['n/a'] * 5) is None                # the other rules still need 80%
    assert personal_by_values(['1012345678'] * 6 + ['n/a'] * 4) is None


@pytest.mark.parametrize('column, family, expected', [
    ('ref_no', 'text', 'national_id'), ('customer_ref', 'number', 'national_id'), (None, None, 'national_id'),
    ('created_at', 'date', None), ('event_time', 'date', None),                  # date and timestamp columns
    ('call_ts', 'number', None), ('START_TS', 'number', None), ('answer_time', 'number', None),
    ('setup_time', 'text', None), ('epoch', 'number', None), ('created_epoch', 'number', None),
    ('unixepoch', 'number', None), ('timestamps_seen', 'number', 'national_id'),   # the name only ends in "seen"
])
def test_a1_ten_digit_times_are_not_national_ids(column, family, expected):
    seconds = [1_700_000_000 + i * 977 for i in range(50)]                       # epoch seconds: ten digits from 1
    assert personal_by_values(seconds, column, family) == expected
    assert personal_by_values([str(v) for v in seconds], column, family) == expected


def test_a1_other_rules_still_apply_to_time_columns():
    assert personal_by_values(['0551234567', '0559876543', '0551112223'], 'call_ts', 'text') == 'phone'


@pytest.mark.parametrize('value, masked', [
    ('abcd', '****'), ('abc', '***'), ('ab', '**'), ('a', '*'), (1234, '****'), (7, '*'), ('1990', '****'),
    ('abcde', 'a***e'), ('abcdef', 'a****f'), ('0512345667', '05******67'),             # longer values as before
])
def test_a1_short_values_are_fully_masked(value, masked):
    assert mask(value) == masked


@pytest.mark.parametrize('value, masked', [
    ('17/05/1990', '**/**/****'), ('5/7/90', '*/*/**'), ('17-05-1990', '**-**-****'), ('17.05.1990', '**.**.****'),
    ('1990/05/17', '****/**/**'), ('17-MAY-1990', '**-***-****'), ('17 May 1990', '** *** ****'),
    ('17MAY1990', '*********'), ('May 17, 1990', '*** **, ****'), ('17-May-90', '**-***-**'),
    ('19900517', '********'), ('20240229', '********'), ('17/05/1990 08:30', '**/**/**** **:**'),
    ('1990-05-17T08:30:00Z', '****-**-*****:**:***'), ('1990-05-17 08:30:00.123', '****-**-** **:**'),
    ('1990-05-17', '****-**-**'), (date(1990, 5, 17), '****-**-**'),
])
def test_a1_date_like_text_is_fully_masked(value, masked):
    hidden = mask(value)
    assert hidden == masked and not re.search(r'[0-9A-Za-z]', hidden)


def test_a1_what_is_not_a_date_keeps_its_ends():
    assert mask('19901399') == '19****99' and mask('12345678') == '12****78'        # month 13: not a date
    assert mask('Ahmed Al-Harbi') == 'Ah**********bi' and mask('05/1990x') == '05****0x'


@pytest.mark.parametrize('value, masked', [
    ('1990', '****'), ('May 1990', '*** ****'), ('Rajab 1410', '***** ****'), (19900517, '********'),
    ('17th of May', '**** ** ***'), ('unknown', '*******'),
])
def test_a1_everything_in_a_birth_date_column_is_fully_masked(value, masked):
    assert mask(value, 'birth_date') == masked and not re.search(r'[0-9A-Za-z]', masked)


def test_a1_through_the_app(client, ws, tmp_path):
    rows = [(i, f'9665{50000000 + i:08d}', f'+9665{60000000 + i:08d}', f'42001{1000000000 + i:010d}', 1_700_000_000 + i * 977,
             None if i % 2 else f'011{2000000 + i:07d}', f'{1 + i % 5:02d}/0{1 + i % 3}/19{61 + i % 2}', 'KSA', f'N{i % 9}')
            for i in range(1, 61)]
    path = sqlite_file(tmp_path, 'cdr.db', '''CREATE TABLE cdr (call_id INTEGER PRIMARY KEY, a_number TEXT, b_number TEXT,
        imsi TEXT, call_ts INTEGER, contact TEXT, dob TEXT, country TEXT, cust_nm TEXT)''',
                       {'INSERT INTO cdr VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)': rows})
    add_db(client, ws, path)
    assert profile_all(client, ws)['errors'] == []
    p = table_profile(client, ws, 'cdr')
    columns = {c['name']: c for c in p['columns']}
    assert {n: c['personal'] for n, c in columns.items()} == {
        'call_id': False, 'a_number': True, 'b_number': True, 'imsi': True, 'call_ts': False, 'contact': True,
        'dob': True, 'country': False, 'cust_nm': True}
    assert columns['imsi']['personal_reason'] == 'Looks like subscriber / SIM / device identifier'
    st = stats_of(p)
    assert st['call_ts']['detected'] is None and st['call_ts']['min'] == 1_700_000_977       # seconds, shown as they are
    assert st['contact']['detected'] == 'phone' and st['contact']['nulls'] == 30              # landlines, by their values
    assert st['dob']['min'] == '**/**/****' and st['dob']['max'] == '**/**/****'              # a birth date keeps nothing
    assert all(v == '**/**/****' for v, _ in st['dob']['top'])
    assert st['cust_nm']['top'][0][0] == '**' and st['country']['top'] == [['KSA', 60]]
    first = dict(zip(p['sample']['columns'], p['sample']['rows'][0]))
    assert first['dob'] == '**/**/****' and first['cust_nm'] == '**' and first['a_number'] == '96********01'
    assert first['call_ts'] == 1_700_000_977 and first['contact'] is None
    text = json.dumps(p)
    for raw in ('966550000001', '4200110000', '0112000002', '/19', '"N1"'):
        assert raw not in text, raw
    blob = b''.join(f.read_bytes() for f in data_files(tmp_path))
    assert b'966550000001' not in blob and b'/1961' not in blob


def test_a1_the_readme_explains_the_rules_file():
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    for text in ('personal_rules.json', 'word_groups', 'unless_table_has', 'not_for_name_endings', 'DATA_EXPLORER_PERSONAL_RULES'):
        assert text in readme, text


# ======================================================================== A2 ====
# One row-count step and ONE read per table; everything else from an in-memory copy.
# (How the read is decided is tested in test_profiling.py, next to the queries.)

class Recorder(ReadOnlyDatabase):
    """Every ReadOnlyDatabase the service opens, so a test can count what was sent."""
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


PEOPLE = '''CREATE TABLE people (person_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, city TEXT, score INTEGER,
    joined DATE, photo BLOB, note TEXT);
CREATE TABLE cities (city TEXT, region TEXT);'''


def people_db(tmp_path, n=120):
    rows = [(i, f'Person Al-Sample{i:03d}', f'05{40000000 + i:08d}', ['Riyadh', 'Jeddah', 'Abha'][i % 3], i % 5,
             f'2025-{1 + i % 12:02d}-15', b'\x00binary' if i % 2 else None, f'MARK-{i:04d}') for i in range(1, n + 1)]
    return sqlite_file(tmp_path, 'people.db', PEOPLE, {
        'INSERT INTO people VALUES (?, ?, ?, ?, ?, ?, ?, ?)': rows,
        'INSERT INTO cities VALUES (?, ?)': [('Riyadh', 'Central'), ('Jeddah', 'West'), ('Abha', 'South')]})


def test_a2_a_profile_job_sends_one_read_per_table(client, ws, tmp_path, recorder):
    add_db(client, ws, people_db(tmp_path))
    recorder.opened = []
    job = profile_all(client, ws)
    assert (job['state'], job['tables_done'], job['errors']) == ('done', 2, [])
    sent = recorder.reads()
    # SQLite's catalog has no row estimates, so each table is counted first (the dry-run round): one COUNT(*) and
    # one read a table. An engine with estimates still gets one catalog statement a schema and one read a table.
    counts = [r for r in sent if r.lower().startswith('select count(*) as n')]
    reads = [r for r in sent if r not in counts]
    assert len(counts) == 2 and len(reads) == 2                                  # two tables: two counts, two reads. That is all.
    assert all('count(' not in r.lower() and 'group by' not in r.lower() and 'distinct' not in r.lower() for r in reads)
    people = next(r for r in reads if '"people"' in r)
    assert 'CASE WHEN ("photo" IS NOT NULL) THEN 1 ELSE NULL END' in people and '"photo" AS' not in people
    for table in ('people', 'cities'):
        assert table_profile(client, ws, table, sample=False)['queries'] == 1
    st = stats_of(table_profile(client, ws, 'people', sample=False))             # and the statistics are all there
    assert (st['city']['distinct'], st['score']['histogram']['kind'], st['joined']['histogram']['kind']) == (3, 'number', 'month')
    assert st['photo']['nulls'] == 60 and st['full_name']['masked'] and st['phone']['detected'] == 'phone'
    assert st['note']['unique'] is True and st['person_id']['avg'] == 60.5


def test_a2_the_copy_cannot_spill_to_disk(tmp_path, monkeypatch):
    con = duck.open_memory()
    settings = dict(con.execute("SELECT name, value FROM duckdb_settings() WHERE name IN ('temp_directory', 'memory_limit', "
                                "'autoinstall_known_extensions', 'autoload_known_extensions', 'python_enable_replacements')").fetchall())
    assert settings['temp_directory'] == '' and settings['python_enable_replacements'] == 'false'
    assert settings['autoinstall_known_extensions'] == settings['autoload_known_extensions'] == 'false'
    assert settings['memory_limit'].endswith('GiB') or settings['memory_limit'].endswith('MiB')
    con.close()
    monkeypatch.setattr(config, 'MEMORY_LIMIT_MB', 64)
    monkeypatch.chdir(tmp_path)                                                  # DuckDB's default would be ./.tmp
    plans = [q.ColumnPlan('id', 'INTEGER', 'number'), q.ColumnPlan('pad', 'TEXT', 'text')]
    copy = SampleData(plans)
    chunks = ([(i, f'{i:040d}' * 12) for i in range(start, start + 5000)] for start in range(0, 400_000, 5000))
    with pytest.raises(QueryFailed) as failure:                                  # too big for 64 MB: it fails ...
        copy.load(chunks)
    assert 'memory' in str(failure.value).lower()
    assert list(tmp_path.rglob('*')) == []                                       # ... and wrote nothing, anywhere
    assert '0000000000' not in str(failure.value)


def test_a2_python_variables_are_never_read_as_tables():
    secret_rows = [1, 2, 3]                                                      # noqa: F841 - DuckDB would find it by name
    con = duck.open_memory()
    with pytest.raises(Exception, match='does not exist'):
        con.execute('SELECT * FROM secret_rows').fetchall()
    con.close()


def test_a2_rows_are_handed_over_fast_enough(tmp_path):
    """Binding Python values one at a time took four minutes for 100,000 rows; a chunk is one JSON text."""
    plans = [q.ColumnPlan(n, t, f) for n, t, f in [('id', 'INTEGER', 'number'), ('msisdn', 'VARCHAR', 'text'),
                                                   ('amount', 'DECIMAL(10,2)', 'number'), ('at', 'TIMESTAMP', 'date')]]
    rows = [(i, f'9665{i:08d}', None if i % 3 else i * 1.5, f'2025-01-{1 + i % 28:02d} 10:00:00') for i in range(40_000)]
    started = time.monotonic()
    copy = SampleData(plans)
    stored, more = copy.load(rows[i:i + 10_000] for i in range(0, len(rows), 10_000))
    took = time.monotonic() - started
    assert (stored, more, copy.loaded_rows) == (40_000, False, 40_000) and took < 20, took
    assert dict(copy.columns) == {'id': 'HUGEINT', 'msisdn': 'VARCHAR', 'amount': 'DOUBLE', 'at': 'TIMESTAMP'}
    copy.close()


def test_a2_values_keep_their_types_in_the_copy(tmp_path):
    from datetime import datetime
    from decimal import Decimal
    plans = [q.ColumnPlan('n', 'NUMBER', 'number'), q.ColumnPlan('big', 'NUMBER(30)', 'number'),
             q.ColumnPlan('price', 'NUMBER(10,2)', 'number'), q.ColumnPlan('at', 'DATE', 'date'),
             q.ColumnPlan('ok', 'BOOLEAN', 'boolean'), q.ColumnPlan('doc', 'CLOB', 'lob'),
             q.ColumnPlan('odd', 'DATE', 'date'), q.ColumnPlan('Code', 'VARCHAR2(5)', 'text'),
             q.ColumnPlan('code', 'VARCHAR2(5)', 'text')]
    rows = [(Decimal('5'), 10 ** 25 + 1, Decimal('19.90'), datetime(2025, 3, 1, 8, 30), True, 1, '17/05/1990', '007', 'a'),
            (Decimal('7'), 3, Decimal('0.10'), datetime(2025, 3, 2), False, None, 'tomorrow', '0550000001', 'b'),
            (None, None, None, None, None, None, None, None, None)]
    copy = SampleData(plans)
    copy.load([rows])
    assert dict(copy.columns) == {'n': 'HUGEINT', 'big': 'HUGEINT', 'price': 'DOUBLE', 'at': 'TIMESTAMP', 'ok': 'BOOLEAN',
                                  'doc': 'BIGINT', 'odd': 'VARCHAR', 'Code': 'VARCHAR', 'code_2': 'VARCHAR'}
    got = copy._con.execute('SELECT * FROM "data" ORDER BY n NULLS LAST').fetchall()
    assert got[0] == (5, 10 ** 25 + 1, 19.9, datetime(2025, 3, 1, 8, 30), True, 1, '17/05/1990', '007', 'a')
    assert got[1][:2] == (7, 3) and got[1][7] == '0550000001' and got[2] == (None,) * 9
    assert [p.family for p in copy.plans][6] == 'text' and 'odd: its values are not all dates' in copy.notes[0]
    assert [p.name for p in copy.plans][-2:] == ['Code', 'code_2']             # DuckDB ignores case in names
    copy.close()


def test_a2_settings_are_configurable():
    source = (ROOT / 'app' / 'config.py').read_text(encoding='utf-8')
    assert (config.SAMPLE_TARGET_ROWS, config.TABLE_BUDGET_S, config.SAMPLE_CAP_FACTOR) == (100_000, 120, 2)
    for setting in ('DATA_EXPLORER_SAMPLE_ROWS', 'DATA_EXPLORER_TABLE_BUDGET', 'DATA_EXPLORER_MEMORY_MB'):
        assert setting in source


# ======================================================================== A3 ====
# Row estimates of 0, -1 and NULL mean "not known". No COUNT(*) just because one is missing.

def test_a3_what_counts_as_a_known_estimate():
    assert [known(v) for v in (None, 0, -1, 0.0, -1.0, float('nan'), '', 'x')] == [None] * 8
    assert [known(v) for v in (1, 1.0, 2_000_000, 1999999.6)] == [1, 1, 2_000_000, 1_999_999]


def test_a3_estimates_of_zero_and_minus_one_are_left_out(tmp_path, monkeypatch):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db(tmp_path))))
    import sqlalchemy as sa
    fake = sa.select(sa.literal_column("'a'"), sa.literal_column('0')).union_all(
        sa.select(sa.literal_column("'b'"), sa.literal_column('-1')), sa.select(sa.literal_column("'c'"), sa.null()),
        sa.select(sa.literal_column("'d'"), sa.literal_column('2500.0')))
    monkeypatch.setattr(q, 'estimate_query', lambda dialect, schema: fake)
    assert catalog_estimates(db, 'main') == {'d': 2500}
    db.dispose()


def test_a3_row_counts_only_counts_sqlite_exactly_and_never_past_the_limit_when_that_times_out(client, ws, tmp_path, recorder,
                                                                                              monkeypatch):
    """Session 3 decided that a table without a row estimate is counted no further than the sample size. That
    left every big SQLite table at ">100,000" (the dry run of 13 Oct 2026): SQLite has no estimates at all. So
    an engine without estimates counts every table, each under the statement timeout - and only a table whose
    count does not finish is counted no further than the limit, and says so. (An engine WITH a catalog still
    counts a table that has no estimate no further than the limit: tests/test_live_engines.py.)"""
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 50)
    add_db(client, ws, people_db(tmp_path))
    recorder.opened = []
    job = profile_all(client, ws, counts_only=True)
    assert (job['state'], job['tables_done'], job['errors']) == ('done', 2, [])
    overview = {t['id']: (t['row_count'], t['row_count_kind']) for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    assert overview == {'people': (120, 'exact'), 'cities': (3, 'exact')}
    assert sorted(recorder.reads()) == ['SELECT count(*) AS n \nFROM "cities"', 'SELECT count(*) AS n \nFROM "people"']
    model = {t['id']: (t['row_count'], t['row_count_kind']) for t in client.get(f'/api/workspaces/{ws}/model').json()['tables']}
    assert model['people'] == (120, 'exact')

    # the count of people does not finish in time: it is counted no further than the limit, and the job says so
    client.delete(f'/api/workspaces/{ws}/profile')
    real = service_module.exact_count

    def slow(data):
        if data.table == 'people':
            raise QueryFailed('timed out after 30 s', timeout=True)
        return real(data)
    monkeypatch.setattr(service_module, 'exact_count', slow)
    recorder.opened = []
    job = profile_all(client, ws, counts_only=True)
    assert (job['state'], job['tables_done']) == ('done', 2)
    assert job['errors'] == [{'table': 'people', 'timeout': True, 'message': 'the exact count did not finish within 30 s: the '
                              'table is listed with more than 50 rows'}]
    overview = {t['id']: (t['row_count'], t['row_count_kind']) for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    assert overview == {'people': (50, 'more_than'), 'cities': (3, 'exact')}
    bounded = [r for r in recorder.reads() if '"people"' in r]
    assert len(bounded) == 1 and bounded[0].startswith('SELECT count(*) AS n \nFROM (SELECT 1 AS x') and 'LIMIT' in bounded[0]
    monkeypatch.setattr(service_module, 'exact_count', real)
    counted = client.post(f'/api/workspaces/{ws}/tables/people/count').json()    # "Exact count": the full COUNT(*), on request
    assert (counted['row_count'], counted['row_count_kind']) == (120, 'exact')
    assert recorder.reads()[-1] == 'SELECT count(*) AS n \nFROM "people"'
    monkeypatch.setattr(service_module, 'exact_count', slow)
    profile_all(client, ws, counts_only=True)                                    # a bounded count does not undo an exact one
    after = {t['id']: (t['row_count'], t['row_count_kind']) for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    assert after['people'] == (120, 'exact')


def test_a3_bounded_count(tmp_path):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db(tmp_path))))
    data = DatabaseData(db, None, 'people')
    assert bounded_count(data, 500) == (120, False) and bounded_count(data, 120) == (120, False)
    assert bounded_count(data, 119) == (119, True) and bounded_count(data, 10) == (10, True)
    assert all('LIMIT' in s for s in db.statements if s.startswith('SELECT'))
    data.close()
    db.dispose()


# ======================================================================== A4 ====
# No raw values in error messages; CSV files read as text; nothing written while a source is added.

DUCK_CSV_ERROR = '''Conversion Error: CSV Error on Line: 30002
Original Line:
30001,Ahmed Al-Harbi,0551234567,not-a-number
Error when converting column "amount". Could not convert string "not-a-number" to 'DOUBLE'

Column amount is being converted as type DOUBLE
This type was auto-detected from the CSV file.
  file = C:/exports/payments.csv'''


def test_a4_duckdb_errors_lose_the_row_and_the_value():
    clean = sanitize_error(DUCK_CSV_ERROR, keep=['amount'])
    assert 'Ahmed' not in clean and '0551234567' not in clean and 'not-a-number' not in clean and 'Original Line' not in clean
    assert 'CSV Error on Line: 30002' in clean and 'converting column "amount"' in clean and "to 'DOUBLE'" in clean
    assert 'Could not convert string "…"' in clean and '\n' not in clean
    same_line = sanitize_error('Invalid Input Error: CSV Error on Line: 7\nOriginal Line: 7,Sara,0559876543\nExpected Number of Columns: 3')
    assert same_line == 'Invalid Input Error: CSV Error on Line: 7 Expected Number of Columns: 3'


@pytest.mark.parametrize('message, gone, kept', [
    ('ERROR: invalid input syntax for type integer: "Ahmed"\nLINE 1: SELECT 1', ['Ahmed'], ['invalid input syntax for type integer', 'LINE 1']),
    ('duplicate key value violates unique constraint "uq_mail"\nDETAIL:  Key (email)=(ahmed@example.com) already exists.',
     ['ahmed', 'example.com', 'DETAIL'], ['duplicate key value', '"uq_mail"']),
    ("Conversion failed when converting the varchar value 'Riyadh' to data type int.", ['Riyadh'], ['Conversion failed', 'data type int']),
    ("1292: Truncated incorrect DOUBLE value: '0551234567'", ['0551234567'], ['1292', 'Truncated incorrect DOUBLE value']),
    ('ORA-01722: unable to convert string value containing 0551234567 to a number\nHelp: https://docs.oracle.com/error-help/db/ora-01722/',
     ['0551234567', 'https://'], ['ORA-01722']),
    ("ORA-01722: unable to convert string value containing 'A' to a number: FULL_NAME\n"
     'ORA-03302: (ORA-01722 details) invalid string value: Ahmed Al-Harbi\nHelp: https://docs.oracle.com/error-help/db/ora-01722/',
     ['Ahmed', 'Al-Harbi', 'ORA-03302', "'A'"], ['ORA-01722: unable to convert string value containing', 'to a number: FULL_NAME']),
    ('value ahmed.harbi@example.com is not allowed here', ['ahmed', 'example.com'], ['is not allowed here']),
    ('COPY x, line 5\nCONTEXT:  COPY customers, line 5, column name: "Sara"', ['Sara', 'CONTEXT'], ['COPY x, line 5']),
    ('relation "telecom.customers" does not exist', [], ['"telecom.customers"']),
    ("Unknown column 'full_name' in 'field list'", [], ["'full_name'", "'field list'"]),
    ('connection to server at "db.internal", port 15432 failed', [], ['"db.internal"', 'port 15432']),
    ('ORA-00904: "CUSTOMER_ID": invalid identifier', [], ['"CUSTOMER_ID"']),
    ('DPY-4024: call timeout of 2000 ms exceeded', [], ['DPY-4024: call timeout of 2000 ms exceeded']),
])
def test_a4_driver_errors_lose_what_they_quote_from_the_data(message, gone, kept):
    clean = sanitize_error(message, keep=['telecom', 'customers', 'full_name', 'uq_mail', 'db.internal', 'CUSTOMER_ID'])
    for text in gone:
        assert text not in clean, clean
    for text in kept:
        assert text in clean, clean


def test_a4_a_signature_made_of_known_names_and_types_is_kept():
    message = "Binder Error: No function matches the given name and argument types 'sum(VARCHAR)'."
    assert sanitize_error(message, keep=['sum', 'amount']) == message          # the user typed SUM(amount)
    assert "'…'" in sanitize_error(message) and "'…'" in sanitize_error(message.replace('VARCHAR', 'Ahmed'), keep=['sum'])


def test_a4_names_are_only_kept_when_the_app_knows_them():
    message = 'column "full_name" of relation "Ahmed" does not exist'
    assert sanitize_error(message, keep=['full_name']) == 'column "full_name" of relation "…" does not exist'
    assert sanitize_error(message) == 'column "…" of relation "…" does not exist'
    assert len(sanitize_error('x' * 5000)) == 600 and sanitize_error('', limit=10) == ''


def test_a4_the_database_adapter_cleans_every_error(tmp_path):
    db = ReadOnlyDatabase(ConnectionProfile(engine='sqlite', database=str(people_db(tmp_path))), password='Pa55-w0rd')

    class DriverError(Exception):
        pass
    mysql_like = DriverError(1292, "Truncated incorrect DOUBLE value: 'Person Al-Sample001' for column 'full_name'")
    assert db.clean_error(mysql_like, keep=['full_name']) == "1292: Truncated incorrect DOUBLE value: '…' for column 'full_name'"
    mssql_like = DriverError(245, b"Conversion failed when converting the varchar value 'Riyadh' to data type int.DB-Lib error")
    assert 'Riyadh' not in db.clean_error(mssql_like) and db.clean_error(mssql_like).startswith('245: Conversion failed')
    assert db.clean_error(Exception('login failed with Pa55-w0rd for "reader"')) == 'login failed with *** for "…"'
    db.dispose()


def bad_csv(folder, rows=30_000):
    folder.mkdir(exist_ok=True)
    lines = ['id,code,amount,joined'] + [f'{i},{i:05d},{i}.5,2025-01-{1 + i % 28:02d}' for i in range(1, rows + 1)]
    lines += [f'{rows + 1},SECRET-CODE,not-a-number,never', f'{rows + 2},short', f'{rows + 3},x,1.5,2025-01-01,extra,cells']
    path = folder / 'payments.csv'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def test_a4_a_bad_late_row_cannot_fail_a_csv(client, ws, tmp_path):
    """Session 2 let DuckDB guess the types from the first rows: row 30,001 then failed the whole file,
    and the error quoted that row."""
    path = bad_csv(tmp_path / 'exports')
    added = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(path.parent)})
    assert added.status_code == 200 and 'SECRET-CODE' not in added.text
    assert added.json()['warnings'] == ['payments.csv: types detected from the first 20,000 rows.']
    job = profile_all(client, ws)
    assert (job['state'], job['errors']) == ('done', []) and 'SECRET-CODE' not in json.dumps(job)
    p = table_profile(client, ws, 'payments')
    assert (p['row_count'], p['row_count_kind'], p['rows_profiled']) == (30_003, 'exact', 30_003)
    st = stats_of(p)
    assert st['id']['family'] == 'number' and st['id']['max'] == 30_003
    # "amount" and "joined" are text now: the last rows are not numbers or dates, and the quality rule says so
    assert (st['amount']['family'], st['amount']['number_like'], st['amount']['nulls']) == ('text', 30_001, 1)
    assert (st['joined']['family'], st['joined']['date_like']) == ('text', 30_001)
    findings = {(f['column'], f['rule']) for f in client.get(f'/api/workspaces/{ws}/quality').json()['findings']}
    assert {('amount', 'mixed_types'), ('joined', 'mixed_types')} <= findings


def test_a4_leading_zeros_survive(client, ws, tmp_path):
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'sims.csv').write_text('sim_id,msisdn,branch_code,units,big_serial\n' + ''.join(
        f'{i},05{50000000 + i:08d},00{i},{i * 3},89966010000000000{i:03d}\n' for i in range(1, 41)), encoding='utf-8')
    source = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)}).json()
    assert source['warnings'] == []
    table = client.get(f'/api/workspaces/{ws}/tables/sims').json()
    assert {c['name']: c['type'] for c in table['columns']} == {
        'sim_id': 'BIGINT', 'msisdn': 'VARCHAR', 'branch_code': 'VARCHAR', 'units': 'BIGINT', 'big_serial': 'HUGEINT'}
    profile_all(client, ws)
    p = table_profile(client, ws, 'sims')
    st = stats_of(p)
    assert st['branch_code']['min'] == '001' and st['branch_code']['max'] == '009'             # text: '009' sorts last
    assert st['msisdn']['min'] == '05******01' and st['msisdn']['min_length'] == 10          # ten characters: the zero is there
    assert st['units']['max'] == 120 and st['big_serial']['max'] == '89966010000000000040'   # 20 digits, not 8.9e19
    first = dict(zip(p['sample']['columns'], p['sample']['rows'][0]))
    assert first['branch_code'] == '001' and first['msisdn'] == '05******01'


def test_a4_types_are_detected_from_text(tmp_path):
    folder = tmp_path / 'f'
    folder.mkdir()
    (folder / 'mix.csv').write_text(
        'whole,decimal,flag,day,stamp,stamp_at_midnight,padded,words,empty,almost\n'
        '1,1.5,true,2025-01-02,2025-01-02 10:00:00,2025-01-02 00:00:00,007,a,,1\n'
        '-20,2,FALSE,2025-12-31,2025-12-31 23:59:59,2025-01-03 00:00:00,010,b,,2\n'
        '300,1e3,True,2024-02-29,2024-02-29T08:00:00,2025-01-04 00:00:00,000,c,,x\n', encoding='utf-8')
    model, warnings = read_files([folder / 'mix.csv'])
    assert warnings == []
    assert {c.name: c.type for c in model.find('mix').columns} == {
        'whole': 'BIGINT', 'decimal': 'DOUBLE', 'flag': 'BOOLEAN', 'day': 'DATE', 'stamp': 'TIMESTAMP',
        'stamp_at_midnight': 'TIMESTAMP', 'padded': 'VARCHAR', 'words': 'VARCHAR', 'empty': 'VARCHAR', 'almost': 'VARCHAR'}


def test_a4_adding_a_files_source_writes_nothing_to_the_temp_folder(client, ws, tmp_path, monkeypatch):
    folder = tmp_path / 'exports'
    folder.mkdir()
    wb = Workbook()
    first = wb.active
    first.title = 'orders'
    first.append(['order_id', 'customer_mobile', 'total'])
    for i in range(1, 31):
        first.append([i, f'05{50000000 + i:08d}', i * 2.5])
    second = wb.create_sheet('codes')
    second.append(['code', 'label'])
    second.append(['001', 'first'])
    wb.save(folder / 'shop.xlsx')
    (folder / 'cities.csv').write_bytes('id,city\n1,الرياض\n2,جدة\n'.encode('cp1256'))
    (folder / 'plain.csv').write_text('id,v\n1,a\n2,b\n', encoding='utf-8')

    temp = tmp_path / 'temp'
    temp.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(temp))
    for name in ('mkstemp', 'mkdtemp', 'TemporaryDirectory', 'NamedTemporaryFile', 'TemporaryFile', 'SpooledTemporaryFile'):
        monkeypatch.setattr(tempfile, name, lambda *a, _name=name, **k: pytest.fail(f'tempfile.{_name} was used'))
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    added = client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    assert added.status_code == 200, added.text
    assert added.json()['tables'] == 4 and added.json()['warnings'] == ['cities.csv: not UTF-8, read as cp1256.']
    assert list(temp.iterdir()) == [] and sorted(p.name for p in tmp_path.iterdir()) == before       # no .tmp either
    assert sorted(p.name for p in folder.iterdir()) == ['cities.csv', 'plain.csv', 'shop.xlsx']
    assert [p.name for p in data_files(tmp_path)] == ['explorer.db']
    types = {c['name']: c['type'] for c in client.get(f'/api/workspaces/{ws}/tables/shop.orders').json()['columns']}
    assert types == {'order_id': 'BIGINT', 'customer_mobile': 'VARCHAR', 'total': 'DOUBLE'}
    assert 'import tempfile' not in (ROOT / 'app' / 'sources' / 'files.py').read_text(encoding='utf-8')


def test_a4_a_file_that_cannot_be_read_gives_a_clean_warning(tmp_path, monkeypatch):
    folder = tmp_path / 'f'
    folder.mkdir()
    (folder / 'good.csv').write_text('id,v\n1,a\n', encoding='utf-8')
    (folder / 'bad.csv').write_text('id,name\n1,Ahmed Al-Harbi\n', encoding='utf-8')
    real = FileData.open

    def failing(self, check=None):
        if self.path.name == 'bad.csv':
            raise QueryFailed(service_module.sanitize_error(DUCK_CSV_ERROR.replace('amount', 'name')))
        return real(self, check)
    monkeypatch.setattr(FileData, 'open', failing)
    model, warnings = read_files([folder / 'good.csv', folder / 'bad.csv'])
    assert [t.name for t in model.tables] == ['good']
    (warning,) = warnings
    assert warning.startswith('bad.csv: could not be read (') and 'Ahmed' not in warning and '0551234567' not in warning


def test_a4_a_jobs_errors_hold_no_values(client, ws, tmp_path, monkeypatch):
    add_db(client, ws, people_db(tmp_path))

    def explode(*args, **kwargs):
        raise RuntimeError('Could not convert string "Person Al-Sample001" to INT32\nOriginal Line: 1,Person Al-Sample001,0540000001')
    monkeypatch.setattr(service_module, 'profile_copy', explode)
    job = profile_all(client, ws)
    assert len(job['errors']) == 2 and job['state'] == 'done'
    text = json.dumps(job) + client.get(f'/api/workspaces/{ws}/profile').text
    assert 'Al-Sample' not in text and '0540000001' not in text and 'Original Line' not in text
    assert job['errors'][0]['message'] == 'RuntimeError: Could not convert string "…" to INT32'
    blob = b''.join(f.read_bytes() for f in data_files(tmp_path))                # the note saved with the table, too
    assert b'Al-Sample' not in blob and b'RuntimeError: Could not convert' in blob


# ======================================================================== A5 ====
# Personal marks are read again, and applied, at the moment a table's statistics are saved.

def test_a5_a_mark_made_while_the_table_is_being_profiled_is_honoured(client, app, ws, tmp_path, monkeypatch):
    add_db(client, ws, people_db(tmp_path))
    real = service_module.profile_copy
    marked = []

    def slow(copy, check=None, settings=None):
        found = real(copy, check, settings)
        if 'city' in found and not marked:                 # the statistics exist in memory; the user marks now
            marked.append(client.post(f'/api/workspaces/{ws}/personal',
                                      json={'table': 'people', 'column': 'city', 'personal': True}).status_code)
            client.post(f'/api/workspaces/{ws}/personal', json={'table': 'people', 'column': 'full_name', 'personal': False})
        return found
    monkeypatch.setattr(service_module, 'profile_copy', slow)
    assert profile_all(client, ws)['errors'] == [] and marked == [200]
    st = stats_of(table_profile(client, ws, 'people', sample=False))
    assert st['city']['masked'] is True and st['city']['top'][0][0] in ('R****h', 'J****h', '****')
    assert st['full_name']['masked'] is False and st['full_name']['min'] == 'Person Al-Sample001'    # un-marked in time, too
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    saved = dict(con.execute("SELECT column_key, stats FROM column_stats WHERE table_key = 'PEOPLE'").fetchall())
    con.close()
    assert 'Riyadh' not in saved['CITY'] and 'Jeddah' not in saved['CITY'] and '"masked": true' in saved['CITY']
    assert 'Person Al-Sample001' in saved['FULL_NAME']


def test_a5_masking_and_saving_cannot_be_split_by_a_mark(app, ws, client, tmp_path):
    """The marks are read, applied and the result saved while one lock is held; a mark waits for it."""
    add_db(client, ws, people_db(tmp_path))
    prof = app.state.profiling
    order = []
    real_save, real_set = prof.store.save_profile, prof.store.set_mark

    def save(*args, **kwargs):
        order.append('save starts')
        marker = threading.Thread(target=lambda: client.post(
            f'/api/workspaces/{ws}/personal', json={'table': 'people', 'column': 'city', 'personal': True}))
        marker.start()
        time.sleep(0.3)                                    # the mark arrives while the save is under way ...
        real_save(*args, **kwargs)
        order.append('save ends')
        save.thread = marker

    def set_mark(*args, **kwargs):
        order.append('mark')
        return real_set(*args, **kwargs)
    prof.store.save_profile, prof.store.set_mark = save, set_mark
    try:
        client.post(f'/api/workspaces/{ws}/profile', json={'tables': ['people']})
        for _ in range(500):
            if 'save ends' in order:
                break
            time.sleep(0.01)
        save.thread.join(10)
    finally:
        prof.store.save_profile, prof.store.set_mark = real_save, real_set
    assert order == ['save starts', 'save ends', 'mark']    # ... and is applied to what was saved, afterwards
    assert stats_of(table_profile(client, ws, 'people', sample=False))['city']['masked'] is True


def test_a5_a_finding_never_quotes_a_marked_column(client, app, ws, tmp_path):
    path = sqlite_file(tmp_path, 'k.db', 'CREATE TABLE staff (staff_id INTEGER PRIMARY KEY, office TEXT, grade TEXT)', {
        'INSERT INTO staff VALUES (?, ?, ?)': [(i, 'Head Office Riyadh', 'G7') for i in range(1, 31)]})
    add_db(client, ws, path)
    profile_all(client, ws)

    def constant(column):
        quality = client.get(f'/api/workspaces/{ws}/quality').json()
        return next(f['message'] for f in quality['findings'] if f['rule'] == 'constant' and f['column'] == column)
    assert constant('office') == "Every row holds the same value: 'Head Office Riyadh'."
    client.post(f'/api/workspaces/{ws}/personal', json={'table': 'staff', 'column': 'office', 'personal': True})
    assert constant('office') == 'Every row holds the same value.' and constant('grade') == "Every row holds the same value: 'G7'."
    # Even when what is stored is not masked (saved before the column counted as personal): the finding
    # asks whether the column is personal now.
    store = app.state.profiling.store
    with store._db() as con:
        row = con.execute("SELECT stats FROM column_stats WHERE table_key = 'STAFF' AND column_key = 'GRADE'").fetchone()
        stats = json.loads(row['stats'])
        assert stats['masked'] is False and stats['top'] == [['G7', 30]]
        con.execute("INSERT INTO personal_marks VALUES (?, 'STAFF', 'GRADE', 1, '2026-10-12T10:00:00')", (ws,))
    assert constant('grade') == 'Every row holds the same value.'
    assert 'G7' not in client.get(f'/api/workspaces/{ws}/quality').text


# ======================================================================== A6 ====
# Removing a source or a workspace cancels its job; nothing is saved for it afterwards; uploads go.

def test_a6_a_sealed_or_cancelled_job_cannot_save():
    jobs = Jobs()
    release = threading.Event()
    job = jobs.start('w1', 'profile', 1, lambda j: release.wait(5), sources={'s1'})
    with jobs.saving(job):
        pass                                               # a running job may save
    assert jobs.stop_for('w1', 'other-source', wait_s=0) == [] and not job.cancelled      # not its source: left alone
    stopped = jobs.stop_for('w1', 's1', wait_s=0)
    assert stopped == [job.id] and job.cancelled and job._sealed
    with pytest.raises(Cancelled):
        with jobs.saving(job):
            pytest.fail('a sealed job saved something')
    release.set()
    assert jobs.wait(job.id, 5)['state'] == 'cancelled'
    other = jobs.start('w1', 'relations', 1, lambda j: None)                 # the workspace is free again
    assert jobs.wait(other.id, 5)['state'] == 'done'
    hold = threading.Event()
    whole = jobs.start('w2', 'profile', 1, lambda j: hold.wait(5), sources={'a', 'b'})
    assert jobs.stop_for('w2', wait_s=0) == [whole.id]     # the whole workspace: whatever the job reads
    hold.set()


def slow_second_table(monkeypatch, name='people'):
    """Make profiling `name` take until the job is cancelled. Returns the list that fills when it is reached."""
    real, reached = service_module.Profiling._profile_one, []

    def slow(self, job, target, cache, estimates):
        if target.table.name == name:
            reached.append(job.id)
            while True:
                job.check()
                time.sleep(0.005)
        return real(self, job, target, cache, estimates)
    monkeypatch.setattr(service_module.Profiling, '_profile_one', slow)
    return reached


def until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_a6_removing_a_source_cancels_its_job_and_nothing_is_saved_afterwards(client, app, ws, tmp_path, monkeypatch):
    source = add_db(client, ws, people_db(tmp_path))
    reached = slow_second_table(monkeypatch)
    job = client.post(f'/api/workspaces/{ws}/profile', json={'tables': ['cities', 'people']}).json()
    assert until(lambda: reached)
    saved = app.state.profiling.store.tables(ws)                             # both are counted (SQLite); cities is profiled and saved
    assert sorted(saved) == ['CITIES', 'PEOPLE'] and [k for k, row in saved.items() if row['profiled_at']] == ['CITIES']
    removed = client.delete(f'/api/workspaces/{ws}/sources/{source}')
    assert removed.status_code == 200
    done = client.get(f'/api/jobs/{job["id"]}').json()
    assert done['state'] == 'cancelled' and done['tables_done'] == 1         # the delete waited for the job to stop
    store = app.state.profiling.store
    assert store.tables(ws) == {} and store.columns(ws) == {}                # what it had saved went with the source
    time.sleep(0.2)
    assert store.tables(ws) == {} and store.columns(ws) == {}                # and nothing arrived late
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    assert con.execute('SELECT COUNT(*) FROM table_stats').fetchone()[0] == 0
    con.close()
    assert client.get(f'/api/workspaces/{ws}/model').json()['tables'] == []


def test_a6_a_save_that_is_under_way_when_the_source_goes_is_not_left_behind(client, app, ws, tmp_path, monkeypatch):
    """The job has its statistics and is about to save when the source is removed."""
    source = add_db(client, ws, people_db(tmp_path))
    real = service_module.profile_copy
    ready, go = threading.Event(), threading.Event()

    def held(copy, check=None, settings=None):
        found = real(copy, check, settings)
        ready.set()
        go.wait(10)                                        # statistics in hand; the save is the next thing
        return found
    monkeypatch.setattr(service_module, 'profile_copy', held)
    job = client.post(f'/api/workspaces/{ws}/profile', json={'tables': ['cities']}).json()
    assert ready.wait(10)
    remover = threading.Thread(target=lambda: client.delete(f'/api/workspaces/{ws}/sources/{source}'))
    remover.start()
    assert until(lambda: app.state.jobs.find(job['id'])._sealed)
    go.set()
    remover.join(20)
    assert client.get(f'/api/jobs/{job["id"]}').json()['state'] == 'cancelled'
    assert app.state.profiling.store.tables(ws) == {}


def test_a6_deleting_a_workspace_cancels_its_job(client, app, ws, tmp_path, monkeypatch):
    add_db(client, ws, people_db(tmp_path))
    reached = slow_second_table(monkeypatch)
    job = client.post(f'/api/workspaces/{ws}/profile', json={'tables': ['cities', 'people']}).json()
    assert until(lambda: reached)
    assert client.delete(f'/api/workspaces/{ws}').status_code == 200
    assert client.get(f'/api/jobs/{job["id"]}').json()['state'] == 'cancelled'
    con = sqlite3.connect(tmp_path / 'data' / 'explorer.db')
    counts = [con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('workspaces', 'sources', 'table_stats', 'column_stats')]
    con.close()
    assert counts == [0, 0, 0, 0]
    assert client.get(f'/api/workspaces/{ws}').status_code == 404


def upload(client, ws, name='customers.csv', rows=50):
    body = 'customer_id,city\n' + ''.join(f'{i},City{i % 4}\n' for i in range(1, rows + 1))
    added = client.post(f'/api/workspaces/{ws}/sources/files', files=[('files', (name, body.encode(), 'text/csv'))])
    assert added.status_code == 200, added.text
    return added.json()['id']


def test_a6_removing_a_source_deletes_its_uploaded_copies(client, ws, tmp_path):
    first, second = upload(client, ws, 'customers.csv'), upload(client, ws, 'orders.csv')
    uploads = tmp_path / 'data' / 'uploads' / ws
    assert sorted(p.name for p in uploads.rglob('*.csv')) == ['customers.csv', 'orders.csv']
    assert client.delete(f'/api/workspaces/{ws}/sources/{first}').status_code == 200
    assert not (uploads / first).exists() and (uploads / second / 'orders.csv').is_file()
    assert client.delete(f'/api/workspaces/{ws}/sources/{second}').status_code == 200
    assert not uploads.exists()                                              # the last copy gone: the folder too
    assert [p.name for p in data_files(tmp_path)] == ['explorer.db']
    assert client.delete(f'/api/workspaces/{ws}/sources/{second}').status_code == 404


def test_a6_uploads_go_even_while_a_job_is_reading_them(client, app, ws, tmp_path, monkeypatch):
    source = upload(client, ws, 'customers.csv', rows=2000)
    folder = tmp_path / 'data' / 'uploads' / ws / source
    real = service_module.profile_copy
    ready = threading.Event()

    def held(copy, check=None, settings=None):             # the file has been read into memory; the job is still busy
        ready.set()
        while True:
            check()
            time.sleep(0.005)
    monkeypatch.setattr(service_module, 'profile_copy', held)
    job = client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json()
    assert ready.wait(10) and folder.is_dir()
    assert client.delete(f'/api/workspaces/{ws}/sources/{source}').status_code == 200
    assert not folder.exists() and client.get(f'/api/jobs/{job["id"]}').json()['state'] == 'cancelled'
    monkeypatch.setattr(service_module, 'profile_copy', real)


def test_a6_deleting_a_workspace_deletes_its_uploads(client, ws, tmp_path):
    upload(client, ws)
    assert (tmp_path / 'data' / 'uploads' / ws).is_dir()
    client.delete(f'/api/workspaces/{ws}')
    assert not (tmp_path / 'data' / 'uploads' / ws).exists()


# ======================================================================== A7 ====
# A file declares no key: the finding there is a low "no column looks like a key".

def test_a7_file_tables_get_a_low_finding_about_keys(client, ws, tmp_path):
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'visits.csv').write_text('branch,weekday,visitors\n' + ''.join(
        f'B{i % 4},{["Sun", "Mon", "Tue"][i % 3]},{i % 5}\n' for i in range(40)), encoding='utf-8')
    (folder / 'notes.csv').write_text('text,tag\n' + ''.join(f'note {i},t{i % 4}\n' for i in range(20)), encoding='utf-8')
    (folder / 'customers.csv').write_text('customer_id,city\n1,Riyadh\n2,Jeddah\n', encoding='utf-8')
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})

    def keys():
        quality = client.get(f'/api/workspaces/{ws}/quality').json()
        return [(f['severity'], f['table'], f['rule'], f['message']) for f in quality['findings']
                if f['rule'] in ('no_primary_key', 'no_key_column')]
    # before profiling only the names are known
    assert keys() == [('low', 'notes', 'no_key_column', 'No column looks like a key by its name.'),
                      ('low', 'visits', 'no_key_column', 'No column looks like a key by its name.')]
    profile_all(client, ws)
    # notes.text holds a different value in every row: it looks like a key by its values
    assert keys() == [('low', 'visits', 'no_key_column', 'No column looks like a key by its name or by its values.')]
    quality = client.get(f'/api/workspaces/{ws}/quality').json()
    assert quality['rules']['no_key_column'] == 'No column looks like a key'
    assert not any(f['rule'] == 'no_primary_key' for f in quality['findings'])


def test_a7_database_and_ddl_tables_keep_the_medium_finding(client, ws, tmp_path):
    ddl = b'CREATE TABLE visits (branch VARCHAR(5), weekday VARCHAR(3), visitors INT);'
    client.post(f'/api/workspaces/{ws}/sources/sql', files=[('files', ('s.sql', ddl, 'text/plain'))])
    folder = tmp_path / 'exports'
    folder.mkdir()
    (folder / 'visits.csv').write_text('branch,weekday,visitors\nB1,Sun,3\nB1,Sun,3\n', encoding='utf-8')
    client.post(f'/api/workspaces/{ws}/sources/files', data={'path': str(folder)})
    quality = client.get(f'/api/workspaces/{ws}/quality').json()             # the same table, declared in DDL too
    assert [(f['severity'], f['rule']) for f in quality['findings'] if 'key' in f['rule']] == [('medium', 'no_primary_key')]


# ======================================================================== A8 ====
# With "show personal values" on, a table's live values are read once and kept for the session.

def test_a8_live_values_are_read_once_per_table(client, app, ws, tmp_path, recorder):
    add_db(client, ws, people_db(tmp_path))
    profile_all(client, ws)
    prof = app.state.profiling
    recorder.opened = []
    assert table_profile(client, ws, 'people', sample=False)['columns'][1]['stats']['min'] == mask('Person Al-Sample001')
    assert recorder.reads() == []                                            # masked: straight from what is saved
    client.put('/api/session', json={'show_personal': True})

    first = table_profile(client, ws, 'people', sample=False)                # the page opens the table ...
    (read,) = recorder.reads()
    assert read == 'SELECT "full_name" AS c0, "phone" AS c1 \nFROM "people"\n LIMIT ? OFFSET ?'     # ONE read, two columns
    st = stats_of(first)
    assert (st['full_name']['min'], st['full_name']['live']) == ('Person Al-Sample001', True)
    assert st['phone']['min'] == '0540000001' and st['phone']['max'] == '0540000120' and st['city'].get('live') is None
    sample = client.get(f'/api/workspaces/{ws}/tables/people/sample').json()     # ... and then asks for its rows
    assert len(recorder.reads()) == 2 and sample['masked'] is False
    assert dict(zip(sample['columns'], sample['rows'][0]))['full_name'] == 'Person Al-Sample001'

    again = table_profile(client, ws, 'people')                              # opening it again: nothing is read
    assert len(recorder.reads()) == 2
    assert stats_of(again)['phone']['min'] == '0540000001' and again['sample'] == sample
    assert len(prof._live) == 1
    table_profile(client, ws, 'cities')                                      # a table without personal columns: its rows only
    assert len(recorder.reads()) == 3

    client.put('/api/session', json={'show_personal': False})                # switching off empties the memory
    assert prof._live == {}
    assert stats_of(table_profile(client, ws, 'people', sample=False))['phone']['min'] == '05******01'
    client.put('/api/session', json={'show_personal': True})
    table_profile(client, ws, 'people', sample=False)
    assert len(recorder.reads()) == 4                                        # on again: read again, once
    blob = (tmp_path / 'data' / 'explorer.db').read_bytes()
    assert b'Al-Sample' not in blob and b'0540000001' not in blob            # shown, never saved


def test_a8_what_is_kept_is_dropped_when_it_could_be_stale(client, app, ws, tmp_path, recorder):
    source = add_db(client, ws, people_db(tmp_path))
    profile_all(client, ws)
    prof = app.state.profiling
    client.put('/api/session', json={'show_personal': True})
    table_profile(client, ws, 'people')
    assert len(prof._live) == 1
    client.post(f'/api/workspaces/{ws}/personal', json={'table': 'people', 'column': 'city', 'personal': True})
    assert prof._live == {}                                                  # a mark changes which columns are live
    st = stats_of(table_profile(client, ws, 'people'))
    assert st['city']['live'] is True and st['city']['min'] == 'Abha'
    profile_all(client, ws, tables=['people'])
    assert prof._live == {}                                                  # a new profile
    table_profile(client, ws, 'people')
    client.post(f'/api/workspaces/{ws}/sources/{source}/refresh', json={})
    assert prof._live == {}                                                  # the source was read again
    table_profile(client, ws, 'people')
    client.delete(f'/api/workspaces/{ws}/profile')
    assert prof._live == {}                                                  # "Clear saved statistics"


def test_a8_the_same_sample_is_read_again(tmp_path):
    """The saved plan (percent, seed, cap) is what the live read uses, so the values match the statistics."""
    asked = []

    class Table:
        dialect, schema, table, is_view, queries = 'oracle', 'T', 'BIG', False, 0

        def read(self, statement, chunk_rows=None):
            from sqlalchemy.dialects import oracle
            asked.append(' '.join(str(statement.compile(dialect=oracle.dialect(), compile_kwargs={'literal_binds': True})).split()))
            self.queries += 1
            yield [('0551234567',)]

        def close(self):
            pass
    plans = [q.ColumnPlan('ID', 'NUMBER(10)', 'number'), q.ColumnPlan('MSISDN', 'VARCHAR2(12)', 'text')]
    plan = {'kind': 'percent', 'percent': 4.25, 'limit': 200_000, 'seed': 987654}
    copy = read_table(Table(), plans, estimate=2_000_000, only={'MSISDN'}, plan=plan)
    assert 'SELECT "MSISDN" AS c0 FROM "T"."BIG" SAMPLE BLOCK (4.25) SEED (987654)' in asked[0] and len(asked) == 1
    assert [p.name for p in copy.plans] == ['MSISDN'] and copy.plan == plan
    copy.close()
