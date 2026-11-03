"""Personal data: which columns look personal, and how their values are masked.

A column is treated as personal when
  * its name says so (name, phone, MSISDN, IMSI, e-mail, national ID, address, birth date, IBAN, salary ...), or
  * its values look like it (phone numbers, e-mail addresses, 10-digit national / Iqama numbers,
    Saudi IBANs), or
  * the user marked it.
A user's mark always wins, in both directions (see ProfileStore.marks).

A name is read together with the name of its table, which says what the table holds:
  gross_pay, housing_allowance   pay in a table of staff or pay (hr_payroll_run); not just anywhere
  latitude, postal_code          a person's in a table of people or addresses; not a radio site's
  compensation                   pay - but paid to a customer in a table of complaints
  dealer_name, grade_name        the name of what the table holds (sal_dealer, hr_pay_grade); a
                                 customer_name in a table of customers is a person's name

Every name rule and value rule is data, not code: they are read from personal_rules.json, next to
this file, when the app starts - the words, the kinds of table and the words that tell them. The
team extends that file; nothing here has to change.

Masked values keep their first and last characters:  05******67   a***@d***.com
Values of four characters or fewer, birth dates and text that looks like a date keep nothing.
Pay (salary, wage, bonus ...) keeps nothing at all, not even its length: every value is *****.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

RULES_FILE = Path(__file__).with_name('personal_rules.json')


class RulesError(Exception):
    """personal_rules.json cannot be used; the message says what is wrong with it."""


class Rules:
    """personal_rules.json, checked and compiled."""

    def __init__(self, data, origin='personal_rules.json'):
        self.origin = origin
        try:
            self._build(data)
        except RulesError as exc:
            raise RulesError(str(exc) if str(exc).startswith(origin) else f'{origin}: {exc}') from None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RulesError(f'{origin}: {type(exc).__name__}: {exc}') from None

    def _build(self, data):
        names, values, masking = data['names'], data['values'], data.get('masking') or {}
        self.category_text = {str(k): str(v) for k, v in data['categories'].items()}
        self.words = {category: {self._word(w) for w in words} for category, words in names['words'].items()}
        self.table_kinds = {str(kind): {self._word(w) for w in words} for kind, words in (names.get('table_kinds') or {}).items()}
        self.groups = []
        for rule in names.get('word_groups') or []:
            groups = [{self._word(w) for w in group} for group in rule['groups']]
            if not groups or not all(groups):
                raise RulesError(f'{self.origin}: a word_groups rule of "{rule.get("category")}" has an empty group')
            where = f'a word_groups rule of "{rule["category"]}"'
            self.groups.append({
                'category': rule['category'], 'groups': groups, 'words': set().union(*groups),
                'unless_table_has': self._words(rule.get('unless_table_has')),
                'only_in_tables': self._kinds(rule.get('only_in_tables'), where),
                'not_in_tables': self._kinds(rule.get('not_in_tables'), where),
                'unless_column_has': self._words(rule.get('unless_column_has')),
                'unless_followed_by': self._words(rule.get('unless_followed_by'))})
        self.word_exceptions = []
        for rule in names.get('word_exceptions') or []:
            where = f'a word_exceptions rule of "{rule["category"]}"'
            only, never = self._kinds(rule.get('only_in_tables'), where), self._kinds(rule.get('not_in_tables'), where)
            if not only:
                raise RulesError(f'{self.origin}: {where} needs only_in_tables: the tables in which its words do not count')
            self.word_exceptions.append({'category': rule['category'], 'words': self._words(rule['words']),
                                         'only_in_tables': only, 'not_in_tables': never})
        self.about_words = {self._word(w) for w in names.get('about_words') or []}
        self.key_words = {self._word(w) for w in names.get('key_words') or []}
        self.kept_with_key_word = set(names.get('kept_with_key_word') or [])
        self.thing_words = {self._word(w) for w in names.get('thing_words') or []}
        self.name_words = self.words.get('name', set())
        own = names.get('own_name')                     # None: a rules file from before the rule existed
        self.own_name = None if own is None else {'not_in_tables': self._kinds(own.get('not_in_tables'), 'own_name'),
                                                  'not_for_words': self._words(own.get('not_for_words'))}

        self.min_values = int(values.get('min_values', 3))
        self.normalisers = {}
        for name, spec in (values.get('normalisers') or {}).items():
            remove = self._regex(spec.get('remove') or '', f'normaliser {name}') if spec.get('remove') else None
            replace = [(self._regex(a, f'normaliser {name}'), str(b)) for a, b in spec.get('replace') or []]
            self.normalisers[name] = (remove, replace)
        self.value_rules = []
        for rule in values['rules']:
            category, how = rule['category'], rule.get('normalise') or 'trim'
            if how not in self.normalisers:
                raise RulesError(f'{self.origin}: value rule "{category}" uses the unknown normaliser "{how}"')
            share = float(rule.get('share', 0.8))
            if not 0 < share <= 1:
                raise RulesError(f'{self.origin}: value rule "{category}" needs a share between 0 and 1')
            patterns = [(label, self._regex(pattern, f'value rule {category}')) for label, pattern in rule['patterns'].items()]
            if not patterns:
                raise RulesError(f'{self.origin}: value rule "{category}" has no pattern')
            self.value_rules.append({
                'category': category, 'share': share, 'normalise': how, 'patterns': patterns,
                'not_for_families': set(rule.get('not_for_families') or []),
                'not_for_name_endings': tuple(str(e).lower() for e in rule.get('not_for_name_endings') or [])})
        for category in (list(self.words) + [g['category'] for g in self.groups] + [r['category'] for r in self.value_rules]
                         + [x['category'] for x in self.word_exceptions]):
            if category not in self.category_text:
                raise RulesError(f'{self.origin}: the category "{category}" has no text under "categories"')

        self.fully_masked_up_to = int(masking.get('fully_masked_up_to', 4))
        self.fully_masked_categories = set(masking.get('fully_masked_categories') or ['birth_date'])
        self.length_hidden_categories = set(masking.get('length_hidden_categories') or [])
        for category in list(masking.get('fully_masked_categories') or []) + list(masking.get('length_hidden_categories') or []):
            if category not in self.category_text:
                raise RulesError(f'{self.origin}: masking names the category "{category}", which has no text under "categories"')
        self.date_like = [self._regex(p, 'masking date_like') for p in masking.get('date_like') or []]

    @staticmethod
    def _word(word):
        text = str(word).strip().lower()
        if not re.fullmatch(r'[a-z0-9]+', text):
            raise RulesError(f'"{word}" is not one word of letters and digits')
        return text

    def _words(self, words):
        return {self._word(w) for w in words or []}

    def _kinds(self, kinds, where):
        """The names of table kinds a rule refers to; each must be listed under names.table_kinds."""
        kinds = [str(k) for k in kinds or []]
        for kind in kinds:
            if kind not in self.table_kinds:
                raise RulesError(f'{self.origin}: {where} names the kind of table "{kind}", which is not under "table_kinds"')
        return kinds

    def _regex(self, pattern, where):
        try:
            return re.compile(pattern)
        except re.error as exc:
            raise RulesError(f'{self.origin}: {where}: bad regular expression {pattern!r} ({exc})') from None


def rules_path():
    """The rules file in use: DATA_EXPLORER_PERSONAL_RULES, or personal_rules.json next to this module."""
    return Path(os.environ.get('DATA_EXPLORER_PERSONAL_RULES') or RULES_FILE)


def load_rules(path=None):
    """Read and check a rules file. Raises RulesError with a message that names the problem."""
    path = Path(path or rules_path())
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise RulesError(f'{path}: cannot be read ({exc.strerror or exc})') from None
    except ValueError as exc:
        raise RulesError(f'{path}: not valid JSON ({exc})') from None
    return Rules(data, path.name)


def use_rules(path=None):
    """Load a rules file and make it the one in use (at start, and by tests). Returns the rules."""
    global RULES, CATEGORY_TEXT
    RULES = load_rules(path)
    CATEGORY_TEXT = RULES.category_text
    return RULES


RULES = None
CATEGORY_TEXT = {}
use_rules()


# ---------------------------------------------------------------- by name ----

def tokens(name):
    """Lower-case words of an identifier: 'FullName', 'full_name' and 'FULL NAME' -> ['full', 'name']."""
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', str(name or ''))
    return [t for t in re.split(r'[^a-z0-9]+', text.lower()) if t]


def _singular(word):
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    if word.endswith('es') and len(word) > 4 and word[:-2] in RULES.thing_words:
        return word[:-2]
    return word[:-1] if word.endswith('s') and not word.endswith('ss') else word


def _is_thing(word):
    return word in RULES.thing_words or _singular(word) in RULES.thing_words


def _forms(word):
    """A word of a name as a list may have it: as written, in the singular, without its 'es'."""
    forms = {word, _singular(word)}
    if word.endswith('es') and len(word) > 4:
        forms.add(word[:-2])
    return forms


def _table_is(table_words, kinds):
    """True when the table's own name says it is a table of one of these kinds (names.table_kinds)."""
    return any(form in RULES.table_kinds[kind] for kind in kinds for word in table_words for form in _forms(word))


def _group_applies(rule, words, table_words):
    """True when a word_groups rule fits this column of this table."""
    if not all(any(w in group for w in words) for group in rule['groups']):
        return False
    if any(w in rule['unless_table_has'] for w in table_words):
        return False                                    # invoice_lines.line_number is a row number, not a phone line
    if rule['only_in_tables'] and not _table_is(table_words, rule['only_in_tables']):
        return False                                    # gross_pay is pay in hr_payroll_run; "pay" alone says too little
    if rule['not_in_tables'] and _table_is(table_words, rule['not_in_tables']):
        return False
    if any(w in rule['unless_column_has'] for w in words):
        return False                                    # allowance_gb: a data allowance
    if rule['unless_followed_by']:
        after = words[max(i for i, w in enumerate(words) if w in rule['words']) + 1:]
        if after and all(w in rule['unless_followed_by'] for w in after):
            return False                                # pay_month, pay_date: a period and a date, not pay
    return True


def _not_counting(category, table_words):
    """The words of a category that do not count in this table (names.word_exceptions)."""
    out = set()
    for rule in RULES.word_exceptions:
        if (rule['category'] == category and _table_is(table_words, rule['only_in_tables'])
                and not _table_is(table_words, rule['not_in_tables'])):
            out |= rule['words']                        # compensation in sup_complaint: paid to a customer
    return out


def _names_its_table(others, table_words):
    """'<x>_name' in a table whose own name holds x: the name of that thing, not of a person
    (names.own_name). Never in a table of people, and never when x goes with a person's name."""
    rule = RULES.own_name
    if rule is None or not table_words or _table_is(table_words, rule['not_in_tables']):
        return False
    held = set().union(*(_forms(w) for w in table_words))
    return all(w not in rule['not_for_words'] and _forms(w) & held for w in others)


def personal_by_name(column, table=None):
    """The category a column's name puts it in ('phone', 'email', 'name' ...), or None.

    table: the name of its table. Some rules need it (pay words count in a table of staff or pay,
    a latitude in a table of people or addresses) and some are switched off by it (the name of the
    thing a table holds). Without a table those rules say nothing either way."""
    r = RULES
    words = tokens(column)
    if not words:
        return None
    table_words = tokens(str(table).rsplit('.', 1)[-1]) if table else []
    category, by_word = None, False
    for rule in r.groups:
        if _group_applies(rule, words, table_words):
            category = rule['category']
            break
    if category is None:
        joined = ''.join(words)
        for found, names in r.words.items():
            if table_words and r.word_exceptions:
                names = names - _not_counting(found, table_words)
            if any(w in names for w in words) or joined in names:
                category, by_word = found, True
                break
    if category is None:
        return None
    last = words[-1]
    if last in r.about_words:
        return None
    if last in r.key_words and category not in r.kept_with_key_word:
        return None
    if category == 'name':
        others = [w for w in words if w not in r.name_words]
        if any(_is_thing(w) for w in others):
            return None                                 # plan_name, branch_name
        if not others and table_words and any(_is_thing(w) for w in table_words):
            return None                                 # a bare "name": depends on what the table holds
        if by_word and others and _names_its_table(others, table_words):
            return None                                 # dealer_name in sal_dealer: the name of what the table holds
    return category


# -------------------------------------------------------------- by values ----

def _text(value):
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        value = int(value)
    return str(value).strip()


def normalise(text, how):
    """A value as a rule's patterns see it (spaces, dashes, brackets, '+966 0', '00966' ...)."""
    remove, replace = RULES.normalisers[how]
    if remove is not None:
        text = remove.sub('', text)
    for pattern, new in replace:
        text = pattern.sub(new, text, count=1)
    return text


def _rule_applies(rule, column, family):
    if family and family in rule['not_for_families']:
        return False
    name = str(column or '').lower()
    if name and rule['not_for_name_endings'] and name.endswith(rule['not_for_name_endings']):
        return False
    return True


def personal_by_values(values, column=None, family=None):
    """The category the values put a column in, or None. `values` is a sample of the column.

    column, family: the column's name and type family (number / text / date ...). Some rules are not
    applied to some columns: ten-digit numbers in a timestamp column are seconds, not national IDs.
    """
    r = RULES
    texts = [t for t in (_text(v) for v in values) if t]
    if len(texts) < r.min_values:
        return None
    for rule in r.value_rules:
        if not _rule_applies(rule, column, family):
            continue
        hits = 0
        for text in texts:
            seen = normalise(text, rule['normalise'])
            if any(pattern.fullmatch(seen) for _, pattern in rule['patterns']):
                hits += 1
        if hits >= r.min_values and hits >= rule['share'] * len(texts):
            return rule['category']
    return None


def value_kind(value):
    """The label of the first value pattern one value matches ('Saudi mobile', 'e-mail address' ...), or None."""
    text = _text(value)
    for rule in RULES.value_rules:
        seen = normalise(text, rule['normalise'])
        for label, pattern in rule['patterns']:
            if pattern.fullmatch(seen):
                return label
    return None


# ----------------------------------------------------------------- masking ----

MAX_STARS = 12
HIDDEN = '*****'                # a value of which nothing may show, not even its length (pay)
EMAIL = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?')


def is_date_like(text):
    """True for text that reads as a date or a date with a time, in any of the usual spellings."""
    text = text.strip()
    return bool(text) and any(pattern.fullmatch(text) for pattern in RULES.date_like)


def _hide_all(text):
    """Every letter and digit hidden; the punctuation stays, so the shape is all that is left."""
    return re.sub(r'\w', '*', text, flags=re.UNICODE)


def mask(value, category=None):
    """A value with everything but its first and last characters hidden. None stays None.

    Short values (four characters or fewer), birth dates (category 'birth_date') and any text
    that looks like a date are hidden completely. A value of a category that hides its length too
    (pay: category 'salary') is always '*****': 9,000 and 185,000 look the same.
    """
    if value is None:
        return None
    if category in RULES.length_hidden_categories:
        return HIDDEN
    if isinstance(value, datetime):
        return '****-**-** **:**'
    if isinstance(value, date):
        return '****-**-**'
    if isinstance(value, time):
        return '**:**'
    text = value if isinstance(value, str) else _text(value) or str(value)
    stripped = text.strip()
    if ISO_DATE.fullmatch(stripped):                      # a date kept as text
        return '****-**-**' if len(stripped) == 10 else '****-**-** **:**'
    if category in RULES.fully_masked_categories or is_date_like(stripped):
        return _hide_all(text)
    if EMAIL.match(stripped):
        local, _, domain = stripped.rpartition('@')
        host, dot, tld = domain.rpartition('.')
        return f'{local[:1]}***@{host[:1]}***{dot}{tld}'
    n = len(text)
    if n <= RULES.fully_masked_up_to:
        return '*' * n
    if n <= 6:
        return text[0] + '*' * (n - 2) + text[-1]
    return text[:2] + '*' * min(n - 4, MAX_STARS) + text[-2:]


def is_masked(text):
    """True for a string that mask() could have produced (used to check what is stored)."""
    return isinstance(text, str) and '*' in text


def mask_stats(stats, category):
    """Hide the values in one column's statistics, in place: what may be saved and shown for a
    personal column. Counts stay (rows, empty, distinct); the values do not.

    The average and the histogram go for every category: the average of phone numbers means nothing,
    and a histogram's edges are the real minimum and maximum. Minimum, maximum and top values are
    masked one by one. For a category that hides even the length of its values (pay) the top values
    and the text lengths go as well: a list of identical masks says nothing but how often a salary
    occurs, and a length says how large it is.
    """
    stats.pop('avg', None)
    stats.pop('histogram', None)
    for key in ('min', 'max'):
        if stats.get(key) is not None:
            stats[key] = mask(stats[key], category)
    if category in RULES.length_hidden_categories:
        for key in ('top', 'top_error', 'min_length', 'max_length'):
            stats.pop(key, None)
        stats['top_hidden'] = True
    elif stats.get('top'):
        stats['top'] = [[mask(v, category), n] for v, n in stats['top']]
    return stats
