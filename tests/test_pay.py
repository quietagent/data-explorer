"""Pay is personal: salary, wage, bonus (bonuses), compensation and remuneration columns are hidden completely.

Decided after the hand test on the Zephyr database (13 Oct 2026). A value of such a column keeps
nothing, not even its length: every value is shown and saved as *****. Minimum, maximum, average, top
values, histogram and sample values are hidden in the Profile tab, in the report and in the SQL tab;
counts and null % still show. "Show personal values" reveals them like the other categories, and a
report stays masked whatever the switch says.
"""

import io
import json
import sqlite3
import time
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import load_workbook

from app.analysis.sqlrunner import count_columns, derived_columns
from app.main import create_app
from app.profiling import personal
from app.profiling.personal import RULES_FILE, Rules, RulesError, mask, mask_stats, personal_by_name
from app.sqlguard import check as guard_check
from conftest import ROOT, local_client

HIDDEN = '*****'

# ------------------------------------------------------------------- by name

PAY = ['salary', 'SALARY', 'Salary', 'salaries', 'base_salary', 'BasicSalary', 'monthly_salary_sar', 'emp_salary',
       'salary_amount', 'min_salary', 'max_salary', 'wage', 'hourly_wage', 'wages', 'WAGE_SAR', 'bonus', 'annual_bonus',
       'bonus_amount', 'bonuses', 'BONUSES', 'paid_bonuses', 'YearlyBonuses', 'compensation', 'total_compensation',
       'remuneration', 'RemunerationTotal']
# Left out on purpose: in telecom an allowance is a data allowance and payment is a business table.
# ("pay" and "allowance" by themselves are pay in a table of staff or pay since 13 Oct 2026, and nowhere
# else: tests/test_rimal.py.)
NOT_PAY = ['payment', 'payment_id', 'payment_amount', 'payment_date', 'paid_on', 'data_allowance',
           'allowance_gb', 'commission', 'commission_rate', 'amount', 'budget', 'unit_price', 'supply_cost', 'credit_limit',
           'monthly_fee', 'total_amount', 'points',
           'salary_id', 'salary_type', 'bonus_flag', 'wage_status', 'bonus_count', 'bonuses_count']      # a key, or a fact about it


@pytest.mark.parametrize('column', PAY)
def test_pay_columns_are_flagged_by_name(column):
    assert personal_by_name(column, 'employee') == 'salary'
    assert personal_by_name(column) == 'salary'                 # the SQL tab has no table to go by


@pytest.mark.parametrize('column', NOT_PAY)
def test_payment_allowance_and_commission_are_not_pay(column):
    assert personal_by_name(column, 'employee') is None and personal_by_name(column) is None


def test_the_rule_is_data_in_the_rules_file():
    rules = json.loads(RULES_FILE.read_text(encoding='utf-8'))
    assert rules['categories']['salary'] == 'salary / pay'
    assert rules['names']['words']['salary'] == ['salary', 'salaries', 'wage', 'wages', 'bonus', 'bonuses', 'compensation',
                                                 'remuneration']
    assert rules['masking']['fully_masked_categories'] == ['birth_date', 'salary']
    assert rules['masking']['length_hidden_categories'] == ['salary']
    every_word = {w for words in rules['names']['words'].values() for w in words}
    assert not {'pay', 'payment', 'payments', 'allowance', 'commission'} & every_word
    assert personal.RULES.length_hidden_categories == {'salary'} and 'salary' in personal.RULES.fully_masked_categories
    assert personal.CATEGORY_TEXT['salary'] == 'salary / pay'


def before_pay():
    """The rules file as it was before pay was a category: no such category, nothing that names it."""
    old = json.loads(RULES_FILE.read_text(encoding='utf-8'))
    del old['categories']['salary'], old['names']['words']['salary'], old['masking']['length_hidden_categories']
    old['masking']['fully_masked_categories'] = ['birth_date']
    old['names']['word_groups'] = [g for g in old['names']['word_groups'] if g['category'] != 'salary']
    old['names']['word_exceptions'] = [x for x in old['names']['word_exceptions'] if x['category'] != 'salary']
    return old


def test_a_masking_category_that_does_not_exist_stops_the_app_with_a_message():
    for key in ('fully_masked_categories', 'length_hidden_categories'):
        rules = json.loads(RULES_FILE.read_text(encoding='utf-8'))
        rules['masking'][key] = ['salry']
        with pytest.raises(RulesError, match='masking names the category "salry"'):
            Rules(rules, 'edited.json')
    # a file from before this round (no such key, no such category) still loads, and hides no length
    assert Rules(before_pay(), 'old.json').length_hidden_categories == set()


# -------------------------------------------------------------------- masking

@pytest.mark.parametrize('value', [0, 5, 9000, 82000, 185000, 1234567.89, Decimal('38500.00'), Decimal('185000.50'),
                                   '82000', '82,000.00 SAR', 'SAR 9k', '-', date(2024, 1, 31), True, ''])
def test_a_pay_value_keeps_nothing_not_even_its_length(value):
    assert mask(value, 'salary') == HIDDEN


def test_pay_is_masked_harder_than_the_other_categories():
    assert mask(None, 'salary') is None                                     # empty stays empty: null % still shows
    assert mask(82000) == '8***0' and mask(185000) == '1****0'              # how any other number is masked ...
    assert mask(82000, 'phone') == '8***0' and mask(82000, 'user') == '8***0'
    assert mask(82000, 'salary') == mask(185000, 'salary') == mask(7, 'salary') == HIDDEN        # ... and pay
    # birth dates keep their shape, as before: only pay hides its length
    assert mask('12 Mar 1990', 'birth_date') == '** *** ****' and mask(date(1990, 3, 12), 'birth_date') == '****-**-**'


def test_statistics_of_a_pay_column_keep_counts_only():
    saved = {'rows': 16, 'nulls': 1, 'distinct': 15, 'unique': False, 'family': 'number', 'min': 38500, 'max': 185000,
             'avg': 74281.25, 'top': [[52000, 2], [38500, 1]], 'histogram': {'kind': 'number', 'bins': [{'from': 38500, 'to': 50000, 'count': 3}]}}
    pay = mask_stats(dict(saved), 'salary')
    assert pay == {'rows': 16, 'nulls': 1, 'distinct': 15, 'unique': False, 'family': 'number', 'min': HIDDEN, 'max': HIDDEN,
                   'top_hidden': True}
    text = mask_stats({'rows': 3, 'nulls': 0, 'distinct': 3, 'min': '9,000 SAR', 'max': 'SAR 185,000', 'min_length': 9,
                       'max_length': 11, 'top': [['9,000 SAR', 1]]}, 'salary')
    assert text == {'rows': 3, 'nulls': 0, 'distinct': 3, 'min': HIDDEN, 'max': HIDDEN, 'top_hidden': True}
    # another personal column: masked value by value, its top values and lengths stay
    phone = mask_stats({'rows': 3, 'nulls': 0, 'min': '0550000001', 'max': '0550000009', 'avg': 5.5, 'min_length': 10, 'max_length': 10,
                        'top': [['0550000001', 2]], 'histogram': {}}, 'phone')
    assert phone == {'rows': 3, 'nulls': 0, 'min': '05******01', 'max': '05******09', 'min_length': 10, 'max_length': 10,
                     'top': [['05******01', 2]]}


# ------------------------------------------------- the app: Profile, report, SQL

SALARIES = [73519.25, 91234.75, 38507.5, 185003.0, 52011.0, 52011.0, 64123.5, 47019.0, 120456.0, 58222.25,
            61077.0, 99313.5, 43561.0, 77408.0, 52011.0, 86659.75, 39874.0, 142907.0, 55731.0, 68092.5]
RAW = ['73519', '91234', '38507', '185003', '52011', '64123', '120456', '142907', '7351', '9123']      # must never be shown


def make_staff(path, rows=20):
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE department (department_id INTEGER PRIMARY KEY, name TEXT NOT NULL, budget DECIMAL(12,2));
        CREATE TABLE staff (staff_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, email TEXT, job_title TEXT,
                            salary DECIMAL(10,2), bonus REAL, hourly_wage TEXT, allowance_gb INTEGER, commission REAL,
                            payment_method TEXT, hire_date DATE, department_id INTEGER REFERENCES department (department_id));''')
    con.executemany('INSERT INTO department VALUES (?, ?, ?)', [(1, 'Sales', 1200000), (2, 'IT', 600000)])
    con.executemany('INSERT INTO staff VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', [
        (i + 1, f'Person{i:02d}', f'person{i:02d}@zephyr.example', ['Clerk', 'Analyst', 'Manager'][i % 3],
         SALARIES[i % len(SALARIES)] if i != 19 else None, 4321.5, f'{31 + i}.75 SAR/h', 10 * (1 + i % 4), 0.05 * (i % 3),
         ['bank', 'cheque'][i % 2], f'20{15 + i % 9}-0{1 + i % 9}-15', 1 + i % 2) for i in range(rows)])
    con.commit()
    con.close()
    return path


def wait(client, job):
    assert 'id' in job, job
    for _ in range(6000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


@pytest.fixture
def staff(client, tmp_path):
    """A workspace on a small database with pay columns, profiled. Returns (workspace id, source id)."""
    ws = client.post('/api/workspaces', json={'name': 'Staff'}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(make_staff(tmp_path / 'staff.db'))})
    assert source.status_code == 200, source.text
    assert wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())['errors'] == []
    return ws, source.json()['id']


def columns_of(client, ws, table='staff', sample=True):
    data = client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': str(sample).lower()}).json()
    return {c['name']: c for c in data['columns']}, data


def run(client, ws, source, sql):
    response = client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': sql})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_profile_tab_hides_everything_but_the_counts(client, app, staff, tmp_path):
    ws, _ = staff
    columns, data = columns_of(client, ws)
    for name in ('salary', 'bonus', 'hourly_wage'):
        c = columns[name]
        assert c['personal'] is True and c['personal_reason'] == 'Looks like salary / pay', name
        st = c['stats']
        assert st['masked'] is True and st['top_hidden'] is True
        assert st['min'] == st['max'] == HIDDEN
        for gone in ('avg', 'top', 'histogram', 'min_length', 'max_length'):
            assert gone not in st, (name, gone)
    # counts and null % still show
    st = columns['salary']['stats']
    assert (st['rows'], st['nulls'], st['distinct'], bool(st.get('unique'))) == (20, 1, 17, False)
    assert columns['bonus']['stats']['distinct'] == 1 and columns['hourly_wage']['stats']['nulls'] == 0
    # allowance, commission and payment are not pay: their numbers are there
    assert columns['allowance_gb']['personal'] is False and columns['allowance_gb']['stats']['avg'] == 25
    assert columns['commission']['stats']['histogram']['bins'] and columns['payment_method']['stats']['top'] == [['bank', 10], ['cheque', 10]]
    assert client.get(f'/api/workspaces/{ws}/tables/department/profile').json()['columns'][2]['stats']['max'] == 1200000        # a budget is not pay
    # the sample rows
    sample = data['sample']
    assert {'salary', 'bonus', 'hourly_wage'} <= set(sample['personal']) and sample['masked'] is True
    at = {name: i for i, name in enumerate(sample['columns'])}
    assert [row[at['salary']] for row in sample['rows']] == [HIDDEN] * 19 + [None]                      # an empty one stays empty
    assert {row[at['bonus']] for row in sample['rows']} == {HIDDEN} and {row[at['hourly_wage']] for row in sample['rows']} == {HIDDEN}
    assert [row[at['allowance_gb']] for row in sample['rows']][:4] == [10, 20, 30, 40]
    # nothing of it anywhere in what the tab is sent, nor in what is saved on disk
    sent = json.dumps(data) + json.dumps(client.get(f'/api/workspaces/{ws}/profile').json())
    stored = (tmp_path / 'data' / 'explorer.db').read_bytes()
    for raw in RAW + ['4321', '31.75', '50.75']:
        assert raw not in sent, raw
        assert raw.encode() not in stored, raw
    saved = app.state.profiling.store.columns(ws)['STAFF']['SALARY']
    assert saved['personal'] == 'salary' and saved['min'] == HIDDEN and 'avg' not in saved and 'top' not in saved


def test_quality_findings_never_quote_a_pay_value(client, staff):
    ws, _ = staff
    findings = client.get(f'/api/workspaces/{ws}/quality').json()['findings']
    constant = [f for f in findings if f['column'] == 'bonus']
    assert constant and all('4321' not in f['message'] for f in findings)
    assert all(raw not in json.dumps(findings) for raw in RAW)


def test_show_personal_values_reveals_pay_like_the_other_categories(client, staff):
    ws, source = staff
    assert client.put('/api/session', json={'show_personal': True}).json() == {'show_personal': True}
    columns, data = columns_of(client, ws)
    st = columns['salary']['stats']
    assert (st['masked'], st['live']) == (False, True) and 'top_hidden' not in st
    assert (st['min'], st['max']) == (38507.5, 185003) and st['top'][0] == [52011, 3]
    assert columns['email']['stats']['min'] == 'person00@zephyr.example'            # the same switch as for the others
    at = data['sample']['columns'].index('salary')
    assert [row[at] for row in data['sample']['rows']][:3] == [73519.25, 91234.75, 38507.5] and data['sample']['masked'] is False
    assert run(client, ws, source, 'SELECT AVG(salary) FROM staff')['rows'] == [[pytest.approx(sum(SALARIES[:19]) / 19)]]
    # off again: masked again, and the real values are gone from the answer
    client.put('/api/session', json={'show_personal': False})
    columns, data = columns_of(client, ws)
    assert columns['salary']['stats']['min'] == HIDDEN and columns['salary']['stats']['top_hidden'] is True
    assert all(raw not in json.dumps(data) for raw in RAW)


def test_a_report_hides_pay_whatever_the_switch_says(client, staff):
    ws, _ = staff
    for switch in (False, True):
        client.put('/api/session', json={'show_personal': switch})
        page = client.get(f'/api/workspaces/{ws}/report', params={'format': 'html'}).text
        book = load_workbook(io.BytesIO(client.get(f'/api/workspaces/{ws}/report', params={'format': 'xlsx'}).content))
        cells = [str(cell) for sheet in book.worksheets for row in sheet.iter_rows(values_only=True) for cell in row if cell is not None]
        assert HIDDEN in page and HIDDEN in cells
        for raw in RAW + ['4321', '31.75']:
            assert raw not in page, (switch, raw)
            assert not any(raw in cell for cell in cells), (switch, raw)
        row = next(r for sheet in book.worksheets if sheet.title == 'Columns' for r in sheet.iter_rows(values_only=True)
                   if r[0] == 'staff' and r[1] == 'salary')
        assert 'salary / pay' in row and [c for c in row if isinstance(c, str) and '*' in c] == [HIDDEN, HIDDEN]      # min and max
    client.put('/api/session', json={'show_personal': False})


# ----------------------------------------------------------------- the SQL tab

def test_in_the_sql_tab_pay_and_anything_built_from_it_is_masked(client, staff):
    ws, source = staff
    r = run(client, ws, source, 'SELECT staff_id, job_title, salary, bonus FROM staff ORDER BY staff_id')
    assert r['personal'] == ['salary', 'bonus'] and r['masked'] is True
    assert r['rows'][0] == [1, 'Clerk', HIDDEN, HIDDEN] and r['rows'][19] == [20, 'Analyst', None, HIDDEN]
    assert [c['reason'] for c in r['columns']] == [None, None, 'staff.salary: looks like salary / pay',
                                                   'staff.bonus: looks like salary / pay']

    # AVG(salary): one row, one cell, and that cell is *****
    r = run(client, ws, source, 'SELECT AVG(salary) FROM staff')
    assert r['rows'] == [[HIDDEN]] and r['personal'] == ['AVG(salary)'] and r['masked'] is True
    assert r['columns'] == [{'name': 'AVG(salary)', 'personal': True, 'reason': 'Built from staff.salary (looks like salary / pay)'}]
    r = run(client, ws, source, 'SELECT AVG(salary) AS average FROM staff')                     # under another name
    assert r['rows'] == [[HIDDEN]] and r['columns'][0] == {'name': 'average', 'personal': True,
                                                           'reason': 'Built from staff.salary (looks like salary / pay)'}

    r = run(client, ws, source, 'SELECT MIN(salary) lo, MAX(salary) AS hi, SUM(salary) AS total, SUM(salary) / COUNT(*) AS mean, '
                                'ROUND(AVG(bonus)) AS b, MAX(hourly_wage) AS w FROM staff')
    assert r['rows'] == [[HIDDEN] * 6] and len(r['personal']) == 6
    r = run(client, ws, source, 'SELECT department_id, AVG(salary) AS a, salary * 12 AS yearly, '
                                "CASE WHEN salary > 60000 THEN 'high' ELSE 'low' END AS band FROM staff GROUP BY department_id")
    assert r['rows'] == [[1, HIDDEN, HIDDEN, HIDDEN], [2, HIDDEN, HIDDEN, HIDDEN]]
    r = run(client, ws, source, 'SELECT x FROM (SELECT salary AS x FROM staff) ORDER BY 1 DESC')    # through a subquery
    assert {row[0] for row in r['rows']} == {HIDDEN, None} and r['columns'][0]['reason'] == 'staff.salary: looks like salary / pay'
    r = run(client, ws, source, 'WITH pay AS (SELECT department_id AS d, MAX(salary) AS top FROM staff GROUP BY department_id) '
                                'SELECT d, top FROM pay ORDER BY d')
    assert r['rows'] == [[1, HIDDEN], [2, HIDDEN]]
    r = run(client, ws, source, 'SELECT * FROM staff WHERE staff_id = 1')
    assert dict(zip([c['name'] for c in r['columns']], r['rows'][0]))['salary'] == HIDDEN
    # nothing of it in any of these answers, and the CSV export is what was shown
    csv = client.get(f'/api/workspaces/{ws}/sql/results/{r["id"]}.csv').content.decode('utf-8-sig')
    assert HIDDEN in csv and all(raw not in csv for raw in RAW)


def test_a_count_of_pay_stays_visible(client, staff):
    ws, source = staff
    r = run(client, ws, source, 'SELECT COUNT(*), COUNT(salary), COUNT(DISTINCT salary), COUNT(salary) AS paid, COUNT(bonus) FROM staff')
    assert r['rows'] == [[20, 19, 17, 19, 20]] and r['personal'] == [] and r['masked'] is False
    # ... also where the engine names the column after its text (SQLite, Oracle): "COUNT(email)" is not an e-mail
    r = run(client, ws, source, 'SELECT COUNT(email), COUNT(DISTINCT first_name) FROM staff')
    assert r['rows'] == [[20, 20]] and r['personal'] == []
    r = run(client, ws, source, 'SELECT department_id, COUNT(salary) FROM staff GROUP BY department_id ORDER BY 1')
    assert r['rows'] == [[1, 10], [2, 9]] and r['personal'] == []
    # a count next to the column itself: only the count is shown
    r = run(client, ws, source, 'SELECT salary, COUNT(*) AS n FROM staff GROUP BY salary ORDER BY n DESC, salary LIMIT 2')
    assert r['rows'] == [[HIDDEN, 3], [None, 1]] and r['personal'] == ['salary']


def test_a_count_does_not_unmask_what_a_union_puts_under_it(client, staff):
    """A result column is a count only when it is one in every branch of the statement."""
    ws, source = staff
    for sql in ('SELECT COUNT(*) AS n FROM staff UNION ALL SELECT salary FROM staff',
                'SELECT salary FROM staff UNION ALL SELECT COUNT(*) FROM staff',
                'SELECT COUNT(salary) FROM staff UNION ALL SELECT MAX(salary) FROM staff',
                'SELECT staff_id AS x FROM staff WHERE staff_id < 0 UNION ALL SELECT salary FROM staff',
                'SELECT allowance_gb AS x FROM staff WHERE 1 = 0 UNION ALL SELECT bonus FROM staff',
                'SELECT 1 AS x WHERE 1 = 0 UNION ALL SELECT y FROM (SELECT salary AS y FROM staff)'):
        r = run(client, ws, source, sql)
        assert len(r['columns']) == 1 and r['columns'][0]['personal'] is True, sql
        assert {row[0] for row in r['rows']} <= {HIDDEN, None}, sql
        assert all(raw not in json.dumps(r['rows']) for raw in RAW), sql
    r = run(client, ws, source, 'SELECT COUNT(salary) FROM staff UNION ALL SELECT COUNT(bonus) FROM staff')    # counts in both
    assert r['rows'] == [[19], [20]] and r['personal'] == []


def test_which_result_columns_are_counts():
    def counts(sql):
        return count_columns(guard_check(sql, 'sqlite').tokens)

    assert counts('SELECT COUNT(*), a, COUNT(DISTINCT b) AS n, COUNT(b) + 1, MAX(b) FROM t') == {0, 2}
    assert counts('SELECT COUNT(a) FROM t UNION SELECT COUNT(b) FROM u') == {0}
    assert counts('SELECT COUNT(a) FROM t UNION SELECT b FROM u') == set()
    assert counts('SELECT *, COUNT(*) FROM t') == set() and counts('SELECT t.* FROM t') == set()          # positions not known
    assert counts('WITH c AS (SELECT a FROM t) SELECT COUNT(*) FROM c') == {0}                             # the statement's own list
    assert counts('SELECT (SELECT COUNT(*) FROM u) FROM t') == set()                                       # a subquery is not a count
    assert counts('(SELECT COUNT(a) FROM t) UNION ALL (SELECT b FROM u)') == set()                         # branches in brackets
    by_name = lambda name: 'salary' if name.lower() == 'salary' else None
    outer, tainted = derived_columns(guard_check('SELECT a FROM t UNION ALL SELECT salary FROM u', 'sqlite').tokens, by_name)
    assert outer == ['salary']
    outer, _ = derived_columns(guard_check('WITH c AS (SELECT 1 AS one) SELECT salary AS s, one FROM u, c', 'sqlite').tokens, by_name)
    assert outer == ['salary', None]


# ------------------------------------------- statistics saved under older rules

@pytest.fixture
def older_rules(tmp_path):
    """The rules as they were before pay was a category; the shipped rules are put back afterwards."""
    path = tmp_path / 'older_rules.json'
    path.write_text(json.dumps(before_pay()), encoding='utf-8')
    try:
        yield path
    finally:
        personal.use_rules()


def test_statistics_saved_before_pay_was_personal_are_masked_in_the_file(tmp_path, older_rules):
    personal.use_rules(older_rules)
    data_dir = tmp_path / 'data'
    with local_client(create_app(data_dir)) as client:
        ws = client.post('/api/workspaces', json={'name': 'Old'}).json()['id']
        client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(make_staff(tmp_path / 'staff.db'))})
        wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
        before = client.get(f'/api/workspaces/{ws}/tables/staff/profile').json()['columns'][4]
        assert before['name'] == 'salary' and before['personal'] is False and before['stats']['max'] == 185003
        assert client.post(f'/api/workspaces/{ws}/personal', json={'table': 'staff', 'column': 'bonus', 'personal': False}).json()['personal'] is False
    assert b'185003' in (data_dir / 'explorer.db').read_bytes()

    personal.use_rules()                                        # the app is started again with today's rules
    app = create_app(data_dir)
    with local_client(app) as client:
        stored = lambda: app.state.profiling.store.columns(ws)['STAFF']
        assert stored()['SALARY']['max'] == 185003              # still as it was saved ...
        shown = client.get(f'/api/workspaces/{ws}/tables/staff/profile').json()['columns'][4]
        assert shown['personal'] is True and shown['stats']['max'] == HIDDEN and 'avg' not in shown['stats']    # ... but never shown
        page = client.get(f'/api/workspaces/{ws}/report', params={'format': 'html'}).text
        assert '185003' not in page and '185,003' not in page
        # opening the workspace (the page asks for its overview) masks what is stored, once
        client.get(f'/api/workspaces/{ws}/profile')
        salary, wage, bonus = stored()['SALARY'], stored()['HOURLY_WAGE'], stored()['BONUS']
        assert salary['masked'] is True and salary['personal'] == 'salary' and salary['max'] == HIDDEN
        assert 'avg' not in salary and 'histogram' not in salary and 'top' not in salary
        assert wage['max'] == HIDDEN and 'min_length' not in wage
        assert bonus['masked'] is False and bonus['max'] == 4321.5              # marked "not personal" by the user: left alone
        assert stored()['ALLOWANCE_GB']['masked'] is False
        assert app.state.profiling.mask_saved(ws) == 0                          # once per run
    raw = (data_dir / 'explorer.db').read_bytes()
    for value in ('185003', '73519', '91234'):
        assert value.encode() not in raw, value


def test_your_own_mark_still_wins(client, staff):
    ws, _ = staff
    answer = client.post(f'/api/workspaces/{ws}/personal', json={'table': 'staff', 'column': 'salary', 'personal': False}).json()
    assert answer['personal'] is False and answer['reprofile'] is True          # masking cannot be undone: profile again
    wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': ['staff']}).json())
    columns, _ = columns_of(client, ws, sample=False)
    assert columns['salary']['personal'] is False and columns['salary']['stats']['max'] == 185003
    assert columns['bonus']['stats']['max'] == HIDDEN                           # the others stay hidden
    marked = client.post(f'/api/workspaces/{ws}/personal', json={'table': 'staff', 'column': 'allowance_gb', 'personal': True}).json()
    assert marked['personal'] is True
    columns, _ = columns_of(client, ws, sample=False)
    assert columns['allowance_gb']['stats']['max'] == '**'                      # your mark: masked like any marked column


def test_the_page_and_the_readme_name_the_new_category():
    assert "st.top_hidden" in (ROOT / 'static' / 'profile.js').read_text(encoding='utf-8')
    assert 'e-mail addresses and pay are read live' in (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
    readme = (ROOT / 'README.md').read_text(encoding='utf-8')
    for word in ('salary', 'wage', 'bonus', 'bonuses', 'compensation', 'remuneration', 'length_hidden_categories', '`*****`'):
        assert word in readme, word
