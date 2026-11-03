"""Session 3, part D: the report - one self-contained HTML file and an Excel workbook, always masked."""

import html as html_module
import io
import re
import sqlite3
import time
from html.parser import HTMLParser

import pytest
from openpyxl import load_workbook

from app import config
from app.analysis import report as report_module
from app.analysis.report import render_html, render_xlsx
from app.profiling.personal import mask
from conftest import ROOT

RAW = ['Person Al-Report', '0570000', '@mail.example.com', '1100000', 'SA4420000001']      # what must never appear


@pytest.fixture
def ws(client):
    return client.post('/api/workspaces', json={'name': 'Billing & CRM'}).json()['id']


def wait(client, job):
    assert 'id' in job, job
    for _ in range(6000):
        state = client.get(f'/api/jobs/{job["id"]}').json()
        if state['state'] != 'running':
            return state
        time.sleep(0.01)
    raise AssertionError('job did not finish')


@pytest.fixture
def crm(client, ws, tmp_path):
    """Customers with personal columns, orders with orphans, profiled and checked. Returns the workspace id."""
    path = tmp_path / 'crm.db'
    con = sqlite3.connect(path)
    con.executescript('''
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, email TEXT, national_id TEXT,
                                iban TEXT, city TEXT, branch_code TEXT, joined DATE);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL, status TEXT, buyer_phone TEXT,
                             FOREIGN KEY (customer_id) REFERENCES customers (customer_id));
        CREATE TABLE branches (code TEXT, label TEXT);
        CREATE UNIQUE INDEX ux_branches_code ON branches (code);
        CREATE TABLE empty_log (message TEXT);''')
    con.executemany('INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
        (i, f'Person Al-Report{i:03d}', f'05{70000000 + i:08d}', f'user{i:03d}@mail.example.com', f'1{100000000 + i:09d}',
         f'SA44200000012345678912{i:02d}', ['Riyadh', 'Jeddah', 'Abha'][i % 3], f'B{i % 6:02d}', f'2024-{1 + i % 12:02d}-10')
        for i in range(1, 61)])
    con.execute('PRAGMA foreign_keys = OFF')
    con.executemany('INSERT INTO orders VALUES (?, ?, ?, ?, ?)', [
        (i, None if i % 10 == 0 else 1 + i % 70, i * 2.5, 'paid', f'05{70000000 + 1 + i % 60:08d}') for i in range(1, 201)])
    con.executemany('INSERT INTO branches VALUES (?, ?)', [(f'B{i:02d}', f'Branch {i}') for i in range(5)])
    con.commit()
    con.close()
    assert client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)}).status_code == 200
    assert wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())['errors'] == []
    assert wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={'suggest': True}).json())['errors'] == []
    (suggestion,) = client.get(f'/api/workspaces/{ws}/relations/check').json()['suggestions']
    assert suggestion['from_columns'] == ['branch_code']
    accepted = client.post(f'/api/workspaces/{ws}/relations/suggestions', json={'key': suggestion['key'], 'action': 'accept'})
    wait(client, accepted.json()['job'])                    # an accepted link is measured at once
    wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    return ws


def html_report(client, ws, **params):
    response = client.get(f'/api/workspaces/{ws}/report', params={'format': 'html', **params})
    assert response.status_code == 200, response.text
    return response


def xlsx_report(client, ws, **params):
    response = client.get(f'/api/workspaces/{ws}/report', params={'format': 'xlsx', **params})
    assert response.status_code == 200, response.text
    return load_workbook(io.BytesIO(response.content)), response


def cells(workbook):
    return [str(cell) for sheet in workbook.worksheets for row in sheet.iter_rows(values_only=True) for cell in row if cell is not None]


class Outside(HTMLParser):
    """Collects everything in a page that points at something: src, href, and tags that load resources."""

    def __init__(self):
        super().__init__()
        self.links, self.tags, self.ids = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for name, value in attrs:
            if name in ('src', 'href', 'action', 'data', 'poster', 'srcset', 'background'):
                self.links.append((tag, name, value or ''))
            if name == 'id':
                self.ids.append(value)


# ------------------------------------------------------------- the two downloads

def test_the_html_report_is_one_self_contained_file(client, crm):
    response = html_report(client, crm)
    assert response.headers['content-type'] == 'text/html; charset=utf-8'
    assert response.headers['content-disposition'] == 'attachment; filename="Billing_CRM-report.html"'
    page = response.text
    assert page.startswith('<!doctype html><html lang="en">') and page.rstrip().endswith('</html>')
    found = Outside()
    found.feed(page)
    assert not {'script', 'link', 'iframe', 'object', 'embed', 'video', 'audio', 'form', 'base'} & set(found.tags)
    images = [value for tag, name, value in found.links if tag == 'img']
    assert len(images) == 1 and images[0].startswith('data:image/png;base64,iVBOR')       # the diagram, inside the file
    anchors = [value for tag, name, value in found.links if tag == 'a']
    assert anchors and all(value.startswith('#') for value in anchors)                    # only links within the page
    assert {value[1:] for value in anchors} <= set(found.ids)
    assert len(found.links) == len(images) + len(anchors)
    assert not re.search(r'https?://|//[a-z0-9.-]+\.[a-z]{2,}/|url\(|@import|javascript:', page.replace(images[0], ''))
    assert 'onclick' not in page and 'onerror' not in page and 'onload' not in page


def test_the_report_has_every_section(client, crm):
    page = html_report(client, crm).text
    for heading in ('<h2 id="summary">Summary</h2>', '<h2 id="diagram">ER diagram</h2>', '<h2 id="tables">Tables</h2>',
                    '<h2 id="profiles">Column profiles</h2>', '<h2 id="quality">Quality findings</h2>',
                    '<h2 id="relations">Relationship checks</h2>', '<h3>Links accepted from value overlap</h3>',
                    '<h2 id="sampling">Notes on sampling</h2>'):
        assert heading in page, heading
    assert '<h1>Billing &amp; CRM - data report</h1>' in page and 'scope: the whole workspace' in page
    for tile in ('<b>1</b><span>sources</span>', '<b>4</b><span>tables</span>', '<b>17</b><span>columns</span>',
                 '<b>2</b><span>relationships</span>', '<b>4</b><span>tables profiled</span>',
                 '<b>2</b><span>relationships checked</span>'):
        assert tile in page, tile
    assert 'Relationships: 1 declared · 0 from joins · 1 from values · 0 from names.' in page
    assert re.search(r'Profiles read \d{4}-\d{2}-\d{2}\.', page) and 'SQLite · crm.db' in page
    # tables with their row counts, and the column profiles
    assert '>customers</a>' in page and '<td class="num">60</td>' in page and '<td class="num">200</td>' in page
    assert 'Riyadh (20)' in page and '<span class="tag key">PK</span>' in page and '<span class="tag key">FK</span>' in page
    # findings; the relationship with orphans; the accepted link
    assert 'The table has no rows.' in page and 'Empty table' in page and '◆ Medium' in page
    assert 'unique: primary key in the catalog' in page and 'keys without a parent: 62 (2)' in page
    assert 'measured on all 200 rows' in page and '1-N' in page and '<td class="num">90%</td>' in page     # 18 of 180 rows
    assert '<span class="strong">customers</span> <span class="mono">(branch_code)</span>' in page
    assert '5 of 6 distinct values of customers.branch_code exist in branches.code' in page and '83.3%' in page
    assert 'is read whole; a bigger one is profiled on a sample' in page
    assert 'No table of this report was profiled on a sample.' in page and 'Personal values are masked in this report' in page


def test_the_excel_report_has_the_same_sections(client, crm):
    workbook, response = xlsx_report(client, crm)
    assert response.headers['content-type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert response.headers['content-disposition'] == 'attachment; filename="Billing_CRM-report.xlsx"'
    assert response.content[:2] == b'PK'
    assert workbook.sheetnames == ['Summary', 'Tables', 'Columns', 'Quality', 'Relationships', 'Accepted links',
                                   'Sampling notes', 'ER diagram']
    summary = {row[0]: row[1] for row in workbook['Summary'].iter_rows(values_only=True) if row[0]}
    assert summary['Report'] == 'Billing & CRM - data report' and summary['Scope'] == 'the whole workspace'
    assert (summary['Tables'], summary['Columns'], summary['Relationships'], summary['Tables profiled']) == (4, 17, 2, 4)
    assert summary['Personal values'] == 'masked - a report is always masked' and summary['  from values'] == 1
    tables = list(workbook['Tables'].iter_rows(values_only=True))
    assert tables[0][:7] == ('Table', 'Schema', 'Kind', 'From', 'Rows', 'Row count is', 'Columns')
    assert {row[0]: (row[4], row[5], row[6], row[7]) for row in tables[1:]} == {
        'branches': (5, 'counted', 2, 'profiled'), 'customers': (60, 'counted', 9, 'profiled'),
        'empty_log': (0, 'counted', 1, 'profiled'), 'orders': (200, 'counted', 5, 'profiled')}
    columns = {(row[0], row[1]): row for row in workbook['Columns'].iter_rows(min_row=2, values_only=True)}
    assert len(columns) == 17
    city = columns[('customers', 'city')]
    assert (city[4], city[6], city[7], city[8], city[9], city[11], city[12]) == ('no', 60, 0, 0, 3, 'Abha', 'Riyadh')
    assert city[16] == 'Abha (20); Jeddah (20); Riyadh (20)'
    name = columns[('customers', 'full_name')]
    assert (name[4], name[5], name[11]) == ('yes', 'name', mask('Person Al-Report001'))
    assert [row[:4] for row in workbook['Quality'].iter_rows(min_row=2, values_only=True)][0][0] in ('high', 'medium', 'low')
    relationships = {row[0]: row for row in workbook['Relationships'].iter_rows(min_row=2, values_only=True)}
    head = [cell.value for cell in workbook['Relationships'][1]]
    assert head == ['Child table', 'Child columns', 'Parent table', 'Parent columns', 'Kind', 'Confidence %', 'Status', 'Coverage',
                    'Rows with a key', 'Orphan rows', 'Orphan rows are', 'Orphan keys', 'Empty keys', 'Rows with an empty key',
                    'Rows measured', 'Cardinality', 'Proven', 'Parent key', 'Evidence', 'Measured on', 'Not the whole table',
                    'Why the counts are a lower bound', 'Example keys without a parent', 'Checked at']
    orders = dict(zip(head, relationships['orders']))
    # coverage and empty keys as the Relations tab prints them; beside them the whole numbers they come from
    assert [orders[k] for k in head[2:17] if k not in ('Parent columns', 'Confidence %')] == [
        'customers', 'declared', 'checked', '90%', 180, 18, 'exact', 9, '10%', 20, 200, '1-N', 'yes']
    assert orders['Parent key'] == 'unique: primary key in the catalog'
    assert orders['Measured on'] == 'measured on all 200 rows. Counted inside the database over the whole child table (one statement)'
    assert (orders['Not the whole table'], orders['Why the counts are a lower bound']) == (None, None)
    assert orders['Example keys without a parent'].startswith('62 (2); 63 (2)')
    (link,) = workbook['Accepted links'].iter_rows(min_row=2, values_only=True)
    assert link[:7] == ('customers', 'branch_code', 'branches', 'code', '83.3%', 6, 5)
    assert workbook['Sampling notes'].max_row == 7 and len(workbook['ER diagram']._images) == 1
    assert workbook['Tables'].freeze_panes == 'A2' and workbook['Columns'].auto_filter.ref == 'A1:R18'


# ------------------------------------------------------------------- always light

def luminance(rgb):
    r, g, b = (v / 255 for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_exports_and_reports_stay_light_whatever_the_theme(client, crm):
    """The page has a dark theme. What leaves the app is made to print and to pass on, and does not
    follow it: the .drawio and .png diagrams, the HTML report and the Excel report."""
    from PIL import Image

    # 1. the page says nothing about its theme when it asks for one
    scripts = {name: (ROOT / 'static' / name).read_text(encoding='utf-8') for name in ('app.js', 'analysis.js')}
    askers = [scripts['app.js'].split('function viewQuery')[1].split('\n}\n')[0],
              scripts['app.js'].split('function exportUrl')[1].split('\n}\n')[0],
              scripts['analysis.js'].split('async function saveFrom')[1].split('\n}\n')[0],
              scripts['analysis.js'].split('async function downloadReport')[1].split('\n}\n')[0],
              scripts['analysis.js'].split('async function exportSqlCsv')[1].split('\n}\n')[0]]
    assert all(asker.strip() for asker in askers)
    for asker in askers:
        assert 'theme' not in asker.lower() and 'dark' not in asker.lower(), asker
    # ... and the app would not listen: no route reads a theme, no renderer asks the reader's PC for one
    assert 'theme' not in (ROOT / 'app' / 'main.py').read_text(encoding='utf-8').lower()
    for module in ('app/analysis/report.py', 'app/export.py', 'app/er/render_png.py', 'app/er/render_drawio.py'):
        assert 'prefers-color-scheme' not in (ROOT / module).read_text(encoding='utf-8'), module

    # 2. a request that names a dark theme anyway gets the same files
    def get(path, **params):
        plain = client.get(f'/api/workspaces/{crm}/{path}', params=params)
        dark = client.get(f'/api/workspaces/{crm}/{path}', params={**params, 'theme': 'dark'},
                          headers={'Sec-CH-Prefers-Color-Scheme': 'dark'})
        assert plain.status_code == 200 and dark.status_code == 200, (path, params)
        return plain, dark

    stamp = lambda text: re.sub(r'Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}', '', text)
    drawio, drawio_dark = get('export', format='drawio')
    assert drawio.text == drawio_dark.text
    png, png_dark = get('export', format='png')
    picture = Image.open(io.BytesIO(png.content)).convert('RGB')
    assert picture.tobytes() == Image.open(io.BytesIO(png_dark.content)).convert('RGB').tobytes()
    page, page_dark = get('report', format='html')
    assert stamp(page.text) == stamp(page_dark.text)
    book, book_dark = (load_workbook(io.BytesIO(r.content)) for r in get('report', format='xlsx'))
    assert cells(book) == cells(book_dark) or [c for c in cells(book) if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', c)] == [
        c for c in cells(book_dark) if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', c)]

    # 3. and those files are light
    # the .png: a white sheet, dark text and lines on it
    width, height = picture.size
    assert picture.getpixel((2, 2)) == (255, 255, 255) and picture.getpixel((width - 3, height - 3)) == (255, 255, 255)
    small = picture.resize((64, 64)).tobytes()
    shades = [small[i:i + 3] for i in range(0, len(small), 3)]
    assert sum(luminance(p) for p in shades) / len(shades) > 0.8
    # the .drawio: white table bodies, pastel headers, no dark fill
    fills = set(re.findall(r'(?:fillColor|swimlaneFillColor)=(#[0-9A-Fa-f]{6})', drawio.text))
    assert '#FFFFFF' in {f.upper() for f in fills}
    assert fills and all(luminance(tuple(int(f[i:i + 2], 16) for i in (1, 3, 5))) > 0.7 for f in fills), fills
    # the HTML report: declared light-only, white page, and no rule that would follow the reader's theme
    assert 'color-scheme: only light' in page.text and 'prefers-color-scheme' not in page.text
    assert re.search(r'body \{[^}]*background: #fff', page.text)
    assert 'data-theme' not in page.text
    # the Excel report: cells are not filled, but for the blue headings with their white text
    fills = {cell.fill.fgColor.rgb for sheet in book.worksheets for row in sheet.iter_rows() for cell in row
             if cell.fill is not None and cell.fill.fill_type == 'solid'}
    assert fills == {'001D4ED8'}, fills
    body = [cell for sheet in book.worksheets for row in sheet.iter_rows(min_row=2) for cell in row if cell.value is not None]
    assert body and all(cell.fill.fill_type is None for cell in body if cell.fill.fgColor.rgb != '001D4ED8')


# ------------------------------------------------------------------ always masked

def test_the_report_is_masked_even_with_show_personal_values_on(client, app, crm):
    """The brief's test: turn the switch on, and the report is still masked - both formats."""
    masked_page = html_report(client, crm).text
    masked_cells = cells(xlsx_report(client, crm)[0])
    assert client.put('/api/session', json={'show_personal': True}).json() == {'show_personal': True}
    # the switch really is on: the app itself now shows the real values ...
    live = client.get(f'/api/workspaces/{crm}/tables/customers/profile').json()
    assert live['show_personal'] is True and live['columns'][1]['stats']['min'] == 'Person Al-Report001'
    assert live['sample']['rows'][0][1] == 'Person Al-Report001' and live['sample']['masked'] is False
    # ... and the report does not
    page = html_report(client, crm).text
    workbook = xlsx_report(client, crm)[0]
    everything = cells(workbook)
    for raw in RAW:
        assert raw not in page, raw
        assert not any(raw in cell for cell in everything), raw
    assert mask('Person Al-Report001') in page and '05******01' in page and 'u***@m***.com' in page
    assert mask('Person Al-Report001') in everything and '05******01' in everything
    assert 'SA************01' in page and '11******01' in page          # IBAN and national ID: by name
    # it is the same report as with the switch off (but for the time it was made)
    strip = lambda text: re.sub(r'Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}', '', text)
    assert strip(page) == strip(masked_page)
    assert [c for c in everything if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', c)] == [
        c for c in masked_cells if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}', c)]
    assert app.state.profiling.show_personal is True                      # and it did not touch the switch


def test_the_report_never_reads_the_switch_or_the_data():
    source = (ROOT / 'app' / 'analysis' / 'report.py').read_text(encoding='utf-8')
    assert 'show_personal' not in source and '_live' not in source
    for reader in ('sample_rows', 'live_values', 'read_table', 'database_table', 'file_table', '.connect(', 'table_profile(',
                   'DatabaseData', 'FileData', 'ReadOnlyDatabase'):
        assert reader not in source, reader                              # only what is saved: no source is read
    assert 'masked_columns' in source


def test_a_column_that_became_personal_after_it_was_saved_is_masked_in_the_report(client, app, crm):
    """city was saved unmasked. A mark saved straight into the table - as if the rules file had changed -
    leaves the saved statistics as they are; the report masks them itself."""
    assert 'Riyadh (20)' in html_report(client, crm).text
    store = app.state.profiling.store
    with store._db() as con:
        con.execute("INSERT INTO personal_marks VALUES (?, 'CUSTOMERS', 'CITY', 1, '2026-10-12T10:00:00')", (crm,))
    saved = store.columns(crm, 'CUSTOMERS')['CUSTOMERS']['CITY']
    assert saved['masked'] is False and saved['top'][0][0] == 'Abha'       # on disk: still as it was
    page = html_report(client, crm).text
    assert 'Riyadh' not in page and 'Jeddah' not in page and 'R****h (20)' in page
    assert not any('Riyadh' in cell or 'Jeddah' in cell for cell in cells(xlsx_report(client, crm)[0]))


def test_orphan_keys_of_personal_columns_are_masked_in_the_report(client, ws, tmp_path):
    path = tmp_path / 'calls.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE subscribers (msisdn TEXT PRIMARY KEY, plan TEXT);'
                      'CREATE TABLE calls (call_id INTEGER PRIMARY KEY, msisdn TEXT, seconds INTEGER, '
                      'FOREIGN KEY (msisdn) REFERENCES subscribers (msisdn));')
    con.executemany('INSERT INTO subscribers VALUES (?, ?)', [(f'9665{i:08d}', 'basic') for i in range(1, 31)])
    con.execute('PRAGMA foreign_keys = OFF')
    con.executemany('INSERT INTO calls VALUES (?, ?, ?)', [(i, f'9665{1 + i % 40:08d}', i) for i in range(1, 201)])
    con.commit()
    con.close()
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    client.put('/api/session', json={'show_personal': True})
    page = html_report(client, ws).text
    assert 'keys without a parent: 96********' in page and '966500000031' not in page and '966500000040' not in page
    assert not any('9665000000' in cell for cell in cells(xlsx_report(client, ws)[0]))


# -------------------------------------------------------------------------- scope

def test_the_report_can_cover_the_current_view(client, crm):
    whole = html_report(client, crm).text
    assert whole.count('<h3 id="t-') == 4 and 'scope: the whole workspace' in whole
    focused = html_report(client, crm, scope='view', focus='branches', hops=1).text
    assert focused.count('<h3 id="t-') == 2 and 'scope: the current view - branches and what is within 1 hop of it' in focused
    assert '>orders</a>' not in focused and 'empty_log' not in focused and '<b>2</b><span>tables</span>' in focused
    assert '<b>1</b><span>relationships</span>' in focused and 'The table has no rows.' not in focused
    two = html_report(client, crm, scope='view', focus='branches', hops=2).text
    assert two.count('<h3 id="t-') == 3 and '>orders</a>' in two
    declared = html_report(client, crm, scope='view', kinds='declared').text
    assert 'relationships: declared' in declared and '<b>1</b><span>relationships</span>' in declared
    assert '5 of 6 distinct values' in declared                          # the accepted link is still listed; only the view's
    workbook = xlsx_report(client, crm, scope='view', focus='branches', hops=1)[0]
    assert [row[0] for row in workbook['Tables'].iter_rows(min_row=2, values_only=True)] == ['branches', 'customers']
    assert client.get(f'/api/workspaces/{crm}/report?format=pdf').status_code == 400
    assert client.get(f'/api/workspaces/{crm}/report?scope=some').status_code == 400
    assert client.get(f'/api/workspaces/{crm}/report?scope=view&focus=nope').status_code == 404
    assert client.get('/api/workspaces/nope/report').status_code == 404


def test_a_view_too_large_for_one_image_says_so(client, crm, monkeypatch):
    monkeypatch.setattr(report_module, 'IMAGE_MAX_PIXELS', 2000)
    monkeypatch.setattr(report_module, 'IMAGE_MIN_DPI', 99)
    page = html_report(client, crm).text
    assert '<img' not in page and 'is too large for one readable image' in page and 'export the diagram as .drawio' in page
    workbook = xlsx_report(client, crm)[0]
    assert len(workbook['ER diagram']._images) == 0 and 'too large' in workbook['ER diagram']['A1'].value


def test_sampled_tables_are_listed_in_the_notes(client, crm, monkeypatch):
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 50)
    wait(client, client.post(f'/api/workspaces/{crm}/profile', json={'tables': ['orders', 'customers']}).json())
    wait(client, client.post(f'/api/workspaces/{crm}/relations/check', json={}).json())
    page = html_report(client, crm).text
    assert '<span class="tag sample">sample of 50</span>' in page
    # SQLite: counted exactly, and read on a sample spread over the table - so nothing says "how many is not known"
    spread = '25 stretches of 2 rows spread over the table, not random'
    assert f'profiled on 50 rows of 200 - {spread}' in page and f'profiled on 50 rows of 60 - {spread}' in page
    assert 'The table has more than' not in page and 'how many is not known' not in page
    assert 'SQLite: short stretches of rows spread over the whole table' in page          # the rule, in the notes
    # the relationship is counted by the database over the whole table, whatever the profile was read on
    assert 'measured on all 200 rows. Counted inside the database over the whole child table (one statement).' in page
    assert 'measured on a sample' not in page and '≥' not in page.split('<h2 id="relations">')[1].split('<h2 id="sampling">')[0]
    notes = [row for row in xlsx_report(client, crm)[0]['Sampling notes'].iter_rows(min_row=2, values_only=True)]
    assert ('orders', f'profiled on 50 rows of 200 - {spread}') in notes
    assert {row[0]: row[9] for row in xlsx_report(client, crm)[0]['Tables'].iter_rows(min_row=2, values_only=True)}['orders'] == 'yes'


def relation_rows(page):
    """The rows of the report's "Relationship checks" table: {child table: the cells as text}."""
    section = page.split('<h2 id="relations">')[1].split('<h3>Links accepted')[0]
    rows = {}
    for row in re.findall(r'<tr>(.*?)</tr>', section, re.S)[1:]:
        cells_ = [html_module.unescape(re.sub(r'<[^>]+>', ' ', cell)) for cell in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        rows[cells_[0].split()[0]] = [' '.join(cell.split()) for cell in cells_]
    return rows


def test_a_relationship_is_printed_in_the_report_as_in_the_tab(client, crm, monkeypatch):
    """The dry run's problem 3: 40 orphan tickets "measured on all 55,406 rows", and coverage "100%" next to 4 orphan
    rows. The report prints the texts the Relations tab prints (check.shown): the same numbers, the same rounding,
    and "≥" with what was measured when a count is a lower bound - HTML and Excel."""
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', False)         # as for files: the keys are read and looked up
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 150)
    monkeypatch.setattr(config, 'RELATION_MAX_KEYS', 40)
    wait(client, client.post(f'/api/workspaces/{crm}/relations/check', json={}).json())
    tab = {r['from']: r['check'] for r in client.get(f'/api/workspaces/{crm}/relations/check').json()['relationships']}
    orders = tab['orders']
    # the 200 orders were counted when they were profiled: 21 stretches of 7 rows, spread over the table
    assert (orders['sampled'], orders['partial_keys'], orders['exact'], orders['rows']) == (True, True, False, 147)
    assert orders['sample_method'] == '21 stretches of 7 rows spread over the table, not random'
    shown = orders['shown']
    assert shown['orphans'] == f'≥ {orders["orphan_rows"]}' and orders['orphan_rows'] > 0
    assert shown['tag'] == f'40 of {orders["keys_total"]} keys looked up · 147 rows spread over the table' and len(shown['notes']) == 3
    page = html_report(client, crm).text
    rows = relation_rows(page)
    for child, check in tab.items():
        printed = check['shown']
        assert rows[child][2:5] == [printed['coverage'], printed['orphans'], printed['empty_keys']], child
        assert (printed['tag'] in rows[child][0]) and all(note in rows[child][6] for note in printed['notes']), child
    assert rows['orders'][0].endswith(f'not the whole table {shown["tag"]}')
    assert 'measured on a sample of 147 rows. The child\'s keys were read on a sample and looked up in the parent.' in rows['orders'][6]
    assert 'the table has at least that many orphan rows' in rows['orders'][6]
    assert 'such a result is a lower bound: its row says "≥" and what was measured.' in html_module.unescape(page)   # the notes
    sheet = xlsx_report(client, crm)[0]['Relationships']
    head = [cell.value for cell in sheet[1]]
    excel = {row[0]: dict(zip(head, row)) for row in sheet.iter_rows(min_row=2, values_only=True)}
    for child, check in tab.items():
        printed, row = check['shown'], excel[child]
        assert (row['Coverage'], row['Empty keys'], row['Orphan rows']) == (printed['coverage'], printed['empty_keys'], check['orphan_rows'])
        assert row['Orphan rows are'] == ('exact' if printed['exact'] else 'at least this many'), child
        assert (row['Not the whole table'] or '') == printed['tag'] and (row['Why the counts are a lower bound'] or '') == ' '.join(printed['notes'])
    assert excel['orders']['Orphan rows are'] == 'at least this many' and excel['orders']['Rows measured'] == 147


def test_coverage_is_never_100_percent_next_to_orphan_rows(client, ws, tmp_path):
    """2,000 of 2,001 rows have a parent: ">99.9%" in the tab, in the HTML report and in the Excel report."""
    path = tmp_path / 'near.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE parents (parent_id INTEGER PRIMARY KEY);'
                      'CREATE TABLE children (child_id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents (parent_id));')
    con.executemany('INSERT INTO parents VALUES (?)', [(i,) for i in range(1, 51)])
    con.executemany('INSERT INTO children VALUES (?, ?)', [(i, 1 + i % 50) for i in range(1, 2001)] + [(2001, 999)])
    con.commit()
    con.close()
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    wait(client, client.post(f'/api/workspaces/{ws}/relations/check', json={}).json())
    (rel,) = client.get(f'/api/workspaces/{ws}/relations/check').json()['relationships']
    check = rel['check']
    assert (check['orphan_rows'], check['rows'], round(100 * check['coverage'], 1)) == (1, 2001, 100.0)     # what rounding would say
    assert (check['shown']['coverage'], check['shown']['orphans'], check['shown']['empty_keys']) == ('>99.9%', '1', '0%')
    row = relation_rows(html_report(client, ws).text)['children']
    assert row[2:5] == ['>99.9%', '1', '0%'] and '100%' not in ' '.join(row)
    sheet = xlsx_report(client, ws)[0]['Relationships']
    excel = dict(zip([cell.value for cell in sheet[1]], next(sheet.iter_rows(min_row=2, values_only=True))))
    assert (excel['Coverage'], excel['Rows with a key'], excel['Orphan rows'], excel['Orphan rows are']) == ('>99.9%', 2001, 1, 'exact')
    assert 100 not in excel.values() and 100.0 not in excel.values()
    # the page prints what the server says: no rounding of its own for a coverage
    script = (ROOT / 'static' / 'analysis.js').read_text(encoding='utf-8')
    assert 'coverageMeter(c.coverage, v.coverage_title' in script and 'v.coverage)' in script
    assert "share === 1 ? '100%'" not in script


def test_an_accepted_link_is_in_the_report_as_checked(client, crm):
    """It is measured when it is accepted - not "never checked" until the next Check all."""
    client.delete(f'/api/workspaces/{crm}/profile')                     # "Clear saved statistics": the checks go too
    view = client.get(f'/api/workspaces/{crm}/relations/check').json()
    assert view['summary']['checked'] == 0 and [x['status'] for x in view['accepted']] == ['accepted']
    key = view['accepted'][0]['key']
    client.post(f'/api/workspaces/{crm}/relations/suggestions', json={'key': key, 'action': 'reset'})
    accepted = client.post(f'/api/workspaces/{crm}/relations/suggestions', json={'key': key, 'action': 'accept'}).json()
    assert wait(client, accepted['job'])['state'] == 'done'
    rows = relation_rows(html_report(client, crm).text)
    assert rows['customers'][2:5] == ['83.3%', '10', '0%'] and 'never checked' not in ' '.join(rows['customers'])
    assert rows['orders'][2] == 'never checked'                         # only the accepted one was measured


def test_a_block_sample_is_called_one_wherever_the_report_shows_it(client, crm, monkeypatch):
    """Oracle above the size limit: SAMPLE BLOCK. The report says what that is - for the table's profile
    and for a relationship check that rests on such a sample."""
    from app.profiling import queries
    label = 'block sample: a few stretches of the table, may not represent all of it'
    assert queries.BLOCK_SAMPLE == label
    monkeypatch.setattr(config, 'SAMPLE_TARGET_ROWS', 50)
    # the test database is SQLite: whatever sample it takes is given Oracle's label here
    monkeypatch.setattr(queries, 'sample_text', lambda dialect, percent=None, limit=None, block=False: f'SAMPLE BLOCK (5%): {label}')
    monkeypatch.setattr(queries, 'spread_text', lambda stretches, rows: f'SAMPLE BLOCK (5%): {label}')
    wait(client, client.post(f'/api/workspaces/{crm}/profile', json={'tables': ['orders']}).json())
    wait(client, client.post(f'/api/workspaces/{crm}/relations/check', json={}).json())
    page = html_report(client, crm).text
    assert f'An Oracle table larger than 1.0 GB is sampled by whole blocks (SAMPLE BLOCK). That is a {label}' in page
    assert f'profiled on 50 rows of 200 - SAMPLE BLOCK (5%): {label}' in page
    assert f'sampled: 50 rows, SAMPLE BLOCK (5%): {label}' in page                       # at the table's own profile
    # the database counted the relationship over the whole table; how often a key repeats was read from that sample
    assert f'how often a key repeats in the child was judged on a sample - SAMPLE BLOCK (5%): {label}' in page
    notes = [row for row in xlsx_report(client, crm)[0]['Sampling notes'].iter_rows(min_row=2, values_only=True)]
    assert ('orders', f'profiled on 50 rows of 200 - SAMPLE BLOCK (5%): {label}') in notes
    assert any(table == 'orders' and 'the check of ' in note and note.endswith(label) for table, note in notes)
    # the Profile tab and a relationship's evidence show the method's whole text as well
    profile = client.get(f'/api/workspaces/{crm}/tables/orders/profile?sample=false').json()
    assert profile['sample_method'].endswith(label)
    checked = [r['check'] for r in client.get(f'/api/workspaces/{crm}/relations/check').json()['relationships']
               if r['check'] and r['check'].get('cardinality_on_sample')]
    assert checked and all(c['child_sample_method'].endswith(label) and not c['sampled'] for c in checked)
    # ... and a check that was itself measured on that sample (the keys looked up) says it as before
    monkeypatch.setattr(config, 'RELATION_WHOLE_TABLE', False)
    wait(client, client.post(f'/api/workspaces/{crm}/relations/check', json={}).json())
    page = html_report(client, crm).text
    assert f'was measured on 50 rows - SAMPLE BLOCK (5%): {label}' in page
    checked = [r['check'] for r in client.get(f'/api/workspaces/{crm}/relations/check').json()['relationships']
               if r['check'] and r['check'].get('sampled')]
    assert checked and all(c['sample_method'].endswith(label) for c in checked)


# ------------------------------------------------------------------- safe output

def test_nothing_is_written_anywhere(client, crm, tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    before = sorted(p.name for p in ROOT.iterdir())
    html_report(client, crm)
    xlsx_report(client, crm)
    assert list(work.iterdir()) == [] and sorted(p.name for p in (tmp_path / 'data').iterdir()) == ['explorer.db']
    assert sorted(p.name for p in ROOT.iterdir()) == before              # nothing new in the project folder


def test_names_and_values_are_escaped_in_html_and_never_become_formulas(client, ws, tmp_path):
    path = tmp_path / 'odd.db'
    con = sqlite3.connect(path)
    con.executescript('CREATE TABLE "<script>alert(1)</script>" ("<b>bold</b>" TEXT, formula TEXT, kind TEXT);')
    con.executemany('INSERT INTO "<script>alert(1)</script>" VALUES (?, ?, ?)',
                    [('<img src=x onerror=alert(1)>', '=HYPERLINK("http://evil.example","x")', '+cmd') for _ in range(5)])
    con.commit()
    con.close()
    client.post(f'/api/workspaces/{ws}/sources/db', json={'engine': 'sqlite', 'database': str(path)})
    wait(client, client.post(f'/api/workspaces/{ws}/profile', json={'tables': 'all'}).json())
    page = html_report(client, ws).text
    assert '<script>' not in page and '<img src=x' not in page and '<b>bold</b>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page and '&lt;img src=x onerror=alert(1)&gt;' in page
    found = Outside()
    found.feed(page)
    assert 'script' not in found.tags and [value for tag, _, value in found.links if tag == 'img'][0].startswith('data:image/png')
    workbook = xlsx_report(client, ws)[0]
    hit = [cell for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row
           if isinstance(cell.value, str) and cell.value[:1] in '=+']
    assert hit and all(cell.data_type == 's' for cell in hit)            # text, not a formula
    assert any(cell.value.startswith('=HYPERLINK') for cell in hit)


def test_an_empty_workspace_still_gives_a_report(client, ws):
    page = html_report(client, ws).text
    assert '<b>0</b><span>tables</span>' in page and 'There are no tables in this view.' in page and '<img' not in page
    workbook = xlsx_report(client, ws)[0]
    assert workbook['Tables'].max_row == 1 and workbook['ER diagram']['A1'].value == 'There are no tables in this view.'


def test_the_renderers_take_plain_data(client, app, crm):
    data = app.state.report.collect(crm)
    assert set(data) >= {'title', 'scope', 'sources', 'counts', 'image', 'tables', 'profiles', 'findings', 'checks',
                         'accepted', 'sampling', 'notes'}
    assert render_html(data).count('<h2 ') == 7 and render_xlsx(data)[:2] == b'PK'
