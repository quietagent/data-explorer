"""Data-quality findings, worked out from the merged model and the saved statistics.

Nothing is queried here: findings are recomputed from what the last profile stored each time
they are asked for. Each finding has a severity (high / medium / low), the table, the column
(None for a finding about the whole table), the rule and a message.

Rules
    empty_table       the table has no rows
    all_null          a column has no value in any row
    constant          a column holds the same value in every row that has one
    high_null         more than HIGH_NULL_SHARE of a column's rows are empty (default 50%)
    no_primary_key    no primary key is declared, and none could be inferred from the names
                      (database and DDL tables)
    no_key_column     a file table in which no column looks like a key: none is named like one,
                      and none of the profiled columns holds a different value in every row
    duplicate_key     a column that is, or looks like, the key holds the same value more than once
    mixed_types       a text column of a file holds mostly numbers, or mostly dates

A finding never quotes a value of a personal column. Whether a column is personal is asked at the
moment the findings are worked out (the caller passes the marks as they are now), not read from
what the last profile happened to store.

Relationships are checked against the data by app.analysis.relations, not here.
"""

from __future__ import annotations

from .. import config

SEVERITIES = ('high', 'medium', 'low')
RULES = {
    'empty_table': 'Empty table',
    'all_null': 'Column with no values',
    'constant': 'Constant column',
    'high_null': 'Mostly empty column',
    'no_primary_key': 'No primary key',
    'no_key_column': 'No column looks like a key',
    'duplicate_key': 'Duplicate values in a key',
    'mixed_types': 'Mixed types in a file column',
}


def _finding(severity, table, column, rule, message, sampled=False):
    return {'severity': severity, 'table': table.id, 'column': column, 'rule': rule, 'message': message,
            'sampled': bool(sampled)}


def _share(part, whole):
    return f'{100.0 * part / whole:.1f}%'.replace('.0%', '%') if whole else '0%'


def key_column(table):
    """The single column that is, or looks like, this table's key - or None.

    A declared one-column primary key; without a declared key, the one column inferred to be it.
    """
    declared = [c for c in table.columns if c.pk]
    if declared:
        return declared[0] if len(declared) == 1 else None
    inferred = [c for c in table.columns if c.pk_inferred]
    return inferred[0] if len(inferred) == 1 else None


def looks_unique(st):
    """True for a profiled column that holds a different value in every row, and one in each."""
    rows = st.get('rows') or 0
    return bool(rows and st.get('nulls') == 0 and st.get('distinct') == rows
                and st.get('family') in ('number', 'text') and not st.get('error'))


def table_findings(table, summary, columns, high_null=None, mixed_share=None, personal=None):
    """Findings for one table.

    summary   its row of ProfileStore.tables() (or None when nothing is saved for it)
    columns   {COLUMN NAME upper: stats} from ProfileStore.columns()
    personal  function(column name, stats) -> True when the column is personal NOW; its values
              are then never quoted. Without it, what the last profile stored decides.
    """
    high_null = config.HIGH_NULL_SHARE if high_null is None else high_null
    mixed_share = config.MIXED_TYPE_SHARE if mixed_share is None else mixed_share
    personal = personal or (lambda name, st: bool(st.get('masked')))
    out = []
    profile = (summary or {}).get('profile')
    has_key = any(c.pk or c.pk_inferred for c in table.columns)
    only_files = not any(o.kind in ('db', 'ddl') for o in table.origins)
    if table.kind == 'table' and table.complete and not has_key:
        if not only_files:
            out.append(_finding('medium', table, None, 'no_primary_key',
                                'No primary key is declared, and no column looks like one by its name.'))
        elif not any(looks_unique(st) for st in columns.values()):
            # A file declares nothing, so this is a hint, not a defect.
            checked = ' or by its values' if profile else ''
            sampled = bool(profile and profile.get('sampled'))
            out.append(_finding('low', table, None, 'no_key_column',
                                f'No column looks like a key by its name{checked}.', sampled))
    exact_empty = summary and summary.get('row_count') == 0 and summary.get('row_count_kind') == 'exact'
    if exact_empty or (profile and not profile.get('sampled') and profile.get('rows_profiled') == 0):
        out.append(_finding('medium', table, None, 'empty_table', 'The table has no rows.'))
        return out
    if not profile:
        return out
    sampled = bool(profile.get('sampled'))
    where = ' in the sample' if sampled else ''
    key = key_column(table)
    file_table = any(o.kind == 'file' for o in table.origins) and (profile.get('source_kind') == 'files')
    for c in table.columns:
        st = columns.get(c.name.upper())
        if not st or st.get('error'):
            continue
        rows, nulls = st.get('rows') or 0, st.get('nulls')
        if not rows or nulls is None:
            continue
        filled = rows - nulls
        distinct = st.get('distinct')
        if filled == 0:
            out.append(_finding('medium', table, c.name, 'all_null',
                                f'No row has a value{where} ({rows:,} rows checked).', sampled))
            continue
        if nulls / rows > high_null:
            severity = 'medium' if nulls / rows >= 0.95 else 'low'
            out.append(_finding(severity, table, c.name, 'high_null',
                                f'{_share(nulls, rows)} of the rows are empty{where} ({nulls:,} of {rows:,}).', sampled))
        if distinct == 1 and rows > 1 and st.get('family') != 'lob':
            shown = ''
            if not st.get('masked') and not personal(c.name, st) and st.get('top'):
                shown = f': {str(st["top"][0][0])[:40]!r}'
            rest = ' that has one' if nulls else ''
            out.append(_finding('low', table, c.name, 'constant',
                                f'Every row{rest} holds the same value{shown}{where}.', sampled))
        if key is c and distinct is not None and distinct < filled:
            kind = 'is the primary key' if c.pk else 'looks like the key'
            out.append(_finding('high', table, c.name, 'duplicate_key',
                                f'{c.name} {kind}, but {filled - distinct:,} of its {filled:,} values repeat '
                                f'an earlier one{where}.', sampled))
        if file_table and st.get('family') == 'text' and filled:
            for what, word in (('number_like', 'numbers'), ('date_like', 'dates')):
                like = st.get(what) or 0
                if like >= mixed_share * filled and like > 0:
                    if like == filled:
                        message = f'Text column, but every value is a {word[:-1]} ({filled:,} values): stored as text.'
                    else:
                        message = (f'Text column, but {_share(like, filled)} of its values are {word}; '
                                   f'{filled - like:,} of {filled:,} are not{where}.')
                    out.append(_finding('medium', table, c.name, 'mixed_types', message, sampled))
                    break
    return out


def workspace_findings(model, tables, columns, personal=None):
    """Every finding of a workspace, most severe first.

    personal  function(table, column name, stats) -> True when the column is personal now

    A finding made on a sample carries how the sample was taken ('sample_method'), as the profile
    it comes from says it: "first 100,000 rows, not random", "1,000 stretches of 100 rows spread
    over the table, not random", "SAMPLE (5%): a random sample of rows" ...
    """
    out = []
    for table in model.tables:
        check = None if personal is None else (lambda name, st, table=table: personal(table, name, st))
        found = table_findings(table, tables.get(table.key), columns.get(table.key) or {}, personal=check)
        method = ((tables.get(table.key) or {}).get('profile') or {}).get('sample_method')
        for finding in found:
            finding['sample_method'] = method if finding['sampled'] else None
        out += found
    order = {s: i for i, s in enumerate(SEVERITIES)}
    out.sort(key=lambda f: (order[f['severity']], f['table'].lower(), (f['column'] or '').lower(), f['rule']))
    return out
