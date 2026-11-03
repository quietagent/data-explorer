"""The report: one self-contained HTML file, and the same content as an Excel workbook.

    Report.collect()   everything the report says, as plain data
    Report.html()      -> (file name, bytes): one file, no reference to anything outside it
    Report.xlsx()      -> (file name, bytes): the same sections as sheets (openpyxl)

Both are built in memory and handed to the browser as a download; nothing is written into the
project folder or anywhere else.

A report is ALWAYS masked. It is made to be passed on, so it does not look at the "show personal
values" switch at all: it is built only from what is saved - where the values of personal columns
are masked already - and from the structure. It reads no data from any source, and holds no
sample rows. A column that counts as personal now but was saved before it did is masked here.

Scope: the whole workspace, or the same view as on screen (the relationship kinds, the schema
filter, N hops around the focused table).
"""

from __future__ import annotations

import base64
import html
import io
import re
from datetime import datetime

from .. import config, export
from ..model import RANK, relationship_kinds
from ..profiling.personal import CATEGORY_TEXT, mask
from ..profiling.quality import RULES, SEVERITIES
from ..profiling.profiler import size_text
from ..profiling.queries import BLOCK_SAMPLE
from .relations import fraction_text

IMAGE_MAX_PIXELS = 16_000_000       # the ER image of a report: when the view needs more, it is left out
IMAGE_MIN_DPI = 55                  # below this the names in the image cannot be read
TOP_SHOWN = 5                       # most frequent values listed per column
PROFILE_WIDTHS = ('20%', '12%', '11%', '7%', '12%', '12%', '26%')      # the column tables of the HTML report
RELATION_WIDTHS = ('25%', '10%', '8%', '9%', '9%', '10%', '29%')
SEVERITY_MARK = {'high': '▲ High', 'medium': '◆ Medium', 'low': '● Low'}
ILLEGAL_XML = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _percent(share, digits=1):
    if share is None:
        return ''
    text = f'{100.0 * share:.{digits}f}'
    return (text.rstrip('0').rstrip('.') if '.' in text else text) + '%'


def _kind_share(kind, share):
    """How sure an inferred link is, as the Relations tab prints it: a link found by values its share as
    measured (98.8%, by the rule of fraction_text), the other kinds a whole percent."""
    return fraction_text(share) if kind == 'values' else f'{share:.0%}'


def _rows(count, kind):
    if count is None:
        return ''
    return {'estimate': f'~{count:,}', 'more_than': f'>{count:,}'}.get(kind, f'{count:,}')


def _count_note(kind):
    return {'estimate': "an estimate from the database's own statistics", 'more_than': 'at least this many: the read stopped there',
            'exact': 'counted'}.get(kind, '')


class Report:
    def __init__(self, workspaces, profiling, relations):
        self.ws, self.prof, self.relations = workspaces, profiling, relations

    # ------------------------------------------------------------- collecting
    def collect(self, workspace_id, scope='all', focus=None, hops=1, kinds=None, min_confidence=0.0, schema=None):
        """Everything the report says. Only saved, masked statistics and structure are read."""
        workspace = self.ws.get_workspace(workspace_id)
        model = self.ws.model(workspace_id)
        whole = scope != 'view'
        focus_table = None
        if not whole and focus:
            focus_table = model.find(focus)
        if whole:
            tables, relationships = list(model.tables), list(model.relationships)
            scope_text = 'the whole workspace'
        else:
            tables, relationships = model.view(
                kinds=None if kinds is None else {k for k in kinds if k in RANK}, min_confidence=min_confidence,
                schemas=None if schema is None else [schema], focus=focus_table, hops=max(1, int(hops)))
            parts = []
            if focus_table is not None:
                parts.append(f'{focus_table.name} and what is within {hops} hop{"s" if int(hops) > 1 else ""} of it')
            if schema is not None:
                parts.append(f'schema {schema or "(none)"}')
            chosen = None if kinds is None else [k for k in kinds if k in RANK]
            if chosen is not None and set(chosen) != set(RANK):     # every kind ticked is no restriction
                labels = {kind['id']: kind['label'] for kind in relationship_kinds()}
                parts.append('relationships: ' + (', '.join(labels[k] for k in chosen) or 'none'))
            if min_confidence:
                parts.append(f'confidence from {min_confidence:.0%}')
            scope_text = 'the current view' + (' - ' + '; '.join(parts) if parts else '')
        keys = {t.key for t in tables}
        saved = self.prof.store.tables(workspace_id)
        is_personal = self.prof.is_personal(workspace_id)

        table_rows, profiles, sampling = [], [], []
        for table in sorted(tables, key=lambda t: t.id.lower()):
            row = saved.get(table.key) or {}
            profile = row.get('profile') or {}
            attempt = row.get('attempt') or {}
            status = attempt.get('status') or ('profiled' if row.get('profiled_at') else None)
            table_rows.append({
                'id': table.id, 'schema': table.schema or '', 'kind': table.kind,
                'from': ', '.join(dict.fromkeys(o.label or o.kind for o in table.origins)),
                'row_count': row.get('row_count'), 'row_count_kind': row.get('row_count_kind'),
                'rows_text': _rows(row.get('row_count'), row.get('row_count_kind')),
                'columns': len(table.columns), 'profiled_at': row.get('profiled_at'),
                'status': {'profiled': 'profiled', 'timed_out': 'timed out', 'failed': 'failed'}.get(status, 'not profiled'),
                'sampled': bool(profile.get('sampled')), 'sample_method': profile.get('sample_method'),
                'rows_profiled': profile.get('rows_profiled')})
            if profile.get('sampled'):
                of = _rows(row.get('row_count'), row.get('row_count_kind'))
                sampling.append({'table': table.id, 'note': f'profiled on {profile.get("rows_profiled") or 0:,} rows'
                                 + (f' of {of}' if of else '') + f' - {profile.get("sample_method")}'})
            for note in self.prof.notes(row):            # without "how many is not known" once the rows are counted
                sampling.append({'table': table.id, 'note': note})
            if attempt:
                sampling.append({'table': table.id, 'note': f'the last profile did not finish: {attempt.get("message")}'})
            stats = self.prof.masked_columns(workspace_id, table)       # masked, whatever the switch says
            columns = []
            for c in table.columns:
                st = stats.get(c.name.upper()) or {}
                category = is_personal(table, c.name, st)
                rows, nulls = st.get('rows'), st.get('nulls')
                top = [(str(v), n) for v, n in (st.get('top') or [])[:TOP_SHOWN]]
                columns.append({
                    'name': c.name, 'type': c.type or '',
                    'key': ' '.join(k for k in ('PK' if c.pk else 'PK?' if c.pk_inferred else '',
                                                'FK' if c.fk else 'FK?' if c.fk_inferred else '') if k),
                    'personal': bool(category),
                    'personal_reason': ('marked by you' if category == 'user' else CATEGORY_TEXT.get(category, category)) if category else '',
                    'profiled': bool(st), 'rows': rows, 'nulls': nulls,
                    'null_share': (nulls / rows) if rows and nulls is not None else None,
                    'distinct': st.get('distinct'), 'unique': bool(st.get('unique')),
                    'min': st.get('min'), 'max': st.get('max'), 'avg': st.get('avg'),
                    'min_length': st.get('min_length'), 'max_length': st.get('max_length'), 'top': top,
                    'note': st.get('note') or st.get('error') or ''})
            profiles.append({'table': table.id, 'rows_text': table_rows[-1]['rows_text'], 'status': table_rows[-1]['status'],
                             'profiled_at': row.get('profiled_at'), 'sampled': bool(profile.get('sampled')),
                             'sample_method': profile.get('sample_method'), 'rows_profiled': profile.get('rows_profiled'),
                             'columns': columns})

        findings = [f for f in self.prof.quality(workspace_id)['findings'] if model.find(f['table']) is not None
                    and model.find(f['table']).key in keys]
        view = self.relations.overview(workspace_id)
        shown = {self.relations.key_of(r) for r in relationships}
        checks = []
        for r in view['relationships']:
            if r['key'] not in shown:
                continue
            check = dict(r['check'] or {})
            # What the Relations tab prints for this row (relations.shown): the report prints the same texts.
            printed = check.get('shown') or {}
            if check.get('sampled') and BLOCK_SAMPLE in str(check.get('sample_method') or ''):
                # a relationship check reads its own sample of the child: a block sample is said here too
                sampling.append({'table': r['from'], 'note': f'the check of {", ".join(r["from_columns"])} -> {r["to"]} was '
                                 f'measured on {check.get("rows") or 0:,} rows - {check["sample_method"]}'})
            elif BLOCK_SAMPLE in str(check.get('child_sample_method') or ''):
                # counted in the database over the whole table; only "how often a key repeats" rests on the sample
                sampling.append({'table': r['from'], 'note': f'the check of {", ".join(r["from_columns"])} -> {r["to"]}: how '
                                 f'often a key repeats in the child was judged on a sample - {check["child_sample_method"]}'})
            child = model.find(r['from'])
            categories = [is_personal(child, c) if child is not None else None for c in r['from_columns']]
            examples = []
            for example in check.get('orphan_examples') or []:
                # masked when it was saved; masked here too if its column counts as personal only now
                key = [mask(v, category) if category and v is not None and '*' not in str(v) else v
                       for v, category in zip(example['key'], categories)]
                examples.append((' / '.join('' if v is None else str(v) for v in key), example['rows']))
            checks.append({
                'from': r['from'], 'from_columns': ', '.join(r['from_columns']), 'to': r['to'],
                'to_columns': ', '.join(r['to_columns']), 'kind': r['kind'], 'confidence': r['confidence'],
                'status': check.get('status') or 'never checked', 'label': check.get('label') or '',
                'coverage': check.get('coverage'), 'orphan_rows': check.get('orphan_rows'),
                'orphan_keys': check.get('orphan_keys'), 'null_share': check.get('null_share'),
                'rows': check.get('rows'), 'rows_checked': check.get('rows_checked'), 'null_rows': check.get('null_rows'),
                # as the tab prints them: ">99.9%", "≥ 40", and what was measured when a count is a lower bound
                'coverage_text': printed.get('coverage') or '', 'orphans_text': printed.get('orphans') or '',
                'empty_text': printed.get('empty_keys') or '', 'exact': printed.get('exact', True), 'tag': printed.get('tag') or '',
                'limit_notes': list(printed.get('notes') or []), 'how': printed.get('how') or '',
                'cardinality': check.get('cardinality') or r['model_cardinality'],
                'proven': check.get('cardinality_proven'), 'cardinality_note': check.get('cardinality_note') or '',
                'parent_key': check.get('parent_unique_how') or '', 'sampled': bool(check.get('sampled')),
                'checked_at': check.get('checked_at'), 'examples': examples})
        accepted = [x for x in view['accepted'] if model.find(x['from']) is not None and model.find(x['from']).key in keys]

        notes = [
            f'A database table with up to {config.SAMPLE_TARGET_ROWS:,} rows is read whole; a bigger one is profiled on a '
            f'sample of about {config.SAMPLE_TARGET_ROWS:,} rows (Oracle SAMPLE - rows, PostgreSQL and SQL Server TABLESAMPLE - '
            f'blocks or pages; SQLite: short stretches of rows spread over the whole table; other engines, views and '
            f'SQLite tables without row numbers: the first rows. The last two are not random samples).',
            f'An Oracle table larger than {size_text(config.ORACLE_ROW_SAMPLE_MAX_BYTES)} is sampled by whole blocks '
            f'(SAMPLE BLOCK). That is a {BLOCK_SAMPLE}; the tables it was used on are listed below.',
            'On a sample, null percentages are estimates, and distinct counts, top values and duplicates are those of the sample.',
            'Row counts: "~" is an estimate from the database\'s own statistics; ">" means the table has at least that many '
            'rows and was not counted further.',
            'Relationship checks: a relationship between two tables of the same database is counted by the database itself, '
            'over the whole child table. Otherwise it is measured on the keys the app read - a sample of a big table, and at '
            f'most {config.RELATION_MAX_KEYS:,} keys - and such a result is a lower bound: its row says "≥" and what was measured.',
            'Values of personal columns are masked in this report. The report is always masked.',
        ]
        profiled = [r for r in table_rows if r['profiled_at']]
        dates = sorted(r['profiled_at'] for r in profiled)
        by_kind = {kind['id']: 0 for kind in relationship_kinds()}
        for r in relationships:
            by_kind[r.source] = by_kind.get(r.source, 0) + 1
        image, image_note = self._image(model, tables, relationships, workspace['name'])
        return {
            'title': f'{workspace["name"]} - data report', 'workspace': workspace['name'],
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'), 'scope': scope_text, 'whole': whole,
            'app': f'{config.APP_NAME} {config.APP_VERSION}',
            'sources': [{'label': s['label'], 'kind': {'database': 'live database', 'sql_files': 'SQL files',
                                                       'files': 'CSV / Excel files'}.get(s['kind'], s['kind']),
                         'tables': s['tables'], 'relationships': s['relationships'], 'read_at': s['refreshed_at']}
                        for s in workspace['sources']],
            'counts': {'tables': len(tables), 'columns': sum(len(t.columns) for t in tables),
                       'relationships': len(relationships), 'profiled': len(profiled),
                       'checked': sum(1 for c in checks if c['status'] == 'checked'), 'findings': len(findings),
                       'accepted': len(accepted), 'sampled': sum(1 for r in table_rows if r['sampled'])},
            'by_kind': by_kind, 'kinds': relationship_kinds(),
            'profiled_from': dates[0] if dates else None, 'profiled_to': dates[-1] if dates else None,
            'image': image, 'image_note': image_note, 'tables': table_rows, 'profiles': profiles,
            'findings': findings, 'findings_by_severity': {s: sum(1 for f in findings if f['severity'] == s) for s in SEVERITIES},
            'rules': RULES, 'checks': checks, 'accepted': accepted, 'sampling': sampling, 'notes': notes,
        }

    @staticmethod
    def _image(model, tables, relationships, name):
        """The ER diagram of the scope as PNG bytes, or (None, why not)."""
        if not tables:
            return None, 'There are no tables in this view.'
        try:
            return export.to_png(model, tables, relationships, name, max_pixels=IMAGE_MAX_PIXELS, min_dpi=IMAGE_MIN_DPI), None
        except export.TooBigForPng:
            return None, (f'The diagram of {len(tables):,} tables is too large for one readable image. Use the current view '
                          f'(focus mode or the schema filter) for the report, or export the diagram as .drawio.')
        except export.NothingToExport as exc:
            return None, str(exc)

    # ------------------------------------------------------------------ HTML
    def html(self, workspace_id, **scope):
        data = self.collect(workspace_id, **scope)
        return export.file_name(data['workspace'], 'report') + '.html', render_html(data).encode('utf-8')

    def xlsx(self, workspace_id, **scope):
        data = self.collect(workspace_id, **scope)
        return export.file_name(data['workspace'], 'report') + '.xlsx', render_xlsx(data)


# ----------------------------------------------------------------------- HTML ----

CSS = '''
/* A report is made to print and to pass on: it is light whatever theme the app or the reader's PC uses. */
:root { color-scheme: only light;
        --ink: #0f172a; --ink2: #334155; --muted: #64748b; --line: #dde3ea; --soft: #f4f6f9; --accent: #1d4ed8;
        --accent-soft: #dbeafe; --warn: #b45309; --warn-soft: #fef3c7; --danger: #b91c1c; }
* { box-sizing: border-box; }
body { margin: 0; font: 13px/1.5 "Segoe UI", system-ui, -apple-system, Arial, sans-serif; color: var(--ink); background: #fff; }
.page { max-width: 1120px; margin: 0 auto; padding: 28px 28px 60px; }
header h1 { margin: 0; font-size: 24px; letter-spacing: -.2px; }
header .sub { color: var(--muted); margin-top: 4px; }
.masked { display: inline-block; margin-top: 10px; padding: 4px 10px; border-radius: 999px; background: var(--warn-soft);
          color: #7c2d12; font-weight: 600; font-size: 12px; }
nav { margin: 18px 0 6px; padding: 10px 14px; background: var(--soft); border-radius: 8px; }
nav a { color: var(--accent); text-decoration: none; margin-right: 16px; white-space: nowrap; }
h2 { font-size: 17px; margin: 34px 0 10px; padding-bottom: 6px; border-bottom: 2px solid var(--ink); }
h3 { font-size: 14px; margin: 22px 0 4px; }
.meta { color: var(--muted); font-size: 12px; margin-bottom: 6px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 10px; margin: 14px 0; }
.tile { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.tile b { display: block; font-size: 22px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.tile span { color: var(--muted); font-size: 12px; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 4px; }
table.fixed { table-layout: fixed; }
th { text-align: left; font-size: 10.5px; letter-spacing: .4px; text-transform: uppercase; color: var(--muted);
     padding: 5px 8px 5px 0; border-bottom: 1px solid var(--line); white-space: nowrap; }
td { padding: 4px 8px 4px 0; border-bottom: 1px solid #eef2f6; vertical-align: top; overflow-wrap: anywhere; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.mono { font-family: Consolas, "Cascadia Mono", "Courier New", monospace; font-size: 11.5px; color: var(--ink2); }
.strong { font-weight: 600; }
.nowrap { white-space: nowrap; }
.muted { color: var(--muted); }
.tag { display: inline-block; padding: 0 6px; border-radius: 4px; font-size: 10.5px; font-weight: 700; background: #eef2f6;
       color: var(--ink2); white-space: nowrap; }
.tag.personal { background: var(--warn-soft); color: #92400e; }
.tag.sample { background: var(--accent-soft); color: #1e3a8a; }
.tag.key { background: #fef3c7; color: #92400e; }
.limit { color: #92400e; font-size: 12px; }
.bar { display: inline-block; width: 60px; height: 6px; border-radius: 3px; background: var(--accent-soft); vertical-align: middle;
       margin-right: 6px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); }
.bar.warn { background: #fde9c8; } .bar.warn i { background: var(--warn); }
.sev-high { color: var(--danger); font-weight: 700; } .sev-medium { color: var(--warn); font-weight: 700; }
.sev-low { color: var(--muted); font-weight: 700; }
.figure { border: 1px solid var(--line); border-radius: 8px; padding: 10px; text-align: center; overflow: auto; }
.figure img { max-width: 100%; height: auto; }
.callout { padding: 8px 12px; border-radius: 6px; background: var(--soft); border: 1px solid var(--line); color: var(--ink2); }
ul.notes { margin: 6px 0; padding-left: 18px; } ul.notes li { margin: 3px 0; }
.block { break-inside: avoid; }
footer { margin-top: 40px; padding-top: 10px; border-top: 1px solid var(--line); color: var(--muted); font-size: 12px; }
@media print { .page { padding: 0; max-width: none; } nav { display: none; } h2 { break-after: avoid; } }
'''


def e(value):
    return html.escape('' if value is None else str(value))


def _number(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return f'{value:,}'
    if isinstance(value, float):
        return f'{value:,.3f}'.rstrip('0').rstrip('.')
    return str(value)


def _table(headers, rows, numeric=(), widths=None):
    """widths: one CSS width per column - the columns then line up from one table to the next."""
    out = ['<table class="fixed"><colgroup>' + ''.join(f'<col style="width:{w}">' for w in widths) + '</colgroup><thead><tr>'
           if widths else '<table><thead><tr>']
    out += [f'<th class="{"num" if i in numeric else ""}">{e(h)}</th>' for i, h in enumerate(headers)]
    out.append('</tr></thead><tbody>')
    for row in rows:
        out.append('<tr>' + ''.join(f'<td class="{"num" if i in numeric else ""}">{cell}</td>' for i, cell in enumerate(row)) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def _bar(share):
    if share is None:
        return '<span class="muted">-</span>'
    width = max(2 if share else 0, round(100 * share))
    return f'<span class="bar{" warn" if share > 0.5 else ""}"><i style="width:{width}%"></i></span>{e(_percent(share))}'


def render_html(d):
    """The report as one HTML document: inline styles, the image as a data: URI, no script, no link."""
    c = d['counts']
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f'<title>{e(d["title"])}</title><style>{CSS}</style></head><body><div class="page">',
           f'<header><h1>{e(d["title"])}</h1>',
           f'<div class="sub">Generated {e(d["generated_at"])} · scope: {e(d["scope"])} · {e(d["app"])}</div>',
           '<div class="masked">Personal values are masked in this report</div></header>',
           '<nav><a href="#summary">Summary</a><a href="#diagram">ER diagram</a><a href="#tables">Tables</a>'
           '<a href="#profiles">Column profiles</a><a href="#quality">Quality findings</a>'
           '<a href="#relations">Relationship checks</a><a href="#sampling">Notes on sampling</a></nav>']

    # summary
    out.append('<h2 id="summary">Summary</h2><div class="tiles">')
    for value, label in [(len(d['sources']), 'sources'), (c['tables'], 'tables'), (c['columns'], 'columns'),
                         (c['relationships'], 'relationships'), (c['profiled'], 'tables profiled'),
                         (c['checked'], 'relationships checked'), (c['findings'], 'quality findings')]:
        out.append(f'<div class="tile"><b>{value:,}</b><span>{e(label)}</span></div>')
    out.append('</div>')
    kinds = ' · '.join(f'{d["by_kind"].get(k["id"], 0):,} {e(k["label"])}' for k in d['kinds'])
    out.append(f'<p class="meta">Relationships: {kinds}.')
    if d['profiled_from']:
        same = d['profiled_from'][:10] == d['profiled_to'][:10]
        out.append(f' Profiles read {e(d["profiled_from"][:10])}' + ('' if same else f' to {e(d["profiled_to"][:10])}') + '.')
    out.append('</p>')
    out.append(_table(['Source', 'Kind', 'Tables', 'Links', 'Read'], [
        [f'<span class="strong">{e(s["label"])}</span>', e(s['kind']), f'{s["tables"]:,}', f'{s["relationships"]:,}',
         e((s['read_at'] or '').replace('T', ' '))] for s in d['sources']], numeric=(2, 3)))

    # diagram
    out.append('<h2 id="diagram">ER diagram</h2>')
    if d['image']:
        encoded = base64.b64encode(d['image']).decode('ascii')
        out.append(f'<div class="figure"><img alt="Entity relationship diagram of {e(d["scope"])}" '
                   f'src="data:image/png;base64,{encoded}"></div>'
                   '<p class="meta">Solid line: declared key. Dashed: inferred. Dotted: accepted from value overlap. '
                   'Key columns only.</p>')
    else:
        out.append(f'<div class="callout">{e(d["image_note"])}</div>')

    # tables
    out.append('<h2 id="tables">Tables</h2>')
    out.append(_table(['Table', 'From', 'Rows', 'Columns', 'Profile', ''], [
        [f'<a href="#t-{i}" class="strong">{e(t["id"])}</a>' + (' <span class="muted">view</span>' if t['kind'] == 'view' else ''),
         e(t['from']), e(t['rows_text']), f'{t["columns"]:,}',
         e(t['status'] + (' ' + t['profiled_at'][:10] if t['profiled_at'] else '')),
         f'<span class="tag sample">sample of {t["rows_profiled"] or 0:,}</span>' if t['sampled'] else '']
        for i, t in enumerate(d['tables'])], numeric=(2, 3)))

    # profiles
    out.append('<h2 id="profiles">Column profiles</h2>')
    if not d['profiles']:
        out.append('<p class="muted">No tables.</p>')
    for i, p in enumerate(d['profiles']):
        meta = [f'{len(p["columns"])} columns']
        if p['rows_text']:
            meta.insert(0, f'{p["rows_text"]} rows')
        meta.append(p['status'] + (f' {p["profiled_at"][:16].replace("T", " ")}' if p['profiled_at'] else ''))
        if p['sampled']:
            meta.append(f'sampled: {p["rows_profiled"] or 0:,} rows, {p["sample_method"]}')
        out.append(f'<div class="block"><h3 id="t-{i}">{e(p["table"])}</h3><div class="meta">{e(" · ".join(meta))}</div>')
        rows = []
        for col in p['columns']:
            name = f'<span class="strong">{e(col["name"])}</span>'
            if col['key']:
                name += f' <span class="tag key">{e(col["key"])}</span>'
            if col['personal']:
                name += f' <span class="tag personal" title="{e(col["personal_reason"])}">personal</span>'
            top = ', '.join(f'{e(v)} ({n:,})' for v, n in col['top'])
            if col['unique']:
                top = '<span class="muted">every value different</span>'
            if col['note']:
                top = f'<span class="muted">{e(col["note"])}</span>'
            rows.append([name, f'<span class="mono">{e(re.sub(r",\s+", ",", col["type"]))}</span>',
                         _bar(col['null_share']) if col['profiled'] else '<span class="muted">not profiled</span>',
                         e(_number(col['distinct'])), f'<span class="mono">{e(_number(col["min"]))}</span>',
                         f'<span class="mono">{e(_number(col["max"]))}</span>', top])
        out.append(_table(['Column', 'Type', 'Empty', 'Distinct', 'Min', 'Max', 'Most frequent values'], rows, numeric=(3,),
                          widths=PROFILE_WIDTHS))
        out.append('</div>')

    # quality
    out.append('<h2 id="quality">Quality findings</h2>')
    if d['findings']:
        counts = ' · '.join(f'{d["findings_by_severity"][s]:,} {s}' for s in SEVERITIES)
        out.append(f'<p class="meta">{e(counts)}. Worked out from the saved statistics.</p>')
        out.append(_table(['Severity', 'Table', 'Column', 'Rule', 'Finding'], [
            [f'<span class="sev-{e(f["severity"])}">{e(SEVERITY_MARK.get(f["severity"], f["severity"]))}</span>',
             f'<span class="strong">{e(f["table"])}</span>', f'<span class="mono">{e(f["column"] or "")}</span>',
             e(d['rules'].get(f['rule'], f['rule'])),
             e(f['message']) + (f' <span class="tag sample" title="{e(f.get("sample_method") or "on a sample")}">sample</span>'
                                if f['sampled'] else '')] for f in d['findings']]))
    else:
        out.append('<p class="muted">No findings' + ('.' if c['profiled'] else ': no table has been profiled yet.') + '</p>')

    # relationships
    out.append('<h2 id="relations">Relationship checks</h2>')
    if d['checks']:
        rows = []
        for r in d['checks']:
            link = (f'<span class="strong">{e(r["from"])}</span> <span class="mono">({e(r["from_columns"])})</span> → '
                    f'<span class="strong">{e(r["to"])}</span> <span class="mono">({e(r["to_columns"])})</span>')
            kind = f'<span class="nowrap">{e(r["kind"])}</span>' + (
                f' {e(_kind_share(r["kind"], r["confidence"]))}' if r['confidence'] is not None else '')
            if r['status'] != 'checked':
                rows.append([link, kind, f'<span class="muted">{e(r["label"] or r["status"])}</span>', '', '', '', ''])
                continue
            if r['tag']:                                # a lower bound: said in the row, as in the Relations tab
                link += f'<div class="limit"><span class="tag personal">not the whole table</span> {e(r["tag"])}</div>'
            evidence = e(r['parent_key']) + ('; ' + e(r['cardinality_note']) if r['cardinality_note'] else '')
            if r['examples']:
                shown = ', '.join(f'{e(k)} ({n:,})' for k, n in r['examples'][:8])
                evidence += f'<br><span class="muted">keys without a parent: {shown}' + (' …' if len(r['examples']) > 8 else '') + '</span>'
            evidence += f'<br><span class="muted">{e(r["label"])}' + (f'. {e(r["how"])}.' if r['how'] else '') + '</span>'
            for note in r['limit_notes']:
                evidence += f'<br><span class="limit">{e(note)}</span>'
            cardinality = e(r['cardinality']) + ('' if r['proven'] else ' <span class="muted">(not proven)</span>')
            rows.append([link, kind, e(r['coverage_text']), e(r['orphans_text']), e(r['empty_text']), cardinality, evidence])
        out.append(_table(['Relationship', 'Kind', 'Coverage', 'Orphan rows', 'Empty keys', 'Cardinality', 'Evidence'], rows,
                          numeric=(2, 3, 4), widths=RELATION_WIDTHS))
    else:
        out.append('<p class="muted">No relationships in this scope.</p>')
    out.append('<h3>Links accepted from value overlap</h3>')
    if d['accepted']:
        out.append(_table(['From', 'To', 'Overlap', 'Evidence', 'Accepted'], [
            [f'<span class="strong">{e(x["from"])}</span> <span class="mono">({e(", ".join(x["from_columns"]))})</span>',
             f'<span class="strong">{e(x["to"])}</span> <span class="mono">({e(", ".join(x["to_columns"]))})</span>',
             e(x.get('overlap_text') or _percent(x['confidence'])),
             e((x['evidence'] or {}).get('text') or '') + ' · ' + e((x['evidence'] or {}).get('label') or ''),
             e((x['decided_at'] or '')[:10])] for x in d['accepted']], numeric=(2,)))
    else:
        out.append('<p class="muted">None.</p>')

    # sampling
    out.append('<h2 id="sampling">Notes on sampling</h2><ul class="notes">')
    out += [f'<li>{e(note)}</li>' for note in d['notes']]
    out.append('</ul>')
    if d['sampling']:
        out.append(_table(['Table', 'Note'], [[f'<span class="strong">{e(s["table"])}</span>', e(s['note'])] for s in d['sampling']]))
    else:
        out.append('<p class="muted">No table of this report was profiled on a sample.</p>')
    out.append(f'<footer>{e(d["app"])} · made on this PC from saved statistics · personal values masked</footer></div></body></html>')
    return ''.join(out)


# ---------------------------------------------------------------------- Excel ----

def render_xlsx(d):
    """The report as an Excel workbook, in memory. Text is always written as text, never as a formula."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    head_font, head_fill = Font(bold=True, color='FFFFFF'), PatternFill('solid', fgColor='1D4ED8')

    def put(ws, row, column, value):
        if isinstance(value, str):
            value = ILLEGAL_XML.sub('', value)[:32000]
        cell = ws.cell(row=row, column=column, value=value)
        if isinstance(value, str) and value[:1] in ('=', '+', '-', '@'):
            cell.data_type = 's'                    # a value from the data must never become a formula
        return cell

    def sheet(title, headers, rows, widths=None):
        ws = wb.create_sheet(title)
        for i, h in enumerate(headers, 1):
            cell = put(ws, 1, i, h)
            cell.font, cell.fill = head_font, head_fill
        for r, row in enumerate(rows, 2):
            for i, value in enumerate(row, 1):
                put(ws, r, i, value)
        ws.freeze_panes = 'A2'
        if rows:
            ws.auto_filter.ref = f'A1:{get_column_letter(len(headers))}{len(rows) + 1}'
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(row[i - 1])) for row in rows[:400] if row[i - 1] is not None])
            ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(h, min(60, max(10, longest + 2)))
        return ws

    c = d['counts']
    summary = wb.active
    summary.title = 'Summary'
    lines = [('Report', d['title']), ('Generated', d['generated_at']), ('Scope', d['scope']), ('Made with', d['app']),
             ('Personal values', 'masked - a report is always masked'), ('', ''),
             ('Sources', len(d['sources'])), ('Tables', c['tables']), ('Columns', c['columns']),
             ('Relationships', c['relationships'])]
    lines += [(f'  {k["label"]}', d['by_kind'].get(k['id'], 0)) for k in d['kinds']]
    lines += [('Tables profiled', c['profiled']), ('Tables profiled on a sample', c['sampled']),
              ('Profiles read from', (d['profiled_from'] or '').replace('T', ' ')),
              ('Profiles read to', (d['profiled_to'] or '').replace('T', ' ')),
              ('Relationships checked', c['checked']), ('Links accepted from value overlap', c['accepted']),
              ('Quality findings', c['findings'])]
    lines += [(f'  {s}', d['findings_by_severity'][s]) for s in SEVERITIES]
    for r, (key, value) in enumerate(lines, 1):
        put(summary, r, 1, key).font = Font(bold=True)
        put(summary, r, 2, value)
    start = len(lines) + 2
    for i, h in enumerate(['Source', 'Kind', 'Tables', 'Links', 'Read'], 1):
        cell = put(summary, start, i, h)
        cell.font, cell.fill = head_font, head_fill
    for r, s in enumerate(d['sources'], start + 1):
        for i, value in enumerate([s['label'], s['kind'], s['tables'], s['relationships'], (s['read_at'] or '').replace('T', ' ')], 1):
            put(summary, r, i, value)
    summary.column_dimensions['A'].width, summary.column_dimensions['B'].width = 36, 48

    sheet('Tables', ['Table', 'Schema', 'Kind', 'From', 'Rows', 'Row count is', 'Columns', 'Profile', 'Profiled at',
                     'Sampled', 'Sample method', 'Rows profiled'], [
        [t['id'], t['schema'], t['kind'], t['from'], t['row_count'], _count_note(t['row_count_kind']), t['columns'], t['status'],
         (t['profiled_at'] or '').replace('T', ' '), 'yes' if t['sampled'] else 'no', t['sample_method'] or '', t['rows_profiled']]
        for t in d['tables']])

    rows = []
    for p in d['profiles']:
        for col in p['columns']:
            rows.append([p['table'], col['name'], col['type'], col['key'], 'yes' if col['personal'] else 'no',
                         col['personal_reason'], col['rows'], col['nulls'],
                         None if col['null_share'] is None else round(100 * col['null_share'], 2), col['distinct'],
                         'yes' if col['unique'] else '', col['min'], col['max'], col['avg'], col['min_length'],
                         col['max_length'], '; '.join(f'{v} ({n})' for v, n in col['top']), col['note']])
    sheet('Columns', ['Table', 'Column', 'Type', 'Key', 'Personal', 'Why personal', 'Rows checked', 'Empty', 'Empty %',
                      'Distinct', 'Every value different', 'Min', 'Max', 'Average', 'Shortest', 'Longest',
                      'Most frequent values', 'Note'], rows, {'Most frequent values': 60})

    sheet('Quality', ['Severity', 'Table', 'Column', 'Rule', 'Finding', 'On a sample', 'How the sample was taken'], [
        [f['severity'], f['table'], f['column'] or '', d['rules'].get(f['rule'], f['rule']), f['message'],
         'yes' if f['sampled'] else 'no', (f.get('sample_method') or '') if f['sampled'] else ''] for f in d['findings']],
          {'Finding': 80, 'How the sample was taken': 50})

    # Coverage and empty keys are the texts of the Relations tab (">99.9%"): a rounded number would say 100 next to
    # orphan rows. The counts beside them are whole numbers, to sort and to work with. So is the confidence of a
    # link found by values (its share as measured, "98.8%"); the other kinds keep a number.
    def checked(r, value):
        return value if r['status'] == 'checked' else None

    sheet('Relationships', ['Child table', 'Child columns', 'Parent table', 'Parent columns', 'Kind', 'Confidence %', 'Status',
                            'Coverage', 'Rows with a key', 'Orphan rows', 'Orphan rows are', 'Orphan keys', 'Empty keys',
                            'Rows with an empty key', 'Rows measured', 'Cardinality', 'Proven', 'Parent key', 'Evidence',
                            'Measured on', 'Not the whole table', 'Why the counts are a lower bound',
                            'Example keys without a parent', 'Checked at'], [
        [r['from'], r['from_columns'], r['to'], r['to_columns'], r['kind'],
         None if r['confidence'] is None else fraction_text(r['confidence']) if r['kind'] == 'values'
         else round(100 * r['confidence'], 1), r['label'] if r['status'] != 'checked' else 'checked',
         checked(r, r['coverage_text']), checked(r, r['rows_checked']), r['orphan_rows'],
         checked(r, 'exact' if r['exact'] else 'at least this many'), r['orphan_keys'], checked(r, r['empty_text']),
         checked(r, r['null_rows']), checked(r, r['rows']), r['cardinality'],
         '' if r['proven'] is None else ('yes' if r['proven'] else 'no'), r['parent_key'], r['cardinality_note'],
         (r['label'] + ('. ' + r['how'] if r['how'] else '')) if r['status'] == 'checked' else '', r['tag'],
         ' '.join(r['limit_notes']), '; '.join(f'{k} ({n})' for k, n in r['examples']),
         (r['checked_at'] or '').replace('T', ' ')] for r in d['checks']],
          {'Evidence': 60, 'Parent key': 44, 'Measured on': 50, 'Why the counts are a lower bound': 70})

    sheet('Accepted links', ['From table', 'From columns', 'To table', 'To columns', 'Overlap', 'Values checked',
                             'Values found', 'Evidence', 'Measured on', 'Accepted at'], [
        [x['from'], ', '.join(x['from_columns']), x['to'], ', '.join(x['to_columns']),
         x.get('overlap_text') or _percent(x['confidence']), (x['evidence'] or {}).get('values_checked'),
         (x['evidence'] or {}).get('values_found'), (x['evidence'] or {}).get('text') or '', (x['evidence'] or {}).get('label') or '',
         (x['decided_at'] or '').replace('T', ' ')] for x in d['accepted']], {'Evidence': 70})

    notes = sheet('Sampling notes', ['Table', 'Note'], [['(all)', n] for n in d['notes']] + [[s['table'], s['note']] for s in d['sampling']],
                  {'Note': 120})
    for row in notes.iter_rows(min_row=2, min_col=2, max_col=2):
        row[0].alignment = Alignment(wrap_text=True, vertical='top')

    diagram = wb.create_sheet('ER diagram')
    if d['image']:
        try:
            from openpyxl.drawing.image import Image
            picture = Image(io.BytesIO(d['image']))
            scale = min(1.0, 1400 / max(1, picture.width))
            picture.width, picture.height = int(picture.width * scale), int(picture.height * scale)
            diagram.add_image(picture, 'A3')
            put(diagram, 1, 1, f'ER diagram of {d["scope"]}. Solid: declared key; dashed: inferred; dotted: accepted from value overlap.')
        except Exception:                           # no image library: the sheet says so instead
            put(diagram, 1, 1, 'The diagram could not be placed in the workbook; it is in the HTML report.')
    else:
        put(diagram, 1, 1, d['image_note'] or 'No diagram.')
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
