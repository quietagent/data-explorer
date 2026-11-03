"""Personal data: flagged by name, flagged by values, and masked."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.profiling.personal import mask, personal_by_name, personal_by_values, tokens, value_kind

# ------------------------------------------------------------------- by name

PERSONAL_NAMES = [
    ('name', 'customers', 'name'), ('full_name', None, 'name'), ('FIRST_NAME', None, 'name'), ('LastName', None, 'name'),
    ('fullname', None, 'name'), ('customer_name', None, 'name'), ('account_holder_name', None, 'name'),
    ('user_name', None, 'name'), ('name_ar', 'employees', 'name'),
    ('msisdn', None, 'phone'), ('MSISDN_NO', None, 'phone'), ('mobile', None, 'phone'), ('mobile_number', None, 'phone'),
    ('phone', None, 'phone'), ('home_phone', None, 'phone'), ('PhoneNumber', None, 'phone'),
    ('email', None, 'email'), ('EMAIL_ADDRESS', None, 'email'), ('contact_email', None, 'email'),
    ('national_id', None, 'national_id'), ('NATIONAL_ID_NO', None, 'national_id'), ('nid', None, 'national_id'),
    ('iqama', None, 'national_id'), ('iqama_number', None, 'national_id'), ('id_number', None, 'national_id'),
    ('passport', None, 'passport'), ('passport_no', None, 'passport'),
    ('address', None, 'address'), ('billing_address', None, 'address'), ('address_line1', None, 'address'),
    ('dob', None, 'birth_date'), ('birth_date', None, 'birth_date'), ('date_of_birth', None, 'birth_date'),
    ('BirthDate', None, 'birth_date'),
    ('iban', None, 'iban'), ('customer_iban', None, 'iban'),
    ('card_number', None, 'card'), ('credit_card', None, 'card'), ('card', None, 'card'),
]
NOT_PERSONAL_NAMES = [
    ('plan_name', None), ('branch_name', None), ('file_name', None), ('table_name', None), ('city_name', None),
    ('name', 'plans'), ('name', 'branches'), ('NAME', 'PRODUCTS'), ('name', 'crm.cities'),
    ('email_verified', None), ('phone_type', None), ('address_type', None), ('card_type', None), ('name_length', None),
    ('address_id', None), ('card_id', None), ('customer_id', None), ('city', None), ('status', None),
    ('created_at', None), ('amount', None), ('rename_count', None), ('filename', None), ('cardinality', None),
    ('emailed', None), ('telemetry', None), ('', None),
]


@pytest.mark.parametrize('column, table, category', PERSONAL_NAMES)
def test_columns_flagged_by_name(column, table, category):
    assert personal_by_name(column, table) == category


@pytest.mark.parametrize('column, table', NOT_PERSONAL_NAMES)
def test_columns_not_flagged_by_name(column, table):
    assert personal_by_name(column, table) is None


def test_names_are_split_into_words():
    assert tokens('FullName') == tokens('full_name') == tokens('FULL NAME') == tokens('full-name') == ['full', 'name']
    assert tokens('MSISDN_NO2') == ['msisdn', 'no2'] and tokens(None) == []


# ----------------------------------------------------------------- by values

def test_saudi_mobile_numbers_are_recognised():
    assert personal_by_values(['0512345678', '0598765432', '0555000111']) == 'phone'
    assert personal_by_values(['966512345678', '966598765432', '966555000111', None, '']) == 'phone'
    assert personal_by_values(['+966512345678', '00966598765432', '055 500 0111']) == 'phone'
    assert personal_by_values([966512345678, 966598765432, Decimal('966555000111')]) == 'phone'      # stored as numbers
    assert {value_kind(v) for v in ('0512345678', '966512345678', 512345678)} == {'Saudi mobile'}


def test_emails_ids_and_ibans_are_recognised():
    assert personal_by_values(['a@b.com', 'ann.lee@example.org', 'x_y@mail.sa']) == 'email'
    assert personal_by_values(['1012345678', '2198765432', '1000000001']) == 'national_id'
    assert personal_by_values([1012345678, 2198765432.0, 1000000001]) == 'national_id'
    assert personal_by_values(['SA0380000000608010167519', 'sa4420000001234567891234',
                               'SA03 8000 0000 6080 1016 7519']) == 'iban'
    assert personal_by_values(['3012345678', '4198765432', '5000000001']) is None           # do not start with 1 or 2
    assert personal_by_values(['101234567', '21987654321', '12345']) is None                # not 10 digits


def test_values_must_mostly_match_and_be_enough():
    assert personal_by_values(['a@b.com', 'c@d.com']) is None                               # too few to say
    assert personal_by_values([]) is None and personal_by_values([None, '', '  ']) is None
    mostly = ['a@b.com'] * 8 + ['n/a', 'unknown']
    assert personal_by_values(mostly) == 'email'                                            # 80% match
    assert personal_by_values(['a@b.com'] * 7 + ['n/a'] * 3) is None                        # 70% do not
    assert personal_by_values(['Riyadh', 'Jeddah', 'Abha', 'Tabuk']) is None
    assert personal_by_values([1, 2, 3, 4, 5]) is None and personal_by_values([True, False, True]) is None


# -------------------------------------------------------------------- masking

@pytest.mark.parametrize('value, masked', [
    ('0512345667', '05******67'),                       # the brief's examples
    ('ahmed@domain.com', 'a***@d***.com'),
    ('a.very.long.name@mail.example.org', 'a***@m***.org'),
    ('966512345678', '96********78'),
    ('1012345678', '10******78'),
    (1012345678, '10******78'), (Decimal('1012345678'), '10******78'), (1012345678.0, '10******78'),
    ('Ahmed Al-Harbi', 'Ah**********bi'),
    ('SA0380000000608010167519', 'SA************19'),   # long values: at most 12 stars
    ('abcdef', 'a****f'), ('abcde', 'a***e'), ('abcd', '****'), ('abc', '***'), ('ab', '**'), ('a', '*'), ('', ''),
    (date(1990, 5, 17), '****-**-**'), (datetime(1990, 5, 17, 8, 30), '****-**-** **:**'),
    ('1990-05-17', '****-**-**'), ('1990-05-17 08:30:00', '****-**-** **:**'),
    (None, None),
])
def test_masking_keeps_only_the_first_and_last_characters(value, masked):
    assert mask(value) == masked


def test_a_masked_value_does_not_contain_the_middle():
    for value in ('0512345667', 'ahmed.harbi@example.com', 'Noura Al-Qahtani', '2198765432'):
        hidden = mask(value)
        assert hidden != value and '*' in hidden
        assert value[2:-2] not in hidden
