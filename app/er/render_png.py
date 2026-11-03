"""PNG renderer (matplotlib).

Copied from tools/sql_to_erd.py. App changes: the layout call passes this
renderer's row sizes, `dash_inferred=True` (used by the web app) draws every
relationship that is not a declared key as a dashed line, and `max_pixels` /
`max_side` lower the resolution of very large diagrams instead of running out of
memory or past matplotlib's limit of 65,536 pixels a side. A diagram that would
need less than `min_dpi` to fit raises DiagramTooLarge before anything is drawn.
A relationship the app's model marks one-to-one (Rel.one_to_one) is drawn "1 - 1".
With the default arguments, and a model the parser made, the output is identical
to the script.
"""

import math

from .layout import layout


import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.font_manager import FontProperties   # noqa: E402
from matplotlib.patches import Rectangle             # noqa: E402
from matplotlib.textpath import TextToPath           # noqa: E402

ROW_H, HEAD_H, PAD, MARGIN, TITLE_H = 24, 30, 14, 40, 56
INK, LINE, HEAD_FILL, MUTED = '#1f2937', '#5b6b7f', '#dde7f3', '#6b7280'
PK_COLOR, FK_COLOR = '#b45309', '#2563eb'
PT = 0.72                                       # px -> pt at 100 dpi
_t2p = TextToPath()
DPI = 200                                       # 200 dpi doubles each side of the 100-dpi layout


class DiagramTooLarge(ValueError):
    """The diagram cannot be written as one PNG, even at the lowest readable resolution."""

    def __init__(self, width, height):
        super().__init__(f'diagram of {round(width):,} x {round(height):,} px is too large for a PNG')
        self.width, self.height = width, height


def choose_dpi(width, height, max_pixels=None, max_side=None, min_dpi=40):
    """The resolution for a diagram laid out at `width` x `height` px (at 100 dpi).

    Lowered from 200 so that the image stays within `max_pixels` in total and `max_side`
    on each side. Raises DiagramTooLarge when that needs less than `min_dpi`.
    """
    dpi = DPI
    if max_pixels and width * height * (dpi / 100) ** 2 > max_pixels:
        dpi = min(dpi, int(100 * math.sqrt(max_pixels / (width * height))))
    if max_side and max(width, height) * dpi / 100 > max_side:
        dpi = min(dpi, int(100 * max_side / max(width, height)))
    if dpi < min(min_dpi, DPI):
        raise DiagramTooLarge(width, height)
    return dpi


def measure(s, px, bold=False):
    prop = FontProperties(weight='bold' if bold else 'normal', size=px * PT)
    return _t2p.get_text_width_height_descent(str(s), prop, ismath=False)[0] / PT


def shown_columns(t, linked, all_cols):
    cols = list(t.columns.values())
    if all_cols and t.from_ddl:
        return sorted(cols, key=lambda c: (c.ddl_order is None, c.ddl_order or 0))
    if not all_cols:
        cols = [c for c in cols if c.show_pk or c.show_fk or (t, c.name.upper()) in linked]
    return ([c for c in cols if c.show_pk] + [c for c in cols if c.show_fk and not c.show_pk]
            + [c for c in cols if not c.show_pk and not c.show_fk])


def render(model, out_path, label, all_cols, dash_inferred=False, max_pixels=None, max_side=None, min_dpi=40):
    tables, rels = model.tables, list(model.rels.values())
    linked = {end for r in rels for end in (r.a, r.b)}
    multi = len({t.schema for t in tables if t.schema}) > 1

    info = {}
    for t in tables:
        cols = shown_columns(t, linked, all_cols)
        title = (t.schema + '.' if multi and t.schema else '') + t.name
        tags = [' '.join(k for k, on in (('PK', c.show_pk), ('FK', c.show_fk)) if on) for c in cols]
        tag_w = max((measure(x, 10, True) for x in tags if x), default=0)
        tag_w = tag_w + 8 if tag_w else 0
        name_w = max((measure(c.name, 12) for c in cols), default=0)
        type_w = max((measure(c.type, 11) + 16 for c in cols if c.type), default=0) if all_cols else 0
        w = max(measure(title, 13, True) + 2 * PAD, tag_w + name_w + type_w + 2 * PAD, 120)
        w = math.ceil(w / 10) * 10
        h = HEAD_H + ROW_H * len(cols) + (6 if cols else 0)
        info[t] = (cols, tags, title, tag_w, w, h)

    rowidx = {}
    for r in rels:
        for (ta, ca), (tb, _) in ((r.a, r.b), (r.b, r.a)):
            i = [c.name.upper() for c in info[ta][0]].index(ca)
            rowidx[(ta, tb)] = min(i, rowidx.get((ta, tb), i))
    pos = layout(tables, rels, {t: (v[4], v[5]) for t, v in info.items()}, rowidx, ROW_H, HEAD_H, True)
    P = {t: (x + MARGIN, y + MARGIN + TITLE_H) for t, (x, y) in pos.items()}

    W = max(P[t][0] + info[t][4] for t in tables) + MARGIN + 60
    H = max(P[t][1] + info[t][5] for t in tables) + MARGIN
    W = max(W, measure(f'ER diagram - {label}', 16, True) + 2 * MARGIN, 520)
    dpi = choose_dpi(W, H, max_pixels, max_side, min_dpi)      # too large: stop before drawing anything
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis('off')

    # title + legend
    ax.text(MARGIN, 26, f'ER diagram - {label}', fontsize=16 * PT, weight='bold', color=INK, va='center')
    notes = ['1 - N = one-to-many', 'PK primary key', 'FK foreign key']
    if any(getattr(r, 'one_to_one', False) for r in rels):
        notes.insert(1, '1 - 1 = one-to-one')
    if dash_inferred:
        if any(not r.declared for r in rels):
            notes.append('solid = declared key')
            notes.append('dashed = inferred')
        if any(getattr(r, 'by_values', False) for r in rels):
            notes.append('dotted = accepted from value overlap')
    else:
        if any(r.one is None for r in rels):
            notes.append('dashed = matched by value')
        if any(not r.declared for r in rels):
            notes.append('keys inferred from the joins')
    ax.text(MARGIN, 48, '   ·   '.join(notes), fontsize=11 * PT, color=MUTED, va='center')

    # relationship lines (under the boxes)
    def anchor(t, key):
        x, y = P[t]
        i = [c.name.upper() for c in info[t][0]].index(key)
        return x, y + HEAD_H + 3 + i * ROW_H + ROW_H / 2, x + info[t][4]

    loops = {}
    for r in rels:
        one, many = (r.one, r.b if r.one == r.a else r.a) if r.one else (r.a, r.b)
        lx1, y1, rx1 = anchor(*one)
        lx2, y2, rx2 = anchor(*many)
        if rx1 <= lx2:
            x1, x2, d1, d2 = rx1, lx2, 1, -1
        elif rx2 <= lx1:
            x1, x2, d1, d2 = lx1, rx2, -1, 1
        else:                                     # same column: loop round the right side
            x1, x2, d1, d2 = rx1, rx2, 1, 1
        if d1 == d2:
            k = round(max(x1, x2) / 10)
            loops[k] = loops.get(k, 0) + 1
            ex = max(x1, x2) + 18 + 12 * loops[k]
            xs, ys = [x1, ex, ex, x2], [y1, y1, y2, y2]
        else:
            xs, ys = [x1, x1 + d1 * 22, x2 + d2 * 22, x2], [y1, y1, y2, y2]
        dashed = r.one is None or (dash_inferred and not r.declared)
        dotted = dash_inferred and getattr(r, 'by_values', False)      # app addition: accepted from value overlap
        ax.plot(xs, ys, color=LINE, lw=1.3, ls=(0, (1, 2.5)) if dotted else (0, (4, 3)) if dashed else '-', zorder=1)
        if r.one is not None:
            other = '1' if getattr(r, 'one_to_one', False) else 'N'       # app addition: 1 - 1, as the graph shows it
            for x, y, d, s in ((x1, y1, d1, '1'), (x2, y2, d2, other)):
                ax.text(x + d * 9, y - 3, s, ha='center', va='bottom', fontsize=11 * PT,
                        weight='bold', color=INK, zorder=2)

    # tables
    for t in tables:
        cols, tags, title, tag_w, w, h = info[t]
        x, y = P[t]
        ax.add_patch(Rectangle((x, y), w, h, facecolor='white', edgecolor=LINE, lw=1.2, zorder=3))
        ax.add_patch(Rectangle((x, y), w, HEAD_H, facecolor=HEAD_FILL, edgecolor=LINE, lw=1.2, zorder=4))
        ax.text(x + w / 2, y + HEAD_H / 2, title, ha='center', va='center', fontsize=13 * PT,
                weight='bold', color=INK, zorder=5)
        for i, (c, tag) in enumerate(zip(cols, tags)):
            cy = y + HEAD_H + 3 + i * ROW_H + ROW_H / 2
            if tag:
                ax.text(x + PAD, cy, tag, va='center', fontsize=10 * PT, weight='bold',
                        color=PK_COLOR if c.show_pk else FK_COLOR, zorder=5)
            ax.text(x + PAD + tag_w, cy, c.name, va='center', fontsize=12 * PT, color=INK, zorder=5)
            if all_cols and c.type:
                ax.text(x + w - PAD, cy, c.type, ha='right', va='center', fontsize=11 * PT,
                        color=MUTED, zorder=5)

    fig.savefig(out_path, dpi=dpi, facecolor='white')
    plt.close(fig)
