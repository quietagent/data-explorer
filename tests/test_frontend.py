"""The page's own files: what every tab needs is there, and nothing unsafe is.

These are checks on the files themselves; how the page behaves was checked in a browser
(see HANDOFF.md, "Checked by running the app").
"""

import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

STATIC = ROOT / 'static'
SCRIPTS = ('app.js', 'profile.js', 'analysis.js', 'scripts.js')


def read(name):
    return (STATIC / name).read_text(encoding='utf-8')


def test_every_tab_is_switched_on_and_has_its_view(client):
    features = client.get('/api/meta').json()['features']
    assert features == {'er_graph': True, 'profile': True, 'quality': True, 'relations': True, 'sql': True,
                        'report': True, 'scripts': True}
    page = read('index.html')
    for feature in features:
        assert f'id="view-{feature}"' in page, feature
        assert f"id: '{feature}'" in read('app.js'), feature                           # and a tab that opens it
    scripts = re.findall(r'<script src="([^"]+)"', page)
    assert scripts[-5:] == ['/static/vendor/chart.umd.min.js', '/static/app.js', '/static/profile.js',
                            '/static/analysis.js', '/static/scripts.js']
    for script in scripts:
        assert client.get(script).status_code == 200
    assert 'Planned - not built yet' in read('app.js')          # what a tab says when its feature is switched off


def test_the_profile_tab_has_its_controls():
    page = read('index.html')
    for control in ('profile-all', 'profile-selected', 'profile-counts', 'job-progress', 'job-cancel',
                    'show-personal', 'personal-banner', 'profile-clear', 'quality-table', 'quality-rule',
                    'quality-severities'):
        assert f'id="{control}"' in page, control
    script = read('profile.js')
    for route in ("'/profile'", "'/jobs/'", '/quality', '/personal', "'/session'", '/sample', '/count'):
        assert route in script, route
    assert 'structure only, no data to profile' in script.lower()
    # "~" for an estimate, ">" for a table that has at least that many rows
    assert "kind === 'estimate' ? '~' : kind === 'more_than' ? '>' : ''" in read('app.js')
    assert 'rowsMark(t.row_count_kind)' in script
    # a table whose last profile timed out or failed says so
    assert "'timed out'" in script and "'failed'" in script and 'status_message' in script


def test_one_job_watcher_serves_every_kind_of_job():
    script = read('profile.js')
    assert "job.kind === 'relations'" in script and "runHooks('job', job, true)" in script
    for box in ('job-box', 'rel-job-box', 'scripts-job-box'):
        assert f"'{box}'" in script, box
    for button in ('profile-all', 'rel-check-all', 'rel-check-selected', 'rel-suggest', 'scripts-scan'):   # one job per workspace
        assert f"'{button}'" in script.split('const JOB_BUTTONS')[1].split(';')[0], button
    assert 'hooks.job.push(relationsJob)' in read('analysis.js')


def test_the_relations_tab_has_its_controls():
    page = read('index.html')
    for control in ('rel-check-all', 'rel-check-selected', 'rel-suggest', 'rel-job-progress', 'rel-job-cancel',
                    'rel-search', 'rel-kind', 'rel-status', 'relations-body'):
        assert f'id="{control}"' in page, control
    script = read('analysis.js')
    for route in ("'/relations/check'", "'/relations/suggestions'"):
        assert route in script, route
    for action in ("'accept'", "'reject'", "'reset'"):
        assert action in script, action
    # coverage, orphans, cardinality, evidence and the sampled flag; sortable and filterable
    for word in ('Coverage', 'Orphans', 'Empty keys', 'Cardinality', 'Measured on', 'parent_unique_how', 'cardinality_note',
                 'orphan_examples', 'tag sampled', 'sortable', 'aria-sort', 'With orphan rows', 'Measured on a sample'):
        assert word in script, word
    assert 'needs_password' in script                                                   # asks before it starts


def test_a_sample_is_shown_with_the_whole_text_of_how_it_was_taken():
    """An Oracle block sample carries its warning in that text ("block sample: a few stretches of the
    table, may not represent all of it"): the Profile tab and a relationship's evidence show all of it."""
    assert '— ${d.sample_method}. ' in read('profile.js')
    # a relationship's evidence: the sentences come from the server (relations.limits) with the method's whole text in
    # them, and the page prints them as they are; a count made by the database names the sample the cardinality rests on
    script = read('analysis.js')
    assert "for (const note of v.notes) fact('Not exact'" in script and '(${c.child_sample_method})' in script
    from app.analysis.relations import limits
    text = 'SAMPLE BLOCK (5%): block sample: a few stretches of the table, may not represent all of it'
    assert f'({text})' in limits({'sampled': True, 'sample_method': text, 'rows': 50})[1][0]


def test_a_count_that_is_a_lower_bound_says_so_in_the_list_row():
    """The dry run's problem 2: "40" orphans in the list, and only the evidence - after a click - said that 5,000 of
    37,026 keys had been looked up. The row itself carries the server's texts now."""
    script = read('analysis.js')
    row = script.split('function relationRows')[1].split('\nfunction ')[0]
    assert 'const v = c ? c.shown : null;' in row and 'text: v.orphans' in row and "class: 'rel-limit'" in row
    assert 'text: v.tag' in row and 'v.empty_keys' in row and 'coverageMeter(c.coverage' in row and 'v.coverage)' in row
    assert 'pct(' not in row                                            # no rounding of its own
    assert "['inexact', 'Counts not exact (at least)']" in script and 'c.shown.exact' in script
    assert '.rel-limit' in read('styles.css')
    # accepting a link starts its check: the page follows that job
    accept = script.split('async function decideLink')[1].split('\n}\n')[0]
    assert 'if (done.job)' in accept and 'watchJob(done.job)' in accept and 'It is being checked now.' in accept


def test_the_sql_tab_has_its_controls():
    page = read('index.html')
    for control in ('sql-source', 'sql-editor', 'sql-run', 'sql-csv', 'sql-banner', 'sql-mask', 'sql-history',
                    'sql-history-clear', 'sql-result'):
        assert f'id="{control}"' in page, control
    script = read('analysis.js')
    for route in ("wsPath('/sql')", '/sql/results/', "'/sql/history'"):
        assert route in script, route
    assert 'more rows exist' in script and 'r.max_rows' in script                       # the cap is said, not hidden
    assert 'Blocked by the read-only guard' in script and 'nothing was sent' in script
    # an Oracle source whose list of what can be called was not read: said in the tab, and Run is off
    assert 'sqlBlocked()' in script and 'No query can run on this source yet' in script and 'cannot run queries yet' in script
    assert "$('#sql-run').disabled = !able || sqlTab.running || !!sqlBlocked();" in script
    assert "r.masked ? 'masked' : 'personal'" in script                                 # personal columns are marked
    assert 'info.history_size' in script and 'Results are never saved' in script
    assert 'title: maskedColumnsTitle(r.columns)' in script and '`${c.name}: ${c.reason}`' not in script


def test_the_tooltip_of_the_masked_columns_names_a_column_once():
    """ "first_name: employee.first_name: looks like name" said the name twice. The page's own function is run
    (with Node.js, when this PC has it) on the reasons the SQL tab gives."""
    function = re.search(r'\nfunction maskedColumnsTitle\(columns\) \{\n.*?\n\}\n', read('analysis.js'), re.S)
    assert function, 'maskedColumnsTitle is not in analysis.js'
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    columns = [
        {'name': 'first_name', 'personal': True, 'reason': 'employee.first_name: looks like name'},       # as it is: once
        {'name': 'who', 'personal': True, 'reason': 'employee.first_name: looks like name'},              # renamed: both
        {'name': 'name', 'personal': True, 'reason': 'customer.first_name: looks like name'},             # "name" is not "first_name"
        {'name': 'FIRST_NAME', 'personal': True, 'reason': 'X_STAFF.FIRST_NAME: looks like name'},        # Oracle
        {'name': 'full_name', 'personal': True, 'reason': 'telecom.customers.full_name: looks like name'},
        {'name': 'name', 'personal': True, 'reason': 'store.name: marked personal by you'},
        {'name': 'ref', 'personal': True, 'reason': 'customers.ref: its values look like national ID / Iqama'},
        {'name': 'AVG(salary)', 'personal': True, 'reason': 'Built from employee.salary (looks like salary / pay)'},
        {'name': '', 'personal': True, 'reason': 'Built from x_staff.salary (looks like salary / pay)'},  # SQL Server: no name
        {'name': 'email', 'personal': True, 'reason': 'Looks like e-mail'},                               # judged by its name alone
        {'name': 'city', 'personal': False, 'reason': None},
        {'name': 'kept', 'personal': True, 'reason': 'Built from a personal column'},
    ]
    script = function.group(0) + 'console.log(maskedColumnsTitle(JSON.parse(process.argv[1])));'
    shown = subprocess.run([node, '-e', script, json.dumps(columns)], capture_output=True, text=True, timeout=60, check=True).stdout
    assert shown.rstrip('\n').split('\n') == [
        'employee.first_name: looks like name',
        'who: employee.first_name: looks like name',
        'name: customer.first_name: looks like name',
        'X_STAFF.FIRST_NAME: looks like name',
        'telecom.customers.full_name: looks like name',
        'store.name: marked personal by you',
        'customers.ref: its values look like national ID / Iqama',
        'AVG(salary): Built from employee.salary (looks like salary / pay)',
        'Built from x_staff.salary (looks like salary / pay)',
        'email: Looks like e-mail',
        'kept: Built from a personal column',
    ]


def test_the_report_tab_has_its_controls():
    page = read('index.html')
    for control in ('report-scope-all', 'report-scope-view', 'report-html', 'report-xlsx', 'report-ready'):
        assert f'id="{control}"' in page, control
    assert 'A report is always masked.' in page
    script = read('analysis.js')
    assert "wsPath('/report?' + q)" in script and 'viewQuery(q)' in script              # the graph's own filters
    body = script.split('async function downloadReport')[1].split('\n}\n')[0]
    assert 'personal' not in body.replace('Personal values are masked in it', '')       # nothing about the switch is sent
    # it is a browser download: the page fetches the file and hands it over
    assert 'URL.createObjectURL' in script and "download: name" in script


def test_accepted_links_are_dotted_in_the_graph():
    assert """edge[kind = "values"]', style: { 'line-style': 'dotted'""" in read('app.js')
    assert "k.id === 'values' ? 'dotted'" in read('app.js')
    assert '<i class="line dotted"></i>accepted from values' in read('index.html')
    assert '.line.dotted' in read('styles.css') and '.tag.values' in read('styles.css')


def at_rules(css):
    """The stylesheet in pieces, in file order: ('', what is outside every @ rule), then (rule, its text) each."""
    out, top, at = [], [], 0
    for m in re.finditer(r'@(?:container|media)[^{]*\{', css):
        if m.start() < at:
            continue
        top.append(css[at:m.start()])
        depth, end = 1, m.end()
        while depth:
            depth += {'{': 1, '}': -1}.get(css[end], 0)
            end += 1
        out.append((' '.join(m.group(0)[:-1].split()), css[m.end():end - 1]))
        at = end
    return [('', ''.join(top + [css[at:]]))] + out


def px(text, selector, prop='width'):
    m = re.search(r'(?<![\w.-])' + re.escape(selector) + r'\s*\{[^}]*?(?<![\w-])' + prop + r':\s*(\d+)px', text)
    return int(m.group(1)) if m else None


# A list of the centre panel is a table with fixed column widths whose first column takes what is left.
# (table, its fixed columns, how often each occurs)
LISTS = (
    ('.cols-grid', {'w-type': 1, 'w-null': 1, 'w-num': 1, 'w-val': 2, 'w-personal': 1}),
    ('.rel-grid', {'w-check': 1, 'w-kind': 1, 'w-cov': 1, 'w-n': 1, 'w-nulls': 1, 'w-card': 1, 'w-rows': 1}),
    ('.overview-grid', {'w-check': 1, 'w-rows': 1, 'w-ncols': 1, 'w-when': 1}),
)
NAME_COLUMN_AT_LEAST = 140


def test_a_list_never_squeezes_its_name_column():
    """Found by hand on the Profile tab: in a narrow panel the fixed columns took everything, the name
    column got 0 px and every name was drawn one letter per line. Each list now has a minimum width
    that leaves the name column at least 140 px, at every step of the narrow layout, and below that
    width it scrolls sideways inside its own box."""
    pieces = [(rule, text) for rule, text in at_rules(read('styles.css')) if not rule or rule.startswith('@container body')]
    assert [rule for rule, _ in pieces][:3] == ['', '@container body (max-width: 717px)', '@container body (max-width: 619px)']
    for table, columns in LISTS:
        widths, steps = {}, 0
        for rule, text in pieces:                                   # each step keeps what the one before set
            for name in columns:
                widths[name] = px(text, f'{table} col.{name}') or widths.get(name)
            least = px(text, table, 'min-width')
            if least is None:
                continue
            steps += 1
            fixed = sum(widths[name] * n for name, n in columns.items())
            assert least - fixed >= NAME_COLUMN_AT_LEAST, (table, rule, least, fixed)
            if rule:                                                # the step fits the panel it is made for
                assert least <= int(re.search(r'max-width: (\d+)px', rule).group(1)) + 1 or 'overflow-x: auto' in text, (table, rule)
        assert steps >= 1, table
    top = pieces[0][1]
    assert px(top, '.cols-grid', 'min-width') == 718 and px(top, '.rel-grid', 'min-width') == 718       # one more than the first step
    assert px(top, '.overview-grid.with-sample', 'min-width') - px(top, '.overview-grid', 'min-width') == px(top, '.overview-grid col.w-sample')
    # narrower than the smallest step: the box scrolls sideways, the name column stays in view,
    # and an open row keeps the width of the box
    narrow = dict(pieces)['@container body (max-width: 619px)']
    assert '.table-fit { overflow-x: auto; }' in narrow and '.table-fit .pin { position: sticky; left: 0;' in narrow
    assert re.search(r'\.detail-fit \{[^}]*position: sticky[^}]*100cqw', top)
    assert 'container: body / inline-size' in top and 'container: centre / inline-size' in top
    # every list is in such a box, and its name cells are the pinned ones
    profile, analysis = read('profile.js'), read('analysis.js')
    assert profile.count("class: 'table-fit'") == 2 and analysis.count("class: 'table-fit'") == 2
    assert "class: 'c-strong pin pin-last', title: c.name" in profile                  # the whole name on hover
    assert profile.count("class: 'detail-fit'") == 1 and analysis.count("class: 'detail-fit'") == 1


def test_names_wrap_after_an_underscore_and_never_inside_a_word():
    css = at_rules(read('styles.css'))[0][1]
    for selector in ('.data-table .c-strong', '.cols-table td.c-name', '.rel-side', '.col-name .n'):
        rule = re.search(r'(?m)^' + re.escape(selector) + r' \{([^}]*)\}', css).group(1)
        assert 'text-overflow: ellipsis' in rule and 'overflow: hidden' in rule, selector
        assert 'anywhere' not in rule and 'break-word' not in rule and 'break-all' not in rule, selector
    assert "h('span', { class: 'n' }, breakable(c.name))" in read('profile.js')
    # the tab bar wraps to a second row instead of hiding a tab; a tab's own text never wraps
    assert re.search(r'(?m)^\.tabs \{[^}]*flex-wrap: wrap', css) and re.search(r'(?m)^\.tab \{[^}]*white-space: nowrap', css)
    # tool bars: a group wraps inside itself instead of running under the next panel
    assert re.search(r'(?m)^\.toolbar \.group \{[^}]*flex-wrap: wrap[^}]*max-width: 100%', css)


# ------------------------------------------------------------------ the theme

def variables(css):
    """The two sets of colour variables of styles.css and what is left of the file without them."""
    light = re.search(r'(?m)^:root \{(.*?)^\}\n', css, re.S)
    dark = re.search(r'(?m)^:root\[data-theme="dark"\] \{(.*?)^\}\n', css, re.S)
    rest = css.replace(light.group(0), '').replace(dark.group(0), '')
    colour = r'--([\w-]+):\s*(#[0-9a-fA-F]{6}|rgba\([^)]*\)|transparent)\s*;'
    return dict(re.findall(colour, light.group(1))), dict(re.findall(colour, dark.group(1))), rest, light.group(1), dark.group(1)


def test_the_theme_is_set_before_the_page_is_drawn():
    page = read('index.html')
    head = page.split('</head>')[0]
    # the first thing the page loads, ahead of the style sheet: a file from /static, not an inline
    # script (the "everything comes from /static" rule), and neither deferred nor async
    assert re.search(r'<(?:script|link)\b[^>]*>', head).group(0) == '<script src="/static/theme.js">'
    assert head.index('/static/theme.js') < head.index('/static/styles.css')
    assert not re.search(r'<script(?![^>]*\ssrc=)', page)
    script = read('theme.js')
    assert "const CHOICES = ['system', 'light', 'dark'];" in script
    assert "return CHOICES.includes(value) ? value : 'system';" in script                # System is the default
    assert "root.setAttribute('data-theme', resolve(choice));" in script                 # what styles.css reads
    # remembered in the browser; a browser that blocks storage still gets a theme
    assert script.count('localStorage.') == 2
    for use in re.finditer(r'localStorage\.', script):
        assert re.search(r'try \{\s*(?:const value = )?$', script[:use.start()]), script[use.start() - 60:use.start() + 40]
    assert script.count('} catch (e) {') == 2
    # System follows the operating system while the page is open; a change in another tab is followed too
    assert "window.matchMedia('(prefers-color-scheme: dark)')" in script and "dark.addEventListener('change', follow)" in script
    assert "window.addEventListener('storage'" in script
    assert not re.search(r'innerHTML|document\.write|\beval\(|fetch\(|XMLHttpRequest|import\(|https?:', script)


def test_the_header_has_a_theme_switch_with_three_choices(client):
    page = read('index.html')
    header = page.split('<header class="topbar">')[1].split('</header>')[0]
    switch = header.split('id="theme-switch"')[1].split('</div>')[0]
    assert re.findall(r'<input type="radio" name="theme" value="(\w+)"( checked)?>', switch) == [
        ('system', ' checked'), ('light', ''), ('dark', '')]
    app = read('app.js')
    assert 'theme.set(input.value)' in app and 'theme.onChange(applyTheme);' in app
    assert client.get('/static/theme.js').status_code == 200


def test_every_colour_is_a_variable_with_a_light_and_a_dark_value():
    light, dark, rest, light_text, dark_text = variables(read('styles.css'))
    assert len(light) >= 80 and set(dark) == set(light)                                 # no colour is left light in the dark theme
    changed = [name for name in light if light[name] != dark[name]]
    assert len(changed) >= len(light) - 8, sorted(set(light) - set(changed))            # a few are the same on purpose (white on a toast ...)
    assert 'color-scheme: light;' in light_text and 'color-scheme: dark;' in dark_text  # form fields, scroll bars, native menus
    # nothing outside the two sets names a colour ...
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(', rest)
    assert not re.search(r':\s*(?:white|black|red|green|blue|gray|grey|silver|navy)\b', rest)
    # ... and every variable that is used exists
    names = set(re.findall(r'--([\w-]+):', light_text))
    assert set(re.findall(r'var\(--([\w-]+)\)', rest + light_text)) <= names
    # the scripts have no colour of their own either, also not in a style they set
    for name in SCRIPTS + ('theme.js',):
        script = read(name)
        assert not re.search(r'#[0-9a-fA-F]{6}(?![\w-])|#[0-9a-fA-F]{3}(?![\w-])|\brgba?\(|\bhsla?\(', script), name
        assert not re.search(r"\.style\.(?:color|background|border)|style: `[^`]*(?:color|background)", script), name
    # the graph and the charts are drawn on a canvas: they read the same variables
    app, profile = read('app.js'), read('profile.js')
    graph = app.split('function graphStyle()')[1].split('\n}\n')[0]
    asked = set(re.findall(r"\bc\('([\w-]+)'\)", graph)) | set(re.findall(r"themeColor\('([\w-]+)'\)", app + profile))
    assert len(asked) >= 20 and asked <= names, asked - names
    assert {f'schema-{n}' for n in range(1, 7)} | {f'schema-{n}-line' for n in range(1, 7)} <= names
    assert 'themeColor(`schema-${n}`), themeColor(`schema-${n}-line`)' in app


def test_the_graph_and_the_charts_change_theme_without_a_new_layout():
    app, profile = read('app.js'), read('profile.js')
    change = app.split('function applyTheme()')[1].split('\n}\n')[0]
    assert 'cy.style(graphStyle());' in change and 'n.data({ bg, bd });' in change and "runHooks('theme');" in change
    for word in ('layout', 'fit', 'zoom', 'pan(', 'buildGraph', 'destroy', 'position', 'reload'):
        assert word not in change.split('*/')[-1].lower(), word                         # colours only
    paint = profile.split('function paintChart()')[1].split('\n}\n')[0]
    for part in ("themeColor('bar')", "themeColor('bar-hover')", "themeColor('line-strong')", "themeColor('chart-grid')",
                 "themeColor('muted')", "themeColor('tooltip')", "themeColor('on-tooltip')", "chart.update('none');"):
        assert part in paint, part
    assert 'new Chart' not in paint and 'hooks.theme.push(paintChart);' in profile


def test_contrast_in_both_themes():
    """4.5:1 for text and 3:1 for lines and icons, for every pair of colours that is used together.
    The pairs and the numbers: python -m scripts.contrast"""
    from scripts import contrast

    rows = contrast.check()
    assert len(rows) >= 90
    low = [(r['drawn'], r['on'], round(r['light'], 2), round(r['dark'], 2)) for r in rows if not r['ok']]
    assert low == []
    text = [r for r in rows if r['least'] == contrast.TEXT]
    lines = [r for r in rows if r['least'] == contrast.LINE]
    assert len(text) >= 50 and all(min(r['light'], r['dark']) >= 4.5 for r in text)
    assert len(lines) >= 30 and all(min(r['light'], r['dark']) >= 3 for r in lines)
    # the check knows a colour that is too weak when it sees one
    assert contrast.ratio('#ffffff', '#000000') == 21 and round(contrast.ratio('#777777', '#ffffff'), 2) == 4.48
    light, dark, _, _, _ = variables(read('styles.css'))
    used = {name for r in rows for name in (r['drawn'], r['on'])}
    solid = {name for name, value in light.items() if value.startswith('#')}
    assert solid - used == set(), sorted(solid - used)                                  # no colour escapes the check


def test_values_from_data_never_become_html():
    # Table names, column names, data values and query results come from databases and files: the
    # page only ever puts them in as text.
    for name in SCRIPTS:
        script = read(name)
        assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function', script), name
    assert '<script>' not in read('index.html').split('<body>')[1]                      # no inline script: the CSP forbids it


LINES_SCRIPT = r"""(function () {             // (a function of its own: node -e runs a script in the global scope)
const fs = require('fs'), path = require('path');
const root = process.argv[1];
const cytoscape = require(path.join(root, 'static/vendor/cytoscape.min.js'));
cytoscape.use(require(path.join(root, 'static/vendor/cytoscape-dagre.min.js')));
const app = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const take = (pattern) => { const m = app.match(pattern); if (!m) throw new Error('not in app.js: ' + pattern); return m[0]; };
globalThis.document = { createElement: () => ({ getContext: () => ({ measureText: (t) => ({ width: 6 * t.length }) }) }) };
globalThis.FONT = 'sans-serif';
(0, eval)([take(/^const LABEL_ENDS = \d+;/m), take(/^const labelRooms = new Map\(\);/m),
           take(/^function labelRoom\(label\) \{\n[\s\S]*?\n\}\n/m),
           take(/^function layoutOptions\(name, count, direction = 'LR'\) \{\n[\s\S]*?\n\}\n/m),
           take(/^function makeRoomForLabels\(nodes, edges, direction\) \{\n[\s\S]*?\n\}\n/m),
           take(/^const LINE_MARGIN = \d+;/m), take(/^function lineInBox\(a, b, box\) \{\n[\s\S]*?\n\}\n/m),
           take(/^function clearLines\(nodes, edges, direction\) \{\n[\s\S]*?\n\}\n/m),
           'globalThis.page = { layoutOptions, makeRoomForLabels, clearLines };'].join('\n'));
const { layoutOptions, makeRoomForLabels, clearLines } = globalThis.page;

// The check of the hand check: each line's straight path, at 51 points, against every other table's box.
function crossings(cy) {
  const out = [];
  cy.edges().forEach((e) => {
    const s = e.source().position(), t = e.target().position();
    cy.nodes().forEach((n) => {
      if (n.same(e.source()) || n.same(e.target())) return;
      const p = n.position(), w = n.width() / 2, h = n.height() / 2;
      for (let k = 0; k <= 50; k += 1) {
        const x = s.x + (t.x - s.x) * k / 50, y = s.y + (t.y - s.y) * k / 50;
        if (x > p.x - w && x < p.x + w && y > p.y - h && y < p.y + h) { out.push(`${e.source().id()}->${e.target().id()} through ${n.id()}`); return; }
      }
    });
  });
  return out;
}
function overlaps(cy) {
  const boxes = cy.nodes().map((n) => [n.position('x') - n.width() / 2, n.position('y') - 17, n.position('x') + n.width() / 2, n.position('y') + 17]);
  return boxes.some((a, i) => boxes.some((b, j) => j > i && a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]));
}
const out = {};
for (const [name, view] of Object.entries(JSON.parse(process.argv[2]))) {
  for (const direction of ['TB', 'LR']) {
    for (const fixed of [false, true]) {
      const cy = cytoscape({ headless: true, styleEnabled: true,
        style: [{ selector: 'node', style: { shape: 'round-rectangle', width: 'data(w)', height: 34 } }],
        elements: view.tables.map(([id, w]) => ({ group: 'nodes', data: { id, w } }))
          .concat(view.lines.map(([source, target, label], i) => ({ group: 'edges', data: { id: 'r' + i, source, target, label } }))) });
      cy.layout(layoutOptions('dagre', view.tables.length, direction)).run();
      makeRoomForLabels(cy.nodes(), cy.edges(), direction);
      const cleared = fixed ? clearLines(cy.nodes(), cy.edges(), direction) : null;
      out[`${name} ${direction}${fixed ? '' : ' before'}`] = { crossings: crossings(cy), overlap: overlaps(cy), cleared };
    }
  }
}
console.log(JSON.stringify(out));
process.exit(0);                                    // (a headless graph with styles keeps a timer running)
})();"""


def test_no_line_runs_through_another_tables_box():
    """Round 4. In a focus a line was drawn straight through the box of a table that is not one of its ends: the
    account seemed to point at the payment method. After the hierarchy layout clearLines() moves such a box aside in
    its rank. The page's own layoutOptions(), makeRoomForLabels() and clearLines() are run with the bundled
    Cytoscape and dagre (with Node.js, when this PC has it), on small graphs made for the test, both directions."""
    app = read('app.js')
    run = app.split('\nfunction runLayout() {')[1].split('\n}\n')[0]
    assert 'if (labelled) makeRoomForLabels(connected, edges, direction);\n      clearLines(connected, edges, direction);' in run
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the layout is only looked at, not run')
    views = {
        # a parent of two tables, one of which is the other's parent too; a fourth table beside the middle one
        'shortcut': {'tables': [['account', 120], ['invoice', 110], ['payment', 110], ['method', 220]],
                     'lines': [['account', 'invoice', 'ACCOUNT_ID'], ['invoice', 'payment', 'REF_NO → INVOICE_NO'],
                               ['account', 'payment', 'ACCOUNT_ID'], ['method', 'payment', 'METHOD_ID']]},
        # a lookup that skips the ranks of a wide table
        'lookup': {'tables': [['city', 90], ['customer', 130], ['address', 120], ['profile', 260], ['contact', 120],
                              ['segment', 110]],
                   'lines': [['segment', 'customer', 'SEGMENT_ID'], ['customer', 'address', 'CUSTOMER_ID'],
                             ['city', 'address', 'CITY_ID'], ['customer', 'profile', 'CUSTOMER_ID'],
                             ['customer', 'contact', 'CUSTOMER_ID'], ['profile', 'contact', 'PROFILE_ID']]},
        # a chain with lines that skip one and two ranks
        'chain': {'tables': [['a', 100], ['b', 200], ['c', 180], ['d', 100], ['e', 240]],
                  'lines': [['a', 'b', 'ID'], ['b', 'c', 'ID'], ['c', 'd', 'ID'], ['a', 'd', 'A_ID'], ['a', 'c', 'A_ID'],
                            ['e', 'd', 'E_ID'], ['b', 'd', 'B_ID']]},
    }
    done = subprocess.run([node, '-e', LINES_SCRIPT, str(ROOT), json.dumps(views)], capture_output=True, text=True,
                          timeout=120, encoding='utf-8')
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout)
    assert any(out[f'{name} {d} before']['crossings'] for name in views for d in ('TB', 'LR'))     # the fault is there
    for name in views:
        for direction in ('TB', 'LR'):
            after = out[f'{name} {direction}']
            assert after['crossings'] == [] and after['cleared'] is True, (name, direction, after)
            assert not after['overlap'], (name, direction)


def test_the_details_panel_shows_a_unique_column_as_unique():
    """Round 4. The table API says `unique` (a UNIQUE constraint or a unique index), the panel showed only PK and FK.
    keyTags() is run with Node.js: a unique column that is not the primary key says "unique"; a primary key stays PK."""
    function = re.search(r'\nfunction keyTags\(c\) \{\n.*?\n\}\n', read('app.js'), re.S)
    assert function, 'keyTags is not in app.js'
    assert '.cols-table td.c-keys .tag { margin-right: 3px; }' in read('styles.css')   # two tags wrap, never overflow
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    columns = [{'pk': True, 'unique': True}, {'unique': True}, {'unique': True, 'fk': True}, {'fk': True},
               {'pk_inferred': True, 'unique': True}, {}]
    script = ('const h = (tag, a) => a.text;' + function.group(0) +
              'console.log(JSON.stringify(JSON.parse(process.argv[1]).map(keyTags)));')
    out = subprocess.run([node, '-e', script, json.dumps(columns)], capture_output=True, text=True, timeout=60, check=True).stdout
    assert json.loads(out) == [['PK'], ['unique'], ['unique', 'FK'], ['FK'], ['PK?', 'unique'], []]


def test_a_self_references_label_stands_beside_its_loop():
    """Round 4. A table that points at itself: the label was written across its loop. It stands level, to the right of
    the loop: the loop's place from the middle of the box does not change with the box's width (measured in a
    browser), so the label is moved by LOOP_LABEL_RIGHT plus half its own width, and LOOP_LABEL_DOWN."""
    app = read('app.js')
    assert ("{ selector: 'edge:loop', style: { 'text-rotation': 'none', 'text-margin-x': 'data(lx)', "
            "'text-margin-y': LOOP_LABEL_DOWN } }") in app
    assert "'loop-direction': '-30deg', 'loop-sweep': '-70deg', 'control-point-step-size': 46" in app     # the loop measured
    assert 'lx: r.to === r.from ? loopLabel(label) : 0' in app
    take = [re.search(pattern, app, re.S | re.M) for pattern in (
        r'^const LABEL_ENDS = \d+;', r'^const labelRooms = new Map\(\);', r'^function labelRoom\(label\) \{\n.*?\n\}\n',
        r'^const LOOP_LABEL_RIGHT = \d+;', r'^const LOOP_LABEL_DOWN = \d+;', r'^function loopLabel\(label\) \{\n.*?\n\}\n')]
    assert all(take)
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    script = ("globalThis.document = { createElement: () => ({ getContext: () => ({ measureText: (t) => ({ width: 6 * t.length }) }) }) };"
              "const FONT = 'sans-serif';" + '\n'.join(m.group(0) for m in take) +
              "console.log(JSON.stringify([loopLabel('MANAGER_ID → EMP_ID'), loopLabel('ID'), loopLabel(''), LOOP_LABEL_DOWN]));")
    out = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=60, check=True, encoding='utf-8').stdout
    right, short, none, down = json.loads(out)
    # 16 px right of the loop, which reaches 4 px right of the box's middle; the label's anchor is 26 px left of it.
    # The label's middle is half its text (6 px a letter here) and half its background's padding (2 px) further.
    assert right == 46 + 6 * 19 / 2 + 2 and short == 46 + 6 * 2 / 2 + 2 and none == 0
    assert 26 + 4 + 16 == 46 and down == 12            # and level with the loop's middle, 34 px above the box's middle


def test_chart_js_is_the_bundled_copy():
    bundle = (STATIC / 'vendor' / 'chart.umd.min.js').read_text(encoding='utf-8')
    assert bundle.startswith('/*!\n * Chart.js v4.5.1')
    assert 'chart.umd.min.js | chart.js' in (STATIC / 'vendor' / 'VERSIONS.md').read_text(encoding='utf-8')
    assert (STATIC / 'vendor' / 'licenses' / 'chart.js.LICENSE.txt').read_text(encoding='utf-8').startswith('The MIT License')
