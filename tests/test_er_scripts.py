"""The two CLI scripts in tools/ still run, and app/er behaves exactly like them."""

import importlib.util
import io
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

from app import er
from app.er import render_drawio, render_png
from conftest import ROOT


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_with(module, sql):
    model = module.Model()
    module.Parser(model, module.tokenize(sql)).run()
    module.finalize(model)
    return model


def snapshot(model):
    tables = [(t.name, t.schema, t.from_ddl, t.ddl_pk, tuple(t.aliases),
               [(c.name, c.type, c.pk, c.fk, c.show_pk, c.show_fk, c.ddl_order) for c in t.columns.values()])
              for t in model.tables]
    rels = [((r.a[0].name, r.a[1]), (r.b[0].name, r.b[1]), sorted(r.kinds), r.declared,
             r.one and (r.one[0].name, r.one[1])) for r in model.rels.values()]
    return tables, rels


def png_size(data):
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    return struct.unpack('>II', data[16:24])


@pytest.fixture(scope='module')
def drawio_script():
    return load_script('sql_to_drawio')


@pytest.fixture(scope='module')
def erd_script():
    return load_script('sql_to_erd')


def test_drawio_script_runs_as_cli(demo_sql, tmp_path):
    out = tmp_path / 'model.drawio'
    run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'sql_to_drawio.py'), str(demo_sql), '-o', str(out)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert 'Tables (10):' in run.stdout and 'Relationships (15):' in run.stdout
    root = ET.parse(out).getroot()
    assert root.tag == 'mxfile'
    assert sum(1 for c in root.iter('mxCell') if 'shape=table;' in (c.get('style') or '')) == 10


def test_erd_script_runs_as_cli(demo_sql, tmp_path):
    out = tmp_path / 'model.png'
    run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'sql_to_erd.py'), str(demo_sql), '-o', str(out)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert '10 tables, 15 relationships' in run.stdout
    assert png_size(out.read_bytes())[0] > 500


def test_app_parser_matches_both_scripts(demo_sql, drawio_script, erd_script):
    sql = demo_sql.read_text(encoding='utf-8')
    ours = snapshot(er.parse_sql(sql))
    assert ours == snapshot(parse_with(drawio_script, sql))
    assert ours == snapshot(parse_with(erd_script, sql))


@pytest.mark.parametrize('sql', [
    # joins only: keys and direction are inferred
    'SELECT o.id, c.name FROM orders o JOIN customers c ON c.customer_id = o.customer_id '
    'LEFT JOIN order_lines l ON l.order_id = o.id WHERE o.status IN (SELECT s.code FROM statuses s)',
    # schema-qualified names, ALTER TABLE keys, CTE lineage
    'CREATE TABLE rdb.account (account_id NUMBER(10) PRIMARY KEY, name VARCHAR2(40));'
    'CREATE TABLE rdb.usage_rec (rec_id NUMBER, account_id NUMBER(10), bytes NUMBER);'
    'ALTER TABLE rdb.usage_rec ADD CONSTRAINT fk_u FOREIGN KEY (account_id) REFERENCES rdb.account (account_id);'
    'WITH big AS (SELECT account_id, SUM(bytes) b FROM rdb.usage_rec GROUP BY account_id) '
    'SELECT a.name, big.b FROM account a JOIN big ON big.account_id = a.account_id',
])
def test_app_parser_matches_script_on_other_sql(sql, drawio_script):
    assert snapshot(er.parse_sql(sql)) == snapshot(parse_with(drawio_script, sql))


def test_app_drawio_render_matches_script(demo_sql, drawio_script):
    sql = demo_sql.read_text(encoding='utf-8')
    theirs = drawio_script.render(parse_with(drawio_script, sql), 'telecom.sql')
    ours = render_drawio.render(er.parse_sql(sql), 'telecom.sql')
    assert ours == theirs


def test_app_png_render_matches_script(demo_sql, erd_script, tmp_path):
    sql = demo_sql.read_text(encoding='utf-8')
    theirs = tmp_path / 'script.png'
    erd_script.render(parse_with(erd_script, sql), theirs, 'telecom.sql', False)
    ours = io.BytesIO()
    render_png.render(er.parse_sql(sql), ours, 'telecom.sql', False)
    assert png_size(ours.getvalue()) == png_size(theirs.read_bytes())


def test_png_resolution_is_capped_for_huge_diagrams(demo_sql):
    model = er.parse_sql(demo_sql.read_text(encoding='utf-8'))
    full, capped = io.BytesIO(), io.BytesIO()
    render_png.render(model, full, 'x', False)
    render_png.render(model, capped, 'x', False, max_pixels=500_000)
    w, h = png_size(capped.getvalue())
    assert w * h < png_size(full.getvalue())[0] * png_size(full.getvalue())[1]
    assert w * h <= 600_000


def test_pk_like_singular_forms():
    table = lambda name: er.Table([name])
    assert er.pk_like(table('customers'), 'CUSTOMER_ID')
    assert er.pk_like(table('categories'), 'CATEGORY_ID')
    assert er.pk_like(table('branches'), 'BRANCH_ID')         # "-es" plural: app addition
    assert er.pk_like(table('invoice_lines'), 'LINE_ID')
    assert er.pk_like(table('anything'), 'ID')
    assert not er.pk_like(table('customers'), 'BRANCH_ID')
