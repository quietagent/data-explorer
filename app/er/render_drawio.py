"""draw.io (.drawio) renderer.

Copied from tools/sql_to_drawio.py. App changes: the layout call passes this
renderer's row sizes, and `dash_inferred=True` (used by the web app) draws every
relationship that is not a declared key as a dashed line, to match the screen.
A relationship the app's model marks one-to-one (Rel.one_to_one) ends in "one" on
both sides instead of the crow's foot. With the default arguments, and a model the
parser made, the output is identical to the script.
"""

import html
import math
import xml.etree.ElementTree as ET

from .layout import layout


ROW_H, HEAD_H = 26, 32
PALETTE = [('#dae8fc', '#6c8ebf'), ('#d5e8d4', '#82b366'), ('#ffe6cc', '#d79b00'),
           ('#e1d5e7', '#9673a6'), ('#fff2cc', '#d6b656'), ('#f8cecc', '#b85450')]
TABLE_STYLE = ('shape=table;startSize={h};container=1;collapsible=1;childLayout=tableLayout;'
               'fixedRows=1;rowLines=0;fontStyle=0;align=center;resizeLast=1;html=1;whiteSpace=wrap;'
               'fontSize=13;fillColor={fill};strokeColor={stroke};swimlaneFillColor=#FFFFFF;')
ROW_STYLE = ('shape=tableRow;horizontal=0;startSize=0;swimlaneHead=0;swimlaneBody=0;fillColor=none;'
             'collapsible=0;dropTarget=0;points=[[0,0.5],[1,0.5]];portConstraint=eastwest;'
             'top=0;left=0;right=0;bottom={bottom};html=1;strokeColor={stroke};')
CELL_STYLE = ('shape=partialRectangle;connectable=0;fillColor=none;top=0;left=0;bottom=0;right=0;'
              'overflow=hidden;whiteSpace=wrap;html=1;fontSize=12;')
EDGE_KNOWN = ('edgeStyle=entityRelationEdgeStyle;html=1;endArrow=ERmany;startArrow=ERmandOne;'
              'endFill=0;startFill=0;endSize=10;startSize=10;')
EDGE_GUESS = 'edgeStyle=entityRelationEdgeStyle;html=1;endArrow=none;startArrow=none;dashed=1;'


def text_width(s, size=12):
    w = 0.0
    for ch in s:
        if ch in 'MW@%':
            w += 11
        elif ch.isupper() or ch.isdigit() or ch == '_':
            w += 8.2
        elif ch in "il.,:;|!'()":
            w += 3.8
        else:
            w += 6.8
    return w * size / 12.0


def ordered_columns(t):
    cols = list(t.columns.values())
    if t.from_ddl:
        return [c for _, c in sorted(enumerate(cols), key=lambda p: (p[1].ddl_order is None,
                                                                     p[1].ddl_order or 0, p[0]))]
    return ([c for c in cols if c.show_pk] + [c for c in cols if c.show_fk and not c.show_pk]
            + [c for c in cols if not c.show_pk and not c.show_fk])


def render(model, source_label, dash_inferred=False):
    tables, rels = model.tables, list(model.rels.values())
    schemas = sorted({t.schema for t in tables if t.schema})
    colors = {s: PALETTE[i % len(PALETTE)] for i, s in enumerate(schemas)} if len(schemas) > 1 else {}
    esc = lambda s: html.escape(str(s), quote=False)

    info = {}
    for t in tables:
        cols = ordered_columns(t)
        key_w = 50 if any(c.show_pk and c.show_fk for c in cols) else 34
        name_w = max([text_width(c.name) for c in cols] + [70]) + 20
        type_w = max(text_width(c.type, 11) + 18 for c in cols if c.type) if any(c.type for c in cols) else 0
        title = (t.schema + '.' if len(schemas) > 1 and t.schema else '') + t.name
        alias_txt = ', '.join(t.aliases[:4]) + (', ...' if len(t.aliases) > 4 else '')
        head_w = text_width(title, 13) * 1.1 + (text_width(f' ({alias_txt})') if alias_txt else 0) + 30
        w = math.ceil(max(key_w + name_w + type_w, head_w, 170) / 10) * 10
        name_w = w - key_w - type_w
        info[t] = (cols, key_w, name_w, type_w, title, alias_txt, w, HEAD_H + ROW_H * len(cols))
    sizes = {t: (v[6], v[7]) for t, v in info.items()}
    rowidx = {}
    for r in rels:
        for (ta, ca), (tb, _) in ((r.a, r.b), (r.b, r.a)):
            i = [c.name.upper() for c in info[ta][0]].index(ca)
            rowidx[(ta, tb)] = min(i, rowidx.get((ta, tb), i))
    pos = layout(tables, rels, sizes, rowidx, ROW_H, HEAD_H, False)

    mxfile = ET.Element('mxfile', host='app.diagrams.net', agent='sql_to_drawio.py')
    diagram = ET.SubElement(mxfile, 'diagram', id='er-diagram', name='ER Diagram')
    gm = ET.SubElement(diagram, 'mxGraphModel', dx='1400', dy='900', grid='1', gridSize='10',
                       guides='1', tooltips='1', connect='1', arrows='1', fold='1', page='0',
                       pageScale='1', pageWidth='1600', pageHeight='1200', math='0', shadow='0')
    root = ET.SubElement(gm, 'root')
    ET.SubElement(root, 'mxCell', id='0')
    ET.SubElement(root, 'mxCell', id='1', parent='0')

    def vertex(cid, value, style, parent, x, y, w, h, alt=False):
        c = ET.SubElement(root, 'mxCell', id=cid, value=value, style=style, vertex='1', parent=parent)
        g = ET.SubElement(c, 'mxGeometry', {'as': 'geometry'})
        for k, v in (('x', x), ('y', y), ('width', w), ('height', h)):
            if v:
                g.set(k, str(int(round(v))))
        if alt:
            ET.SubElement(g, 'mxRectangle', {'width': str(int(w)), 'height': str(int(h)), 'as': 'alternateBounds'})

    # legend
    notes = [f'<b>ER diagram</b> from {esc(source_label)}']
    if len(schemas) == 1:
        notes.append(f'schema {esc(schemas[0])}')
    if any(not t.from_ddl for t in tables):
        notes.append('only the columns the SQL uses are shown')
    if dash_inferred:
        if any(not r.declared for r in rels):
            notes.append('solid line = declared key, dashed line = inferred relationship')
        if any(getattr(r, 'by_values', False) for r in rels):
            notes.append('dotted line = accepted from value overlap')
    else:
        if any(not r.declared for r in rels):
            notes.append('PK/FK and line direction inferred from the joins')
        if any(r.one is None for r in rels):
            notes.append('dashed line = matched by value, no clear key')
    right = max((pos[t][0] + sizes[t][0] for t in tables), default=600)
    vertex('legend', ' &middot; '.join(notes),
           'text;html=1;align=left;verticalAlign=top;whiteSpace=wrap;fontSize=11;fontColor=#666666;',
           '1', 0, 0, max(right, 600), 34)

    top_offset = 70
    row_id = {}
    for ti, t in enumerate(tables):
        cols, key_w, name_w, type_w, title, alias_txt, w, h = info[t]
        fill, stroke = colors.get(t.schema, PALETTE[0])
        tid = f't{ti}'
        head = f'<b>{esc(title)}</b>' + (f' <span style="font-weight:normal">({esc(alias_txt)})</span>'
                                         if alias_txt else '')
        x, y = pos[t]
        vertex(tid, head, TABLE_STYLE.format(h=HEAD_H, fill=fill, stroke=stroke), '1',
               round(x / 10) * 10, round(y / 10) * 10 + top_offset, w, h)
        last_pk = max((i for i, c in enumerate(cols) if c.show_pk), default=-1)
        for ci, c in enumerate(cols):
            rid = f'{tid}r{ci}'
            row_id[(t, c.name.upper())] = rid
            vertex(rid, '', ROW_STYLE.format(bottom=1 if ci == last_pk else 0, stroke=stroke), tid,
                   0, HEAD_H + ci * ROW_H, w, ROW_H)
            key = ' '.join(k for k, on in (('PK', c.show_pk), ('FK', c.show_fk)) if on)
            key_color = '#d79b00' if c.show_pk else '#6c8ebf'
            vertex(rid + 'k', key, CELL_STYLE + f'fontStyle=1;fontSize=11;fontColor={key_color};',
                   rid, 0, 0, key_w, ROW_H, alt=True)
            vertex(rid + 'n', esc(c.name), CELL_STYLE + f'align=left;spacingLeft=6;fontStyle={5 if c.show_pk else 0};',
                   rid, key_w, 0, name_w, ROW_H, alt=True)
            if type_w:
                vertex(rid + 't', esc(c.type or ''), CELL_STYLE + 'align=left;spacingLeft=6;fontSize=11;fontColor=#808080;',
                       rid, key_w + name_w, 0, type_w, ROW_H, alt=True)

    for ei, r in enumerate(rels):
        if r.one is not None:
            many = r.b if r.one == r.a else r.a
            src, tgt, style = row_id[r.one], row_id[many], EDGE_KNOWN
            if getattr(r, 'one_to_one', False):
                style = style.replace('endArrow=ERmany;', 'endArrow=ERone;')      # app addition: 1 - 1, not 1 - N
            if dash_inferred and getattr(r, 'by_values', False):
                style += 'dashed=1;dashPattern=1 3;'        # app addition: dotted = accepted from value overlap
            elif dash_inferred and not r.declared:
                style += 'dashed=1;'
        else:
            src, tgt, style = row_id[r.a], row_id[r.b], EDGE_GUESS
        e = ET.SubElement(root, 'mxCell', id=f'e{ei}', style=style, edge='1', parent='1', source=src, target=tgt)
        ET.SubElement(e, 'mxGeometry', {'relative': '1', 'as': 'geometry'})

    if hasattr(ET, 'indent'):
        ET.indent(mxfile)
    return ET.tostring(mxfile, encoding='unicode')
