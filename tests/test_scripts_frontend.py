"""The Scripts tab's page (static/scripts.js and its view in index.html): what it needs is there.
Like tests/test_frontend.py, these are checks on the files; how the tab behaves was checked in a
browser (HANDOFF.md, the Rimal hand check)."""

import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT

STATIC = ROOT / 'static'


def read(name):
    return (STATIC / name).read_text(encoding='utf-8')


def test_the_tab_is_switched_on_and_has_its_view(client):
    assert client.get('/api/meta').json()['features']['scripts'] is True
    page = read('index.html')
    assert 'id="view-scripts"' in page and "{ id: 'scripts', label: 'Scripts' }" in read('app.js')
    for control in ('scripts-folder', 'scripts-dialect', 'scripts-scan', 'scripts-job-box', 'scripts-job-progress',
                    'scripts-job-text', 'scripts-job-cancel', 'scripts-body'):
        assert f'id="{control}"' in page, control
    assert re.findall(r'<script src="([^"]+)"', page)[-1] == '/static/scripts.js'     # after the helpers it uses
    assert client.get('/static/scripts.js').status_code == 200


def test_it_talks_to_the_scripts_routes():
    script = read('scripts.js')
    for route in ("wsPath('/scripts')", "wsPath('/scripts/scan')", "wsPath('/scripts/impact?name=' + enc(key))",
                  "wsPath('/scripts/report')"):
        assert route in script, route
    assert "api(wsPath('/scripts'), { method: 'DELETE' })" in script                    # Forget scan
    assert 'saveFrom(' in script                                                         # the report is a browser download
    assert "if (err.status === 409 && data.job) { watchJob(data.job); toast(err.message); return; }" in script


def test_the_top_has_scan_cancel_and_after_a_scan_rescan_forget_and_report():
    script = read('scripts.js')
    for text in ("text: 'Rescan'", "text: 'Forget scan'", "text: 'Download report'", 'd.scan.folder', 'when(d.scan.scanned_at)',
                 "'Forget this scan?'"):
        assert text in script, text
    for number in ("'files'", "'statements'", "'unreadable'", "'tables'", "'joins'", "'subquery links'", "'conflicts'",
                   "'repeated groups'", "'joins without a key'"):
        assert f'[{number}, s.' in script, number
    # the dialect: preselected from the workspace's one database source, else the user picks
    assert 'data.default_dialect' in script and "h('option', { value: '', text: 'Dialect…' })" in script


def test_the_sub_views_are_a_segmented_control():
    script = read('scripts.js')
    views = re.findall(r"\{ id: '(\w+)', label: '([^']+)'", script.split('const SCRIPT_VIEWS')[1].split('];')[0])
    assert views == [('joins', 'Joins'), ('conflicts', 'Conflicts'), ('views', 'Suggested views'),
                     ('subqueries', 'Subquery links'), ('nokey', 'Joins without a key'), ('impact', 'Impact'),
                     ('unreadable', 'Unreadable')]
    assert "class: 'seg scr-seg', role: 'tablist'" in script and '.seg {' in read('styles.css')
    assert '.scr-seg { flex-wrap: wrap; }' in read('styles.css')                        # a narrow tab wraps them


def test_the_join_catalog_is_sortable_and_has_a_filter():
    script = read('scripts.js')
    columns = re.findall(r"label: '([^']+)'", script.split('const JOIN_COLUMNS')[1].split('];')[0])
    assert columns == ['Join', 'Types', 'Statements', 'Scripts', 'Declared FK', 'Flags']
    view = script.split('function joinsView')[1].split('\nfunction ')[0]
    assert "'aria-sort'" in view and "class: 'sortable'" in view and "id: 'scr-filter'" in view
    assert "'derived'" in script and "'non-key'" in script and "'conflict'" in script


def test_impact_has_a_heading_per_level_with_its_tables_and_statements():
    view = read('scripts.js').split('function impactView')[1].split('\nfunction ')[0]
    assert "text: `Direct (${plural(r.direct.length, 'use')})`" in view
    assert 'text: `Level ${level.level} (' in view and 'level.tables.map(' in view and 'level.statements.map(' in view
    assert "'Statements that read them:'" in view and 'statementRef(s)' in view        # script and line
    assert "id: 'scr-targets'" in view and 'd.targets.map(' in view                     # picked from the scan


def test_a_table_name_selects_the_table_in_the_app():
    script = read('scripts.js')
    link = script.split('function scriptTable')[1].split('\n}\n')[0]
    assert 'scr.data.in_model[key]' in link and 'selectTable(id' in link and 'state.tables.has(id)' in link
    for view in ('joinCell', 'conflictsView', 'viewsView', 'impactView'):
        assert 'scriptTable(' in script.split(f'function {view}')[1].split('\nfunction ')[0], view


def test_the_job_watcher_knows_the_scan():
    profile = read('profile.js')
    assert "['scripts-job-box', 'scripts-job-progress', 'scripts-job-text', 'scripts-job-cancel']" in profile
    assert "'scripts-scan'" in profile.split('const JOB_BUTTONS')[1].split(';')[0]
    assert "job.kind === 'scripts'" in profile and 'Reading scripts' in profile
    assert "if (job.kind !== 'profile') runHooks('job', job, true);" in profile
    assert "if (job.kind !== 'relations') return;" in read('analysis.js').split('async function relationsJob')[1]
    assert 'hooks.job.push(scriptsJob)' in read('scripts.js')


def test_script_text_is_shown_as_written_and_never_as_html():
    script = read('scripts.js')
    assert "h('pre', { class: 'scr-sql', text: g.sql })" in script and "h('pre', { class: 'scr-sql', text: u.text })" in script
    rule = re.search(r'\.scr-sql \{([^}]*)\}', read('styles.css')).group(1)
    assert 'white-space: pre;' in rule and 'overflow: auto;' in rule
    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function', script)
    assert not re.search(r'#[0-9a-fA-F]{3,8}(?![\w-])|\brgba?\(|\.style\.', script)     # no colour of its own


def test_a_statement_is_named_with_its_script_and_line():
    match = re.search(r'\nconst statementRef = .*?;\n', read('scripts.js'))
    assert match, 'statementRef is not in scripts.js'
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is not installed: the function is only looked at, not run')
    rows = [{'script': 'etl/load.sql', 'number': 2, 'line': 14, 'first_line': 12},
            {'script': 'jobs/run.py', 'number': 1, 'line': None, 'first_line': 7},
            {'script': 'a.sql', 'number': 3}]
    code = match.group(0) + 'for (const r of JSON.parse(process.argv[1])) console.log(statementRef(r));'
    shown = subprocess.run([node, '-e', code, json.dumps(rows)], capture_output=True, text=True, timeout=60, check=True).stdout
    assert shown.splitlines() == ['etl/load.sql #2, line 14', 'jobs/run.py #1, line 7', 'a.sql #3']
