"""Diagram layout shared by both renderers.

Copied from tools/sql_to_erd.py. App changes: the row and header heights are
parameters instead of module globals (each renderer has its own), and
`sort_by_link` selects the row ordering of sql_to_erd.py (True) or
sql_to_drawio.py (False), so each renderer lays out exactly like its script.
"""

from collections import deque
from itertools import product


HGAP, VGAP = 150, 40


def layout(tables, rels, sizes, rowidx, row_h=26, head_h=32, sort_by_link=False):
    order = {t: i for i, t in enumerate(tables)}
    adj = {t: set() for t in tables}
    for r in rels:
        a, b = r.a[0], r.b[0]
        if a is not b:
            adj[a].add(b)
            adj[b].add(a)
    comps, seen = [], set()
    for t in sorted(tables, key=lambda t: (-len(adj[t]), order[t])):
        if t in seen:
            continue
        comp, dq = [], deque([t])
        seen.add(t)
        while dq:
            u = dq.popleft()
            comp.append(u)
            for v in sorted(adj[u], key=order.get):
                if v not in seen:
                    seen.add(v)
                    dq.append(v)
        comps.append(comp)

    pos, y, right = {}, 0.0, 0.0
    for comp in comps:
        if len(comp) == 1:
            continue
        placed = place(arrange(comp, adj, order), adj, sizes, rowidx, row_h, head_h, sort_by_link)
        top = min(p[1] for p in placed.values())
        bottom = max(p[1] + sizes[t][1] for t, p in placed.items())
        for t, (px, py) in placed.items():
            pos[t] = (px, py - top + y)
        right = max(right, max(p[0] + sizes[t][0] for t, p in placed.items()))
        y += bottom - top + 100
    singles = [c[0] for c in comps if len(c) == 1]
    if singles:
        limit, x, row_h = max(right, 1000.0), 0.0, 0.0
        for t in singles:
            w, h = sizes[t]
            if x > 0 and x + w > limit:
                x, y, row_h = 0.0, y + row_h + 60, 0.0
            pos[t] = (x, y)
            x += w + 60
            row_h = max(row_h, h)
    return pos


def arrange(comp, adj, order):
    """Pick a central table and spread its neighbours to the left and right in columns."""
    best = None
    for root in sorted(comp, key=lambda t: (-len(adj[t]), order[t]))[:15]:
        lay, group, dq = {root: 0}, {root: None}, deque([root])
        while dq:
            u = dq.popleft()
            for v in sorted(adj[u], key=order.get):
                if v not in lay:
                    lay[v] = lay[u] + 1
                    group[v] = v if u is root else group[u]
                    dq.append(v)
        kids = sorted((t for t in comp if lay[t] == 1), key=order.get)
        weight = {k: 0 for k in kids}
        for t in comp:
            if group[t] is not None:
                weight[group[t]] += 1
        edges = [(u, v) for u in comp for v in adj[u] if order[u] < order[v]]

        def cost(side):
            cross = sum(1 for u, v in edges if u is not root and v is not root
                        and side[group[u]] != side[group[v]])
            left = sum(weight[k] for k in kids if side[k] < 0)
            rgt = sum(weight[k] for k in kids if side[k] > 0)
            return cross * 10 + abs(rgt - left) + (0.1 if left > rgt else 0)

        if len(kids) <= 10:
            side = min((dict(zip(kids, combo)) for combo in product((1, -1), repeat=len(kids))), key=cost)
        else:
            side, lw, rw = {}, 0, 0
            for k in sorted(kids, key=lambda k: -weight[k]):
                side[k] = 1 if rw <= lw else -1
                rw, lw = (rw + weight[k], lw) if side[k] > 0 else (rw, lw + weight[k])
        col = {t: 0 if t is root else lay[t] * side[group[t]] for t in comp}
        same = sum(1 for u, v in edges if col[u] == col[v])
        span = sum(1 for u, v in edges if abs(col[u] - col[v]) > 1)
        counts = {}
        for t in comp:
            counts[col[t]] = counts.get(col[t], 0) + 1
        score = same * 4 + span * 5 + max(counts.values()) * 2 + len(counts)
        if best is None or score < best[0]:
            best = (score, col)
    cols = {}
    for t in sorted(comp, key=order.get):
        cols.setdefault(best[1][t], []).append(t)
    return cols


def place(cols, adj, sizes, rowidx, row_h=26, head_h=32, sort_by_link=False):
    keys = sorted(cols)
    yc = {}

    def off(a, b):                                # y of a's row that joins b, from a's top
        return head_h + rowidx.get((a, b), 0) * row_h + row_h / 2

    def aligned(t, v):                            # centre of t that lines up the joined rows
        return yc[v] - sizes[v][1] / 2 + off(v, t) - off(t, v) + sizes[t][1] / 2

    for t in cols[0]:
        yc[t] = 0.0
    for c in sorted((c for c in keys if c != 0), key=lambda c: (abs(c), c < 0)):
        inner = set(cols.get(c - 1 if c > 0 else c + 1, []))
        want, link_y = {}, {}
        for t in cols[c]:
            near = [v for v in adj[t] if v in inner] or [v for v in adj[t] if v in yc]
            ys = [aligned(t, v) for v in near] or [0.0]
            want[t] = sum(ys) / len(ys)
            ly = [yc[v] - sizes[v][1] / 2 + off(v, t) for v in near]   # where its lines land
            link_y[t] = sum(ly) / len(ly) if ly else want[t]
        cols[c].sort(key=(lambda t: (link_y[t], want[t])) if sort_by_link else (lambda t: want[t]))
        tops, cur = [], -1e18
        for t in cols[c]:
            top = max(want[t] - sizes[t][1] / 2, cur)
            tops.append(top)
            cur = top + sizes[t][1] + VGAP
        shift = sum(want[t] - (tp + sizes[t][1] / 2) for t, tp in zip(cols[c], tops)) / len(cols[c])
        for t, tp in zip(cols[c], tops):
            yc[t] = tp + shift + sizes[t][1] / 2
    for t in cols[0]:                             # centre the hub on its neighbours
        ys = [aligned(t, v) for v in adj[t] if v in yc]
        if ys:
            yc[t] = sum(ys) / len(ys)
    widths = {c: max(sizes[t][0] for t in cols[c]) for c in keys}
    pos, x = {}, 0.0
    for c in keys:
        for t in cols[c]:
            w, h = sizes[t]
            pos[t] = (x + (widths[c] - w if c < 0 else 0), yc[t] - h / 2)
        x += widths[c] + HGAP
    return pos
