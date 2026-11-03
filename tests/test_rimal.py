"""Rimal Telecom: the demo database (fictional), and the personal-data rules decided for it.

tests/fixtures/rimal/schema.sql is the schema.sql of C:\\Projects\\Demo_Database, byte for byte: 67
tables, 515 columns, 4 views. That project's answer key lists 63 columns as personal. On 13 Oct 2026
the author decided what the app does with them:

  1  pay, allowance and deduction(s) are pay in a table of staff or pay (HR_, payroll, salary,
     employee, staff ...); a period or a date is not pay
  2  compensation in a complaint, dispute, claim or ticket table is paid to a customer: not pay
  3  an account holder or a card holder is a person's name
  4  <x>_NAME in a table that holds x names the thing; a person's name stays hidden
  5  address details in an address table; latitude and longitude in a people or address table
  6  PUK codes and MAC addresses are hidden, like IMEI and ICCID
  7  eleven of the 63 are left visible on purpose, and five other columns are accepted as hidden

That makes 57 hidden columns: 55 by their names - tested here on every column of the schema - and
two (the number ranges) by their values, tested on the real data.

The database itself is not in this repository. The tests at the end read
C:\\Projects\\Demo_Database\\out\\rimal_telecom_small.db (read-only, like every source) and skip
themselves when that file is missing. RIMAL_DEMO_DIR names another folder.
"""

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from app.main import create_app
from app.profiling import personal
from app.profiling.personal import CATEGORY_TEXT, RULES_FILE, Rules, RulesError, personal_by_name
from conftest import local_client

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'rimal' / 'schema.sql'
DEMO_DIR = Path(os.environ.get('RIMAL_DEMO_DIR') or r'C:\Projects\Demo_Database')
SMALL = DEMO_DIR / 'out' / 'rimal_telecom_small.db'
real_data = pytest.mark.skipif(not SMALL.is_file(), reason=f'the demo database is not on this PC: {SMALL}')

HIDDEN = '*****'
HIDDEN_BY_NAME = {
    'CRM_CUSTOMER.FIRST_NAME': 'name', 'CRM_CUSTOMER.FATHER_NAME': 'name', 'CRM_CUSTOMER.LAST_NAME': 'name',
    'CRM_CUSTOMER.FULL_NAME_AR': 'name', 'CRM_BANK_MANDATE.HOLDER_NAME': 'name', 'HR_BANK_ACCOUNT.ACCOUNT_HOLDER': 'name',
    'HR_EMPLOYEE.FIRST_NAME': 'name', 'HR_EMPLOYEE.LAST_NAME': 'name', 'SAL_DEALER.CONTACT_NAME': 'name',
    'SAL_STG_DEALER_ACTIVATION.CUSTOMER_NAME': 'name',
    'CRM_CUSTOMER.ID_NUMBER': 'national ID / Iqama', 'HR_EMPLOYEE.NATIONAL_ID': 'national ID / Iqama',
    'SAL_STG_DEALER_ACTIVATION.ID_NUMBER': 'national ID / Iqama',
    'CRM_CUSTOMER.BIRTH_DATE': 'birth date', 'HR_EMPLOYEE.BIRTH_DATE': 'birth date',
    'CRM_CUSTOMER.EMAIL': 'e-mail', 'HR_EMPLOYEE.EMAIL': 'e-mail',
    'CRM_CUSTOMER.CONTACT_MSISDN': 'phone number', 'CRM_CUSTOMER.ALT_CONTACT_NO': 'phone number',
    'CRM_CUSTOMER.FAX_NO': 'phone number', 'HR_EMPLOYEE.MOBILE_NO': 'phone number', 'SAL_DEALER.CONTACT_MSISDN': 'phone number',
    'SAL_STG_DEALER_ACTIVATION.CONTACT_PHONE': 'phone number', 'SAL_STG_DEALER_ACTIVATION.MSISDN_ACTIVATED': 'phone number',
    'SUB_SUBSCRIPTION.MSISDN': 'phone number', 'SUP_CALL_LOG.CALLER_NO': 'phone number', 'SUP_COMPLAINT.SERVICE_NO': 'phone number',
    'USG_CDR.OTHER_PARTY': 'phone number', 'SUB_FIBRE_SERVICE.SERVICE_NO': 'phone number',
    'CRM_ADDRESS.STREET_NAME': 'address', 'CRM_ADDRESS.BUILDING_NO': 'address', 'CRM_ADDRESS.UNIT_NO': 'address',
    'CRM_ADDRESS.ADDITIONAL_NO': 'address', 'CRM_ADDRESS.POSTAL_CODE': 'address', 'CRM_ADDRESS.LATITUDE': 'address',
    'CRM_ADDRESS.LONGITUDE': 'address',
    'CRM_BANK_MANDATE.IBAN': 'IBAN', 'HR_BANK_ACCOUNT.IBAN': 'IBAN', 'SAL_DEALER.IBAN': 'IBAN',
    'CRM_BANK_MANDATE.CARD_NUMBER': 'card number', 'CRM_BANK_MANDATE.CARD_EXPIRY': 'card number',
    'SUB_SIM_CARD.ICCID': 'subscriber / SIM / device identifier', 'SUB_SIM_CARD.IMSI': 'subscriber / SIM / device identifier',
    'SUB_SIM_CARD.PUK_CODE': 'subscriber / SIM / device identifier', 'SUB_DEVICE.IMEI': 'subscriber / SIM / device identifier',
    'SUB_FIBRE_SERVICE.ROUTER_MAC': 'subscriber / SIM / device identifier',
    'HR_SALARY_HISTORY.BASIC_SALARY': 'salary / pay', 'HR_SALARY_HISTORY.HOUSING_ALLOWANCE': 'salary / pay',
    'HR_SALARY_HISTORY.TRANSPORT_ALLOWANCE': 'salary / pay', 'HR_PAYROLL_RUN.GROSS_PAY': 'salary / pay',
    'HR_PAYROLL_RUN.SOCIAL_INS_DEDUCTION': 'salary / pay', 'HR_PAYROLL_RUN.OTHER_DEDUCTIONS': 'salary / pay',
    'HR_PAYROLL_RUN.NET_PAY': 'salary / pay', 'HR_PAY_GRADE.MIN_SALARY': 'salary / pay', 'HR_PAY_GRADE.MAX_SALARY': 'salary / pay',
}
# no name says it: the real values do (966 5x xxx xxxx)
HIDDEN_BY_VALUES = {'SUB_NUMBER_RANGE.RANGE_START': 'phone number', 'SUB_NUMBER_RANGE.RANGE_END': 'phone number'}
# point 7: in the answer key, and left visible on purpose
LEFT_VISIBLE = {'SUP_TICKET_NOTE.NOTE_TEXT', 'SUP_SURVEY.COMMENT_TEXT', 'CRM_CUSTOMER.GENDER', 'CRM_CUSTOMER.NATIONALITY',
                'HR_EMPLOYEE.GENDER', 'HR_EMPLOYEE.NATIONALITY', 'CRM_BUSINESS_PROFILE.CR_NUMBER', 'CRM_BUSINESS_PROFILE.VAT_NUMBER',
                'USG_CDR.CELL_ID', 'SUB_FIBRE_SERVICE.ONT_SERIAL', 'CRM_ADDRESS.DISTRICT'}
# point 7: not in the answer key, and accepted as hidden (the safe side)
ACCEPTED = {'HR_PAY_GRADE.MIN_SALARY', 'HR_PAY_GRADE.MAX_SALARY', 'SUB_FIBRE_SERVICE.SERVICE_NO', 'SUB_NUMBER_RANGE.RANGE_START',
            'SUB_NUMBER_RANGE.RANGE_END'}
EVERY_HIDDEN = {**HIDDEN_BY_NAME, **HIDDEN_BY_VALUES}
ANSWER_KEY = (set(EVERY_HIDDEN) - ACCEPTED) | LEFT_VISIBLE                  # the 63 columns of its section 7


def schema_columns():
    """Every table of the schema with its columns in order, read by SQLite itself."""
    con = sqlite3.connect(':memory:')
    con.executescript(FIXTURE.read_text(encoding='utf-8'))
    tables = [name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    found = {table: [row[1] for row in con.execute(f'PRAGMA table_info({table})')] for table in tables}
    con.close()
    return found


COLUMNS = schema_columns()


def text_of(column, table):
    category = personal_by_name(column, table)
    return CATEGORY_TEXT[category] if category else None


# -------------------------------------------------------- the schema, by name

def test_the_fixture_is_the_demo_schema_and_the_numbers_add_up():
    schema = FIXTURE.read_text(encoding='utf-8')
    assert 'RIMAL TELECOM  --  FICTIONAL DEMO DATABASE' in schema.splitlines()[1]
    assert (schema.count('CREATE TABLE '), schema.count('CREATE VIEW ')) == (67, 4)
    assert (len(COLUMNS), sum(len(c) for c in COLUMNS.values())) == (67, 515)
    every = {f'{table}.{column}' for table, columns in COLUMNS.items() for column in columns}
    assert set(EVERY_HIDDEN) | LEFT_VISIBLE <= every
    # 63 in the answer key, minus the 11 left visible, plus the 5 accepted: 57 - 55 by name, 2 by their values
    assert (len(ANSWER_KEY), len(LEFT_VISIBLE), len(ACCEPTED)) == (63, 11, 5)
    assert LEFT_VISIBLE <= ANSWER_KEY and not ACCEPTED & ANSWER_KEY
    assert (len(EVERY_HIDDEN), len(HIDDEN_BY_NAME), len(HIDDEN_BY_VALUES)) == (57, 55, 2)


def test_the_name_rules_on_every_column_of_the_schema():
    """All 515 columns: hidden by its name exactly when it is one of the 55, and as the right thing."""
    found = {f'{table}.{column}': text_of(column, table) for table, columns in COLUMNS.items() for column in columns}
    assert len(found) == 515
    assert {key: text for key, text in found.items() if text} == HIDDEN_BY_NAME
    assert not any(found[key] for key in LEFT_VISIBLE | set(HIDDEN_BY_VALUES))


# ----------------------------------------------------------- point by point

PAY_TABLES = ['HR_PAYROLL_RUN', 'HR_SALARY_HISTORY', 'employee', 'Employees', 'staff', 'payroll', 'emp_payslips', 'hr.salaries',
              'STAFF_WAGES', 'personnel_file']
OTHER_TABLES = [None, 'BIL_PAYMENT', 'SAL_COMMISSION', 'PRD_TARIFF_PLAN', 'SUB_SUBSCRIPTION', 'bil_invoice', 'usg_recharge',
                'sal_shop_target']
PAY_WORDS = ['GROSS_PAY', 'NET_PAY', 'SOCIAL_INS_DEDUCTION', 'OTHER_DEDUCTIONS', 'HOUSING_ALLOWANCE', 'TRANSPORT_ALLOWANCE',
             'pay', 'allowance', 'allowances', 'deduction', 'NetPay', 'basic_pay', 'monthly_pay', 'month_pay', 'pay_per_month',
             'pay_rate', 'pay_amount', 'pay_grade']
NOT_PAY_ANYWHERE = ['PAY_MONTH', 'PAID_DATE', 'pay_date', 'pay_day', 'pay_year', 'pay_period', 'pay_period_start',
                    'allowance_month', 'deduction_date', 'gross_pay_month',                 # a period or a date
                    'PAY_TYPE', 'pay_id', 'pay_status', 'deduction_type',                   # a fact about it, a key
                    'allowance_gb', 'data_allowance', 'sms_allowance',                      # a data allowance
                    'payment', 'payment_amount', 'paid_amount', 'commission', 'payer', 'repay']


@pytest.mark.parametrize('column', PAY_WORDS)
def test_1_pay_allowance_and_deduction_are_pay_in_a_table_of_staff_or_pay(column):
    for table in PAY_TABLES:
        assert personal_by_name(column, table) == 'salary', table
    for table in OTHER_TABLES:                                  # the word alone says too little
        assert personal_by_name(column, table) is None, table


@pytest.mark.parametrize('column', NOT_PAY_ANYWHERE)
def test_1_a_period_a_date_and_a_data_allowance_are_not_pay(column):
    for table in PAY_TABLES + OTHER_TABLES:
        assert personal_by_name(column, table) is None, table


def test_1_what_the_brief_lists_for_pay():
    hidden = ['HR_PAYROLL_RUN.GROSS_PAY', 'HR_PAYROLL_RUN.NET_PAY', 'HR_PAYROLL_RUN.SOCIAL_INS_DEDUCTION',
              'HR_PAYROLL_RUN.OTHER_DEDUCTIONS', 'HR_SALARY_HISTORY.HOUSING_ALLOWANCE', 'HR_SALARY_HISTORY.TRANSPORT_ALLOWANCE']
    assert all(HIDDEN_BY_NAME[key] == 'salary / pay' for key in hidden)
    visible = [('HR_PAYROLL_RUN', 'PAY_MONTH'), ('HR_PAYROLL_RUN', 'PAID_DATE'), ('PRD_TARIFF_PLAN', 'PAY_TYPE'),
               ('SUB_SUBSCRIPTION', 'PAY_TYPE')]
    visible += [(table, column) for table in ('BIL_PAYMENT', 'SAL_COMMISSION') for column in COLUMNS[table]]
    assert len(visible) == 4 + 9 + 8
    for table, column in visible:
        assert column in COLUMNS[table] and personal_by_name(column, table) is None, (table, column)
    # the words that were pay before stay pay wherever they are
    for table in PAY_TABLES + OTHER_TABLES:
        assert personal_by_name('basic_salary', table) == personal_by_name('bonus', table) == 'salary', table


@pytest.mark.parametrize('column, table, category', [
    ('COMPENSATION_AMT', 'SUP_COMPLAINT', None), ('compensation', 'bil_dispute', None), ('compensation_amount', 'claims', None),
    ('compensation', 'sup_ticket', None), ('Compensation', 'CustomerCases', None), ('compensation_sar', 'refunds', None),
    ('compensation', None, 'salary'), ('compensation', 'employee', 'salary'), ('total_compensation', 'hr_package', 'salary'),
    ('compensation', 'orders', 'salary'),
    ('compensation', 'hr_claim', 'salary'), ('compensation', 'staff_grievance', 'salary'),     # a table of staff, whatever else it is
    ('salary', 'sup_complaint', 'salary'), ('bonus', 'claims', 'salary'),                      # only the word compensation
])
def test_2_compensation_in_a_complaint_table_is_paid_to_a_customer(column, table, category):
    assert personal_by_name(column, table) == category


@pytest.mark.parametrize('column, table', [
    ('ACCOUNT_HOLDER', 'HR_BANK_ACCOUNT'), ('account_holder', None), ('AccountHolder', 'bank_details'), ('acct_holder', 'mandate'),
    ('card_holder', None), ('CARD_HOLDER', 'CRM_BANK_MANDATE'), ('cardholder', 'payment_card'), ('ACCOUNTHOLDER', 'HR_BANK_ACCOUNT'),
    ('card_holder_name', None), ('HOLDER_NAME', 'CRM_BANK_MANDATE'), ('account_holder_name', 'account_holder'),
])
def test_3_an_account_holder_or_card_holder_is_a_persons_name(column, table):
    assert personal_by_name(column, table) == 'name'
    assert personal_by_name('holder', table) is None and personal_by_name('account_holder_id', table) is None


@pytest.mark.parametrize('column, table', [
    ('ADDON_NAME', 'PRD_ADDON'), ('FAMILY_NAME', 'PRD_PRODUCT_FAMILY'), ('PARTNER_NAME', 'USG_ROAMING_PARTNER'),
    ('GRADE_NAME', 'HR_PAY_GRADE'), ('DEALER_NAME', 'SAL_DEALER'),
    ('dealer_name', 'dealers'), ('DealerName', 'DimDealer'), ('dealers_name', 'sal_dealer'), ('widget_name', 'dim_widget'),
    ('roaming_partner_name', 'usg_roaming_partner'), ('dealer_name', 'sal_stg_dealer_activation'), ('box_name', 'boxes'),
])
def test_4_the_name_of_what_the_table_holds_is_shown(column, table):
    assert personal_by_name(column, table) is None
    assert personal_by_name(column) == 'name'                   # without its table nothing says so
    assert personal_by_name(column, 'sal_order') == 'name'      # nor does another table


@pytest.mark.parametrize('column, table', [
    ('FAMILY_NAME', 'CRM_CUSTOMER'), ('CUSTOMER_NAME', 'CRM_CUSTOMER'), ('family_name', 'hr_employee'),
    ('customer_name', 'customers'), ('subscriber_name', 'subscriber'), ('contact_name', 'contacts'),
    ('employee_name', 'employee'), ('member_name', 'team_members'), ('user_name', 'app_user'), ('patient_name', 'patient'),
    ('account_name', 'crm_account'), ('holder_name', 'card_holder'), ('family_name', 'customer_family'),
    ('dependent_name', 'employee_dependent'), ('investor_name', 'investors'), ('household_name', 'household'),
    ('reviewer_name', 'doc_reviewer'),                                                  # tables of people
    ('CONTACT_NAME', 'SAL_DEALER'), ('CUSTOMER_NAME', 'SAL_STG_DEALER_ACTIVATION'), ('HOLDER_NAME', 'CRM_BANK_MANDATE'),
    ('dealer_contact_name', 'sal_dealer'), ('dealer_owner_name', 'sal_dealer'),         # a person in a table of things
    ('first_name', 'first_aid_kit'), ('last_name', 'last_visit'), ('full_name', 'full_backup'),
    ('middle_name', 'middle_east_office'), ('dealer_first_name', 'sal_dealer'), ('dealer_name_ar', 'sal_dealer'),
    ('FIRST_NAME', 'HR_EMPLOYEE'), ('FATHER_NAME', 'CRM_CUSTOMER'), ('FULL_NAME_AR', 'CRM_CUSTOMER'),
])
def test_4_a_persons_name_stays_hidden(column, table):
    assert personal_by_name(column, table) == 'name'


def test_4_what_was_the_name_of_a_thing_before_still_is():
    for column, table in [('plan_name', None), ('SHOP_NAME', 'SAL_SHOP'), ('CITY_NAME', 'LKP_CITY'), ('DEPT_NAME', 'HR_DEPARTMENT'),
                          ('COMPANY_NAME', 'CRM_BUSINESS_PROFILE'), ('BANK_NAME', 'HR_BANK_ACCOUNT'), ('SEGMENT_NAME', 'CRM_SEGMENT'),
                          ('QUEUE_NAME', 'SUP_CALL_LOG'), ('name', 'store'), ('department_name', 'employee')]:
        assert personal_by_name(column, table) is None, (column, table)
    assert personal_by_name('name', 'dealer') == 'name'         # a bare name: only a table of known things shows it


@pytest.mark.parametrize('column, table, category', [
    ('BUILDING_NO', 'CRM_ADDRESS', 'address'), ('UNIT_NO', 'CRM_ADDRESS', 'address'), ('ADDITIONAL_NO', 'CRM_ADDRESS', 'address'),
    ('POSTAL_CODE', 'CRM_ADDRESS', 'address'), ('LATITUDE', 'CRM_ADDRESS', 'address'), ('LONGITUDE', 'CRM_ADDRESS', 'address'),
    ('zip_code', 'addresses', 'address'), ('postcode', 'customer_address', 'address'), ('ApartmentNumber', 'HomeAddress', 'address'),
    ('secondary_number', 'national_address', 'address'),
    ('latitude', 'crm_customer', 'address'), ('longitude', 'subscriber', 'address'), ('lat', 'employee_home', 'address'),
    ('lng', 'customer_addr', 'address'),
    ('LATITUDE', 'NET_SITE', None), ('LONGITUDE', 'NET_SITE', None), ('latitude', 'net_cell', None), ('latitude', None, None),
    ('lat', 'sal_shop', None), ('DISTRICT', 'CRM_ADDRESS', None), ('CITY_CODE', 'CRM_ADDRESS', None), ('city', 'address', None),
    ('DISTRICT', 'NET_FIBRE_CABINET', None), ('DISTRICT', 'SAL_SHOP', None), ('ADDRESS_TYPE', 'CRM_ADDRESS', None),
    ('postal_code', 'customer', None), ('postal_code', 'store', None), ('postal_code', None, None),      # as Zephyr's answer key has it
    ('building_no', 'net_site', None), ('unit_no', 'employee', None), ('unit_price', 'address', None), ('UNIT', 'PRD_RATE_CARD', None),
    ('floor_count', 'address', None), ('building_id', 'crm_address', None),
])
def test_5_address_details_and_where_a_place_is_a_persons(column, table, category):
    assert personal_by_name(column, table) == category


@pytest.mark.parametrize('column, table', [
    ('PUK_CODE', 'SUB_SIM_CARD'), ('ROUTER_MAC', 'SUB_FIBRE_SERVICE'), ('puk', None), ('PUK1', None), ('puk2', 'sim'),
    ('mac', 'device'), ('mac_address', None), ('MacAddress', 'cpe'), ('wifi_mac_addr', None), ('IMEI', 'SUB_DEVICE'),
    ('ICCID', 'SUB_SIM_CARD'),
])
def test_6_puk_codes_and_mac_addresses_are_device_identifiers(column, table):
    assert personal_by_name(column, table) == 'subscriber_id'


def test_7_left_visible_on_purpose_and_accepted_as_hidden():
    for key in LEFT_VISIBLE:
        table, column = key.split('.')
        assert column in COLUMNS[table] and personal_by_name(column, table) is None, key
    assert {key: HIDDEN_BY_NAME.get(key) for key in sorted(ACCEPTED - set(HIDDEN_BY_VALUES))} == {
        'HR_PAY_GRADE.MAX_SALARY': 'salary / pay', 'HR_PAY_GRADE.MIN_SALARY': 'salary / pay',
        'SUB_FIBRE_SERVICE.SERVICE_NO': 'phone number'}


# ----------------------------------------------------- the rules are data

def shipped():
    return json.loads(RULES_FILE.read_text(encoding='utf-8'))


def test_the_new_rules_are_data_in_the_rules_file():
    names = shipped()['names']
    assert set(names['table_kinds']) == {'people', 'address', 'staff', 'customer_case'}
    assert {'hr', 'payroll', 'salary', 'employee', 'staff'} <= set(names['table_kinds']['staff'])
    assert {'customer', 'employee', 'subscriber', 'contact'} <= set(names['table_kinds']['people'])
    assert not {'dealer', 'partner', 'family', 'grade', 'addon', 'hr'} & set(names['table_kinds']['people'])
    assert {'complaint', 'dispute', 'claim', 'ticket'} <= set(names['table_kinds']['customer_case'])
    pay = [g for g in names['word_groups'] if g['category'] == 'salary']
    assert len(pay) == 1 and pay[0]['groups'] == [['pay', 'allowance', 'allowances', 'deduction', 'deductions']]
    assert pay[0]['only_in_tables'] == ['staff'] and {'month', 'date', 'period'} <= set(pay[0]['unless_followed_by'])
    assert names['word_exceptions'] == [{'category': 'salary', 'words': ['compensation'], 'only_in_tables': ['customer_case'],
                                         'not_in_tables': ['staff']}]
    assert names['own_name']['not_in_tables'] == ['people'] and {'first', 'last', 'full'} <= set(names['own_name']['not_for_words'])
    assert {'puk', 'mac'} <= set(names['words']['subscriber_id'])
    # still not words of their own: pay and allowance count through the rule above, in its tables only
    assert not {'pay', 'allowance', 'deduction', 'latitude', 'postal', 'holder'} & {w for words in names['words'].values() for w in words}


@pytest.fixture
def rules_file(tmp_path):
    """use(change) loads the shipped rules with a change made to them; the shipped rules are put back afterwards."""
    def use(change):
        data = shipped()
        change(data['names'])
        path = tmp_path / 'edited_rules.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return personal.use_rules(path)
    try:
        yield use
    finally:
        personal.use_rules()


def test_the_team_can_change_what_a_table_is(rules_file):
    assert personal_by_name('DEALER_NAME', 'SAL_DEALER') is None and personal_by_name('broker_name', 'broker') is None
    assert personal_by_name('gross_pay', 'duty_roster') is None

    def change(names):
        names['table_kinds']['people'] += ['dealer', 'broker']                  # here, dealers are people
        names['table_kinds']['staff'].append('roster')
    rules_file(change)
    assert personal_by_name('DEALER_NAME', 'SAL_DEALER') == 'name' and personal_by_name('broker_name', 'broker') == 'name'
    assert personal_by_name('gross_pay', 'duty_roster') == 'salary'
    assert personal_by_name('GRADE_NAME', 'HR_PAY_GRADE') is None               # the others as before


def before_this_round(names):
    """Takes out of the rules everything this round added."""
    for key in ('table_kinds', 'word_exceptions', 'own_name'):
        del names[key]
    names['word_groups'] = [g for g in names['word_groups'] if g['category'] not in ('name', 'address', 'salary')]
    names['words']['subscriber_id'] = [w for w in names['words']['subscriber_id'] if w[:3] not in ('puk', 'mac')]


def test_a_rules_file_from_before_this_round_still_loads_and_says_what_it_said(rules_file):
    """Everything this round added is in the file: taken out of it again, the app says what it said before."""
    rules = rules_file(before_this_round)
    assert (rules.table_kinds, rules.word_exceptions, rules.own_name) == ({}, [], None)
    before = {f'{table}.{column}': text_of(column, table) for table, columns in COLUMNS.items() for column in columns}
    hidden = {key for key, text in before.items() if text}
    assert len(hidden) == 46                                    # what the rules hid before this round
    assert hidden - set(HIDDEN_BY_NAME) == {'PRD_ADDON.ADDON_NAME', 'PRD_PRODUCT_FAMILY.FAMILY_NAME', 'USG_ROAMING_PARTNER.PARTNER_NAME',
                                            'HR_PAY_GRADE.GRADE_NAME', 'SAL_DEALER.DEALER_NAME', 'SUP_COMPLAINT.COMPENSATION_AMT'}
    assert set(HIDDEN_BY_NAME) - hidden == {
        'HR_PAYROLL_RUN.GROSS_PAY', 'HR_PAYROLL_RUN.NET_PAY', 'HR_PAYROLL_RUN.SOCIAL_INS_DEDUCTION', 'HR_PAYROLL_RUN.OTHER_DEDUCTIONS',
        'HR_SALARY_HISTORY.HOUSING_ALLOWANCE', 'HR_SALARY_HISTORY.TRANSPORT_ALLOWANCE', 'HR_BANK_ACCOUNT.ACCOUNT_HOLDER',
        'CRM_ADDRESS.BUILDING_NO', 'CRM_ADDRESS.UNIT_NO', 'CRM_ADDRESS.ADDITIONAL_NO', 'CRM_ADDRESS.POSTAL_CODE',
        'CRM_ADDRESS.LATITUDE', 'CRM_ADDRESS.LONGITUDE', 'SUB_SIM_CARD.PUK_CODE', 'SUB_FIBRE_SERVICE.ROUTER_MAC'}
    assert personal_by_name('line_number', 'subscriber') == 'phone' and personal_by_name('line_number', 'invoice_lines') is None


@pytest.mark.parametrize('change, message', [
    (lambda names: names['word_groups'].append({'category': 'salary', 'groups': [['stipend']], 'only_in_tables': ['staf']}),
     'a word_groups rule of "salary" names the kind of table "staf", which is not under "table_kinds"'),
    (lambda names: names['word_groups'].append({'category': 'salary', 'groups': [['stipend']], 'not_in_tables': ['shops']}),
     'names the kind of table "shops"'),
    (lambda names: names['own_name'].update(not_in_tables=['persons']), 'own_name names the kind of table "persons"'),
    (lambda names: names['word_exceptions'].append({'category': 'salary', 'words': ['bonus'], 'only_in_tables': ['cases']}),
     'a word_exceptions rule of "salary" names the kind of table "cases"'),
    (lambda names: names['word_exceptions'].append({'category': 'salary', 'words': ['bonus']}),
     'needs only_in_tables: the tables in which its words do not count'),
    (lambda names: names['word_exceptions'].append({'category': 'pay', 'words': ['bonus'], 'only_in_tables': ['staff']}),
     'the category "pay" has no text under "categories"'),
    (lambda names: names['table_kinds'].update(people=['sales rep']), '"sales rep" is not one word of letters and digits'),
])
def test_a_wrong_edit_of_the_new_keys_stops_the_app_with_a_message(change, message):
    data = shipped()
    change(data['names'])
    with pytest.raises(RulesError, match=re.escape(message)) as error:
        Rules(data, 'edited.json')
    assert str(error.value).startswith('edited.json: ')


# ----------------------------------------------------- the app, on the schema

ROWS = {
    'HR_PAYROLL_RUN': [(1, '2026-01', 14250.5, 1282.55, 310, 12657.95, '2026-01-27'),
                       (1, '2026-02', 14250.5, 1282.55, 0, 12967.95, '2026-02-26'),
                       (2, '2026-01', 9120.25, 820.82, 75, 8224.43, '2026-01-27')],
    'NET_SITE': [(1, 'RUH-0001', 'Olaya North', 3, 'RUH', 'MACRO', 24.713552, 46.675297, 30, '2019-04-01', 'ON_AIR'),
                 (2, 'JED-0001', 'Corniche Tower', 4, 'JED', 'ROOFTOP', 21.543333, 39.172779, 18, '2020-09-15', 'ON_AIR'),
                 (3, 'DMM-0001', 'King Fahd Road', 5, 'DMM', 'MACRO', 26.420683, 50.088794, 42, None, 'PLANNED')],
    'CRM_ADDRESS': [(1, 1, 'HOME', '7411', 'Prince Sultan Road', 'Al Olaya', 'RUH', '12211', '3320', '12', 24.690247, 46.685043, 'Y'),
                    (2, 2, 'HOME', '2954', 'Palestine Street', 'Al Hamra', 'JED', '23323', '6018', None, 21.527411, 39.160882, 'Y'),
                    (3, 3, 'BILLING', '8830', 'Dhahran Street', 'Al Shati', 'DMM', '32413', None, '4B', 26.451297, 50.119342, 'N')],
    'SUP_COMPLAINT': [(1, 11, '966520000001', 'BILLING', 'CST-000001', '2026-03-01 10:00:00', 'REFUND', 150),
                      (2, 12, '966520000002', 'COVERAGE', None, '2026-03-02 11:30:00', None, None),
                      (3, 13, '966520000003', 'BILLING', 'CST-000003', '2026-03-05 09:15:00', 'CREDIT', 75.5)],
}


def wait(client, job):
    assert 'id' in job, job
    for _ in range(60000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    """One app for this file, on its own empty data folder."""
    with local_client(create_app(tmp_path_factory.mktemp('rimal-data'))) as c:
        yield c


def workspace(client, name, database):
    """A database added to a workspace and profiled. Returns (workspace id, source id)."""
    ws = client.post('/api/workspaces', json={'name': name}).json()['id']
    source = client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(database)})
    assert source.status_code == 200, source.text
    assert source.json()['warnings'] == []
    done = wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    assert (done['state'], done['tables_done'], done['errors']) == ('done', 67, [])
    return ws, source.json()['id']


def build(path):
    """The schema as a SQLite file, with a few made-up rows in four tables."""
    con = sqlite3.connect(path)
    con.executescript(FIXTURE.read_text(encoding='utf-8'))
    for table, rows in ROWS.items():
        con.executemany(f'INSERT INTO {table} VALUES ({", ".join("?" * len(rows[0]))})', rows)
    con.commit()
    con.close()
    return path


@pytest.fixture(scope='module')
def schema(client, tmp_path_factory):
    """That file in a workspace, profiled. Returns (workspace id, source id)."""
    return workspace(client, 'Rimal schema', build(tmp_path_factory.mktemp('rimal-db') / 'rimal_schema.db'))


def run(client, where, sql):
    ws, source = where
    response = client.post(f'/api/workspaces/{ws}/sql', json={'source': source, 'sql': sql})
    assert response.status_code == 200, response.text
    return response.json()


def profile_of(client, ws, table, sample=False):
    return client.get(f'/api/workspaces/{ws}/tables/{table}/profile', params={'sample': str(sample).lower()}).json()


def walk(client, where, hidden):
    """Every column of every table, in the Profile tab and three ways in the SQL tab (by its name, under
    another name, through SELECT *): masked in the SQL tab exactly when the Profile tab masks it, for the
    same reason, and masked means no value without a star. Returns {table.column: why the Profile tab hides it}."""
    ws, _ = where
    walked, found = 0, {}
    for table in sorted(COLUMNS):
        profile = profile_of(client, ws, table)['columns']
        assert [c['name'] for c in profile] == COLUMNS[table], table
        star = run(client, where, f'SELECT * FROM {table}')
        assert [c['name'] for c in star['columns']] == COLUMNS[table], table
        for i, c in enumerate(profile):
            key = f'{table}.{c["name"]}'
            plain = run(client, where, f'SELECT {c["name"]} FROM {table}')
            alias = run(client, where, f'SELECT t.{c["name"]} AS v FROM {table} t')
            reason = None
            if c['personal']:
                assert c['personal_reason'] == f'Looks like {hidden.get(key)}', key
                by_name = personal_by_name(c['name'], table) is not None
                reason = f'{key}: {"looks" if by_name else "its values look"} like {hidden[key]}'
                found[key] = 'name' if by_name else 'values'
            shown = [(plain['columns'][0], [row[0] for row in plain['rows']]), (alias['columns'][0], [row[0] for row in alias['rows']]),
                     (star['columns'][i], [row[i] for row in star['rows']])]
            for column, values in shown:
                assert (column['personal'], column['reason']) == (c['personal'], reason), (key, column)
                if c['personal']:
                    assert all('*' in str(v) for v in values if v not in (None, '')), key
            assert (plain['masked'], alias['masked']) == (c['personal'], c['personal']), key
            walked += 1
    assert walked == 515
    return found


def test_the_profile_tab_and_the_sql_tab_on_the_schema(client, schema):
    """Without the real data: the 55 columns that their names hide, in both tabs, on all 515 columns."""
    found = walk(client, schema, HIDDEN_BY_NAME)
    assert set(found) == set(HIDDEN_BY_NAME) and set(found.values()) == {'name'}


def test_what_is_hidden_is_hidden_wherever_its_values_would_show(client, schema):
    ws, _ = schema
    pay = profile_of(client, ws, 'HR_PAYROLL_RUN', sample=True)
    columns = {c['name']: c for c in pay['columns']}
    for name in ('GROSS_PAY', 'SOCIAL_INS_DEDUCTION', 'OTHER_DEDUCTIONS', 'NET_PAY'):          # pay: nothing but counts
        st = columns[name]['stats']
        assert (st['min'], st['max'], st['top_hidden'], st['rows']) == (HIDDEN, HIDDEN, True, 3), name
        assert not {'avg', 'top', 'histogram'} & set(st), name
    assert (columns['PAY_MONTH']['personal'], columns['PAY_MONTH']['stats']['min']) == (False, '2026-01')
    assert (columns['PAID_DATE']['personal'], columns['PAID_DATE']['stats']['max']) == (False, '2026-02-26')
    assert pay['sample']['personal'] == ['GROSS_PAY', 'SOCIAL_INS_DEDUCTION', 'OTHER_DEDUCTIONS', 'NET_PAY']
    assert pay['sample']['rows'][0] == [1, '2026-01', HIDDEN, HIDDEN, HIDDEN, HIDDEN, '2026-01-27']
    # an address: the details and the place masked, district and city shown
    address = profile_of(client, ws, 'CRM_ADDRESS', sample=True)
    assert address['sample']['personal'] == ['BUILDING_NO', 'STREET_NAME', 'POSTAL_CODE', 'ADDITIONAL_NO', 'UNIT_NO', 'LATITUDE',
                                             'LONGITUDE']
    assert address['sample']['rows'][0] == [1, 1, 'HOME', '****', 'Pr************ad', 'Al Olaya', 'RUH', '1***1', '****', '**',
                                            '24*****47', '46*****43', 'Y']
    latitude = next(c for c in address['columns'] if c['name'] == 'LATITUDE')['stats']
    assert '*' in str(latitude['min']) and 'avg' not in latitude and 'histogram' not in latitude
    # the same words in a table of the network: shown, with their numbers
    site = {c['name']: c for c in profile_of(client, ws, 'NET_SITE')['columns']}
    assert (site['LATITUDE']['personal'], site['LATITUDE']['stats']['min'], site['LONGITUDE']['stats']['max']) == (
        False, 21.543333, 50.088794)
    # compensation paid to a customer: shown
    complaint = {c['name']: c for c in profile_of(client, ws, 'SUP_COMPLAINT')['columns']}
    assert (complaint['COMPENSATION_AMT']['personal'], complaint['COMPENSATION_AMT']['stats']['max']) == (False, 150)
    assert complaint['SERVICE_NO']['stats']['min'] == '96********01'
    # the report says the same
    page = client.get(f'/api/workspaces/{ws}/report', params={'format': 'html'}).text
    for raw in ('14250', '14,250', '12657', '12,657', '1282.55', '24.690247', '46.685043', 'Prince Sultan'):
        assert raw not in page, raw
    assert 'Olaya North' in page and 'CST-000001' in page       # a site's name and a complaint's reference: shown


def test_the_sql_tab_on_the_new_rules(client, schema):
    r = run(client, schema, 'SELECT EMP_ID, PAY_MONTH, GROSS_PAY, NET_PAY, PAID_DATE FROM HR_PAYROLL_RUN ORDER BY 1, 2')
    assert r['personal'] == ['GROSS_PAY', 'NET_PAY'] and r['rows'][0] == [1, '2026-01', HIDDEN, HIDDEN, '2026-01-27']
    assert r['columns'][2]['reason'] == 'HR_PAYROLL_RUN.GROSS_PAY: looks like salary / pay'
    r = run(client, schema, 'SELECT PAY_MONTH, SUM(GROSS_PAY - OTHER_DEDUCTIONS) AS paid, COUNT(NET_PAY) AS n FROM HR_PAYROLL_RUN '
                            'GROUP BY PAY_MONTH ORDER BY 1')
    assert r['rows'] == [['2026-01', HIDDEN, 2], ['2026-02', HIDDEN, 1]]
    assert r['columns'][1]['reason'] == 'Built from HR_PAYROLL_RUN.GROSS_PAY (looks like salary / pay)'
    # the same name, two tables: each judged by its own
    r = run(client, schema, 'SELECT s.SITE_CODE, s.LATITUDE, a.LATITUDE AS home, a.DISTRICT FROM NET_SITE s '
                            'JOIN CRM_ADDRESS a ON a.CITY_CODE = s.CITY_CODE ORDER BY 1')
    assert [(c['name'], c['personal'], c['reason']) for c in r['columns']] == [
        ('SITE_CODE', False, None), ('LATITUDE', False, None), ('home', True, 'CRM_ADDRESS.LATITUDE: looks like address'),
        ('DISTRICT', False, None)]
    assert r['rows'][0] == ['DMM-0001', 26.420683, '26*****97', 'Al Shati']
    r = run(client, schema, 'SELECT COMPENSATION_AMT, RESOLUTION FROM SUP_COMPLAINT ORDER BY COMPLAINT_ID')
    assert r['personal'] == [] and r['rows'][0] == [150, 'REFUND']


def test_a_column_that_is_not_resolved_is_not_shown_for_want_of_its_table(client, schema):
    """Pay words and places need their table to be judged. Where the SQL tab cannot tell the table for
    sure it goes by names alone - and then a name that some table of the workspace hides is hidden."""
    merged = 'NATURAL JOIN (SELECT 1 AS one) x'                 # a join that merges columns: the * is not expanded
    r = run(client, schema, f'SELECT * FROM HR_PAYROLL_RUN {merged} ORDER BY 1, 2')
    assert r['personal'] == ['GROSS_PAY', 'SOCIAL_INS_DEDUCTION', 'OTHER_DEDUCTIONS', 'NET_PAY']
    assert {c['reason'] for c in r['columns'] if c['personal']} == {'Looks like salary / pay'}
    assert r['rows'][0] == [1, '2026-01', HIDDEN, HIDDEN, HIDDEN, HIDDEN, '2026-01-27', 1]
    r = run(client, schema, f'SELECT * FROM (SELECT GROSS_PAY - NET_PAY AS kept, PAY_MONTH FROM HR_PAYROLL_RUN) {merged}')
    assert (r['personal'], r['columns'][0]['reason'], {row[0] for row in r['rows']}) == (['kept'], 'Built from a personal column', {HIDDEN})
    r = run(client, schema, f'SELECT * FROM CRM_ADDRESS {merged}')
    assert r['personal'] == ['BUILDING_NO', 'STREET_NAME', 'POSTAL_CODE', 'ADDITIONAL_NO', 'UNIT_NO', 'LATITUDE', 'LONGITUDE']
    # the safe side: a site's place too, as long as the SQL tab cannot say that it is the site's
    r = run(client, schema, f'SELECT * FROM NET_SITE {merged}')
    assert r['personal'] == ['LATITUDE', 'LONGITUDE'] and {c['reason'] for c in r['columns'] if c['personal']} == {'Looks like address'}
    assert run(client, schema, 'SELECT LATITUDE, LONGITUDE FROM NET_SITE ORDER BY SITE_ID')['rows'][0] == [24.713552, 46.675297]
    # a mark "not personal" on the table's column is read there as well
    ws, _ = schema
    for column in ('LATITUDE', 'LONGITUDE'):
        assert client.post(f'/api/workspaces/{ws}/personal', json={'table': 'CRM_ADDRESS', 'column': column, 'personal': False}).status_code == 200
    try:
        assert run(client, schema, f'SELECT * FROM NET_SITE {merged}')['personal'] == []
    finally:
        for column in ('LATITUDE', 'LONGITUDE'):
            client.post(f'/api/workspaces/{ws}/personal', json={'table': 'CRM_ADDRESS', 'column': column, 'personal': None})
    assert run(client, schema, f'SELECT * FROM NET_SITE {merged}')['personal'] == ['LATITUDE', 'LONGITUDE']


def test_a_workspace_profiled_before_this_round_is_masked_when_the_app_starts_with_todays_rules(tmp_path, rules_file):
    rules_file(before_this_round)
    data_dir = tmp_path / 'data'
    with local_client(create_app(data_dir)) as old:
        ws, source = workspace(old, 'Profiled before', build(tmp_path / 'rimal_schema.db'))
        before = {c['name']: c for c in profile_of(old, ws, 'HR_PAYROLL_RUN')['columns']}
        assert before['GROSS_PAY']['personal'] is False and before['GROSS_PAY']['stats']['max'] == 14250.5
        assert profile_of(old, ws, 'SUP_COMPLAINT')['columns'][7]['personal_reason'] == 'Looks like salary / pay'
    assert b'14250.5' in (data_dir / 'explorer.db').read_bytes() and b'24.690247' in (data_dir / 'explorer.db').read_bytes()

    personal.use_rules()                                        # the app is started again, with today's rules
    app = create_app(data_dir)
    with local_client(app) as new:
        shown = {c['name']: c for c in profile_of(new, ws, 'HR_PAYROLL_RUN')['columns']}
        assert shown['GROSS_PAY']['personal'] is True and shown['GROSS_PAY']['stats']['max'] == HIDDEN      # never shown ...
        new.get(f'/api/workspaces/{ws}/profile')                # ... and opening the workspace masks what is stored
        stored = app.state.profiling.store.columns(ws)
        for name in ('GROSS_PAY', 'SOCIAL_INS_DEDUCTION', 'OTHER_DEDUCTIONS', 'NET_PAY'):
            assert stored['HR_PAYROLL_RUN'][name]['masked'] is True and stored['HR_PAYROLL_RUN'][name]['max'] == HIDDEN, name
            assert not {'avg', 'top', 'histogram'} & set(stored['HR_PAYROLL_RUN'][name]), name
        assert stored['CRM_ADDRESS']['LATITUDE']['max'] == '26*****97' and stored['CRM_ADDRESS']['POSTAL_CODE']['masked'] is True
        assert stored['NET_SITE']['LATITUDE']['max'] == 26.420683
        # the other way round: a complaint's compensation was masked as pay; its numbers return with the next profile
        amount = profile_of(new, ws, 'SUP_COMPLAINT')['columns'][7]
        assert (amount['name'], amount['personal'], amount['stats']['max'], amount['stats']['stale_mask']) == (
            'COMPENSATION_AMT', False, HIDDEN, True)
        assert run(new, (ws, source), 'SELECT MAX(COMPENSATION_AMT) FROM SUP_COMPLAINT')['rows'] == [[150]]
        wait(new, new.post(f'/api/workspaces/{ws}/profile', json={'tables': ['SUP_COMPLAINT']}).json())
        amount = profile_of(new, ws, 'SUP_COMPLAINT')['columns'][7]
        assert amount['stats']['max'] == 150 and 'stale_mask' not in amount['stats']
    raw = (data_dir / 'explorer.db').read_bytes()
    for value in (b'14250.5', b'12657.95', b'24.690247', b'46.685043'):
        assert value not in raw, value


# ------------------------------------------------------------ the real data

@pytest.fixture(scope='module')
def rimal(client):
    """The small demo database, read where it is, in a workspace of its own."""
    return workspace(client, 'Rimal small', SMALL)


@real_data
def test_the_copy_of_the_schema_is_the_demo_databases():
    theirs = DEMO_DIR / 'schema.sql'
    if theirs.is_file():
        assert FIXTURE.read_bytes() == theirs.read_bytes(), 'schema.sql of the demo database has changed: copy it again'
    con = sqlite3.connect(f'{SMALL.resolve().as_uri()}?mode=ro', uri=True)
    try:
        tables = [name for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        assert {t: [row[1] for row in con.execute(f'PRAGMA table_info({t})')] for t in tables} == COLUMNS
    finally:
        con.close()


@real_data
def test_the_answer_key_lists_the_63_columns():
    key = DEMO_DIR / 'ANSWER_KEY.md'
    if not key.is_file():
        pytest.skip(f'no answer key in {DEMO_DIR}')
    section = key.read_text(encoding='utf-8').split('## 7. Personal data that should be masked')[1].split('\n## ')[0].split('\n### ')[0]
    listed = set(re.findall(r'^\| `([A-Z0-9_]+\.[A-Z0-9_]+)` \|', section, flags=re.M))
    assert len(listed) == 63 and listed == ANSWER_KEY
    assert (listed - LEFT_VISIBLE) | ACCEPTED == set(EVERY_HIDDEN)


@real_data
def test_the_profile_tab_hides_exactly_the_57_columns_and_the_sql_tab_agrees_on_every_column(client, rimal):
    """The answer key's 63, minus the 11 left visible on purpose, plus the 5 accepted: 57 - by name and by values."""
    found = walk(client, rimal, EVERY_HIDDEN)
    assert set(found) == set(EVERY_HIDDEN) and len(found) == 57
    assert {key for key, how in found.items() if how == 'values'} == set(HIDDEN_BY_VALUES)
    assert sum(1 for how in found.values() if how == 'name') == 55


@real_data
def test_the_real_values_are_masked_and_what_is_left_visible_is_shown(client, rimal):
    ws, _ = rimal
    tables = {t['id']: t for t in client.get(f'/api/workspaces/{ws}/profile').json()['tables']}
    assert set(tables) == set(COLUMNS) and all(t['status'] == 'profiled' for t in tables.values())
    hidden = 0
    for table in sorted(COLUMNS):
        profile = profile_of(client, ws, table, sample=True)
        at = {name: i for i, name in enumerate(profile['sample']['columns'])}
        assert profile['sample']['personal'] == [c['name'] for c in profile['columns'] if c['personal']], table
        for c in profile['columns']:
            key, st = f'{table}.{c["name"]}', c['stats']
            values = [row[at[c['name']]] for row in profile['sample']['rows'] if row[at[c['name']]] not in (None, '')]
            if key not in EVERY_HIDDEN:
                assert c['personal'] is False and not st.get('masked'), key
                continue
            hidden += 1
            assert c['personal'] is True and st['masked'] is True and 'avg' not in st and 'histogram' not in st, key
            assert all('*' in str(v) for v in values), key
            assert all('*' in str(st[m]) for m in ('min', 'max') if st.get(m) not in (None, '')), key
            assert all('*' in str(v) for v, _ in st.get('top') or [] if v not in (None, '')), key
            if EVERY_HIDDEN[key] == 'salary / pay':
                assert st['min'] == st['max'] == HIDDEN and st['top_hidden'] is True and set(values) <= {HIDDEN}, key
    assert hidden == 57
    # left visible on purpose: with their values
    customer = {c['name']: c for c in profile_of(client, ws, 'CRM_CUSTOMER')['columns']}
    assert {v for v, _ in customer['GENDER']['stats']['top']} == {'M', 'F'} and customer['NATIONALITY']['stats']['min'] != ''
    assert not any('*' in str(v) for v, _ in customer['NATIONALITY']['stats']['top'])
    business = {c['name']: c for c in profile_of(client, ws, 'CRM_BUSINESS_PROFILE')['columns']}
    assert re.fullmatch(r'90\d{8}', business['CR_NUMBER']['stats']['min']) and re.fullmatch(r'39\d{12}3', business['VAT_NUMBER']['stats']['min'])
    r = run(client, rimal, 'SELECT n.NOTE_TEXT, s.COMMENT_TEXT FROM SUP_TICKET_NOTE n JOIN SUP_SURVEY s ON s.TICKET_ID = n.TICKET_ID '
                           'WHERE s.COMMENT_TEXT IS NOT NULL LIMIT 5')
    assert r['personal'] == [] and len(r['rows']) == 5
    r = run(client, rimal, 'SELECT a.DISTRICT, c.CELL_ID, f.ONT_SERIAL FROM CRM_ADDRESS a, USG_CDR c, SUB_FIBRE_SERVICE f '
                           'WHERE c.CELL_ID IS NOT NULL LIMIT 3')
    assert r['personal'] == [] and len(r['rows']) == 3
    # the things with their names, the people without
    r = run(client, rimal, 'SELECT d.DEALER_NAME, d.CONTACT_NAME, g.GRADE_NAME, e.FIRST_NAME, p.NET_PAY, p.PAY_MONTH '
                           'FROM SAL_DEALER d, HR_EMPLOYEE e JOIN HR_PAY_GRADE g ON g.GRADE_CODE = e.GRADE_CODE '
                           'JOIN HR_PAYROLL_RUN p ON p.EMP_ID = e.EMP_ID WHERE d.CONTACT_NAME IS NOT NULL LIMIT 4')
    assert r['personal'] == ['CONTACT_NAME', 'FIRST_NAME', 'NET_PAY']
    for row in r['rows']:
        assert '*' not in row[0] and '*' in row[1] and '*' not in row[2] and '*' in row[3] and row[4] == HIDDEN
        assert re.fullmatch(r'20\d\d-\d\d', row[5])
