"""Contrast of the colour pairs the page uses together, in the light and the dark theme.

    python -m scripts.contrast            the table (Markdown), and a non-zero exit when a pair is too low

The colours are read from static/styles.css: the variables of `:root` (light) and of
`:root[data-theme="dark"]`. A pair is (what is drawn, what it is drawn on); the ratio is WCAG 2's.
Text needs 4.5:1. A line or an icon that carries meaning - the edge of a button or field, a focus
ring, a line of the ER graph, a bar, an icon - needs 3:1. Separators and the grid of a chart are
decoration: they are listed with their ratio, and nothing is asked of them.

tests/test_frontend.py runs the same check, so a colour cannot be changed below its minimum unnoticed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / 'static' / 'styles.css'
TEXT, LINE, DECOR = 4.5, 3.0, 0.0
THEMES = ('light', 'dark')

# (drawn, on, minimum, where it is used)
PAIRS = [
    # ---- text on the plain surfaces
    ('ink', 'bg', TEXT, 'the page itself, behind the panels'),
    ('ink', 'surface', TEXT, 'text of the panels, tables, dialogs, fields'),
    ('ink', 'surface-2', TEXT, 'source cards, the empty graph, the disabled SQL editor'),
    ('ink', 'hover', TEXT, 'an icon button under the mouse'),
    ('ink', 'detail', TEXT, 'the facts of an open row'),
    ('ink', 'accent-soft', TEXT, 'a selected table, an open row, a menu item under the mouse'),
    ('ink', 'chip-strong', TEXT, 'the tag "declared"'),
    ('ink', 'mark', TEXT, 'the column name a search found'),
    ('ink-2', 'surface', TEXT, 'tabs, types, messages, the foot of the graph'),
    ('ink-2', 'surface-2', TEXT, 'callouts, headings of the sample and result grids, an unticked chip'),
    ('ink-2', 'chip', TEXT, 'counters, neutral tags, the theme switch, the tabs of the source dialog'),
    ('ink-2', 'chip-strong', TEXT, 'the tag of a source without a kind'),
    ('ink-2', 'accent-soft', TEXT, 'types and counters in a selected or open row'),
    ('ink-2', 'detail', TEXT, 'masked values and evidence in an open row'),
    ('muted', 'surface', TEXT, 'notes, headings, placeholders, "null", row numbers'),
    ('muted', 'surface-2', TEXT, 'the second line of a source, notes on the graph'),
    ('muted', 'chip', TEXT, 'the tags "query" and "ref", "soon" on a tab'),
    ('muted', 'accent-soft', TEXT, 'the second line of a selected table, "not proven" in an open row'),
    ('muted', 'detail', TEXT, 'labels and notes in an open row, the ticks of a chart'),
    ('muted', 'accent-faint', TEXT, 'the second line of a card under the mouse'),
    # ---- the accent
    ('link', 'surface', TEXT, 'links, the active tab, the chosen theme'),
    ('link', 'surface-2', TEXT, 'a link in a callout'),
    ('link', 'detail', TEXT, '"Show as table" in an open row'),
    ('on-accent', 'accent', TEXT, 'a primary button'),
    ('on-accent', 'accent-hover', TEXT, 'a primary button under the mouse'),
    ('accent-ink', 'accent-soft', TEXT, 'a pressed toggle, the tags "SQL" and "DDL"'),
    ('accent-ink', 'info-soft', TEXT, 'the "Sampled" callout and tag'),
    ('accent-ink', 'surface', TEXT, 'the other table of a relationship, the table of a finding'),
    ('accent-ink', 'accent-faint', TEXT, 'the same, under the mouse'),
    ('pk', 'pk-soft', TEXT, 'the tag PK'),
    ('fk', 'fk-soft', TEXT, 'the tag FK'),
    ('pk', 'surface', TEXT, 'the tag "PK?" of an inferred key (no fill)'),
    ('fk', 'surface', TEXT, 'the tag "FK?" of an inferred key (no fill)'),
    # ---- states
    ('ok-ink', 'ok-soft', TEXT, '"Local only", the tags "DB" and "ok", a connection that worked'),
    ('ok-ink', 'surface', TEXT, 'a schema to click after a connection test'),
    ('warn-ink', 'warn-soft', TEXT, 'the "Personal values are visible" banner, "Personal", "masked", warnings'),
    ('warn', 'surface', TEXT, 'orphan counts, "timed out"'),
    ('warn', 'surface-2', TEXT, 'the notes of a source'),
    ('warn', 'accent-soft', TEXT, 'an orphan count in an open row'),
    ('danger-ink', 'danger-soft', TEXT, '"Blocked by the read-only guard", the tags "failed" and "blocked"'),
    ('danger', 'danger-soft', TEXT, 'a Delete or Cancel button under the mouse'),
    ('on-toast', 'toast', TEXT, 'a toast'),
    ('on-toast', 'toast-error', TEXT, 'a toast with an error'),
    ('on-toast', 'toast-ok', TEXT, 'a toast with good news'),
    ('on-tooltip', 'tooltip', TEXT, 'the tooltip of a chart'),
    # ---- the ER graph
    ('graph-node-ink', 'graph-node', TEXT, 'a table in the graph'),
    ('graph-node-ink', 'graph-partial', TEXT, 'a table known only in part'),
    ('graph-node-ink', 'graph-selected', TEXT, 'the selected table'),
    *[('graph-node-ink', f'schema-{n}', TEXT, f'a table of schema colour {n}') for n in range(1, 7)],
    ('graph-edge-ink', 'surface-2', TEXT, 'the label of a relationship'),
    ('graph-hl-ink', 'surface-2', TEXT, 'the label of a highlighted relationship'),
    ('graph-edge', 'surface-2', LINE, 'a declared key (solid) or one from joins (dashed) or values (dotted)'),
    ('graph-edge-light', 'surface-2', LINE, 'a relationship inferred from names (dashed, lighter)'),
    ('graph-hl', 'surface-2', LINE, 'a highlighted relationship'),
    ('graph-node-line', 'surface-2', LINE, "a table's outline"),
    ('graph-neighbor', 'surface-2', LINE, "a neighbour's outline"),
    ('graph-root', 'surface-2', LINE, "the focused table's outline"),
    ('graph-selected-line', 'surface-2', LINE, "the selected table's outline"),
    *[(f'schema-{n}-line', 'surface-2', LINE, f'the outline of schema colour {n}') for n in range(1, 7)],
    ('graph-edge', 'surface', LINE, 'the line styles in the legend and on the kind chips'),
    ('graph-edge-light', 'surface', LINE, 'the lighter line there, the dashed edge of an inferred tag'),
    # ---- lines and icons
    ('line-strong', 'surface', LINE, 'the edge of a button, a field, a chip, a dialog; the scroll bar'),
    ('line-strong', 'surface-2', LINE, 'the edge of an unticked chip; the scroll bar of the graph'),
    ('line-strong', 'detail', LINE, 'the axis of a chart'),
    ('line-hover', 'surface-2', LINE, 'the edge of a button under the mouse'),
    ('focus', 'surface', LINE, 'the focus ring'),
    ('focus', 'surface-2', LINE, 'the focus ring on a card'),
    ('focus', 'accent-soft', LINE, 'the focus ring on a selected row'),
    ('accent', 'surface', LINE, 'the line under the active tab, a tick box, a card under the mouse'),
    ('bar', 'detail', LINE, 'the bars of a histogram and of the top values'),
    ('bar-hover', 'detail', LINE, 'a bar under the mouse'),
    ('bar', 'meter-track', LINE, 'the fill of a "null %" meter on its track'),
    ('warn', 'meter-warn-track', LINE, 'the fill of a meter above 50 %'),
    ('ok', 'ok-soft', LINE, 'the fill of a full coverage meter, the dot of "Local only"'),
    ('muted', 'surface', LINE, 'icon buttons, the arrow of a row, the icon of a low finding'),
    ('warn', 'surface', LINE, 'the icon of a medium finding, "to do" in the report list'),
    ('danger', 'surface', LINE, 'the icon and the edge of a high finding'),
    ('ok', 'surface', LINE, '"done" in the report list'),
    ('link', 'surface', LINE, 'the arrow of the sorted column'),
    # ---- decoration: no minimum
    ('line', 'surface', DECOR, 'separators between panels and sections'),
    ('line-soft', 'surface', DECOR, 'separators between rows'),
    ('chart-grid', 'detail', DECOR, 'the grid of a chart'),
    ('meter-track', 'surface', DECOR, 'the track of a meter'),
    ('accent-line', 'accent-soft', DECOR, 'the edge of a selected row'),
    ('warn-line', 'warn-soft', DECOR, 'the edge of a warning'),
    ('danger-line', 'danger-soft', DECOR, 'the edge of an error'),
    ('ok-line', 'surface', DECOR, 'the edge of a schema button'),
    ('disabled', 'surface', DECOR, 'a tab that is switched off (not a control then)'),
]


def themes(css: str | None = None) -> dict[str, dict[str, str]]:
    """{'light': {name: '#rrggbb'}, 'dark': {...}} - the dark set on top of the light one."""
    css = STYLES.read_text(encoding='utf-8') if css is None else css
    blocks = {'light': re.search(r'(?m)^:root \{(.*?)^\}', css, re.S),
              'dark': re.search(r'(?m)^:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S)}
    out = {}
    for name, block in blocks.items():
        if not block:
            raise ValueError(f'no {name} set of variables in {STYLES.name}')
        out[name] = dict(re.findall(r'--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;', block.group(1)))
    out['dark'] = {**out['light'], **out['dark']}
    return out


def luminance(colour: str) -> float:
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(colour[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check(css: str | None = None) -> list[dict]:
    """One row per pair: its colours and ratio in each theme, and whether it reaches its minimum."""
    sets = themes(css)
    rows = []
    for drawn, on, least, where in PAIRS:
        row = {'drawn': drawn, 'on': on, 'least': least, 'where': where, 'ok': True}
        for theme in THEMES:
            row[theme] = ratio(sets[theme][drawn], sets[theme][on])
            row[theme + '_colours'] = (sets[theme][drawn], sets[theme][on])
            row['ok'] = row['ok'] and row[theme] >= least
        rows.append(row)
    return rows


def main() -> int:
    rows = check()
    kinds = {TEXT: 'text, 4.5', LINE: 'line or icon, 3', DECOR: 'decoration, none'}
    print('| Drawn | On | Needs | Light | Dark | Where |')
    print('| --- | --- | --- | --- | --- | --- |')
    for row in rows:
        mark = '' if row['ok'] else ' **TOO LOW**'
        print(f"| `{row['drawn']}` | `{row['on']}` | {kinds[row['least']]} | {row['light']:.2f} | {row['dark']:.2f} | {row['where']}{mark} |")
    for least, label in kinds.items():
        mine = [r for r in rows if r['least'] == least]
        low = min((min(r['light'], r['dark']), r['drawn'], r['on']) for r in mine)
        print(f'\n{label}: {len(mine)} pairs, lowest {low[0]:.2f} ({low[1]} on {low[2]})', end='')
    bad = [r for r in rows if not r['ok']]
    print(f'\n\n{len(rows)} pairs, {len(bad)} too low')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
