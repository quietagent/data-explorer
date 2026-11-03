"""Diagram export: .drawio and .png, drawn by the render code from the ER scripts (app.er)."""

from __future__ import annotations

import io
import re
import threading

from . import config
from .model import to_er

_png_lock = threading.Lock()        # matplotlib's pyplot is not thread-safe


class NothingToExport(Exception):
    pass


class TooBigForPng(Exception):
    """The view is too large to be drawn as one PNG; the API answers 400 with this message."""


def _er_model(model, tables, relationships):
    if not tables:
        raise NothingToExport('There are no tables in this view.')
    em, _ = to_er(model, tables, relationships, flags=True)
    return em


def to_drawio(model, tables, relationships, label):
    """The view as draw.io XML (text)."""
    from .er.render_drawio import render
    return render(_er_model(model, tables, relationships), label, dash_inferred=True)


def to_png(model, tables, relationships, label, all_columns=False, max_pixels=None, min_dpi=None):
    """The view as PNG bytes. Key columns only, unless all_columns.
    max_pixels / min_dpi: tighter limits than the export's own (the report's image has to stay readable)."""
    em = _er_model(model, tables, relationships)
    from .er.render_png import DiagramTooLarge, render      # imported late: matplotlib is slow to load
    out = io.BytesIO()
    with _png_lock:
        try:
            render(em, out, label, all_columns, dash_inferred=True, max_pixels=max_pixels or config.PNG_MAX_PIXELS,
                   max_side=config.PNG_MAX_SIDE, min_dpi=min_dpi or config.PNG_MIN_DPI)
        except DiagramTooLarge as exc:
            raise TooBigForPng(
                f'This view is too big for a PNG ({len(tables):,} tables, about {round(exc.width):,} x '
                f'{round(exc.height):,} px before scaling). Export it as .drawio instead, or use focus mode '
                'or the schema filter to export part of it.') from None
    return out.getvalue()


def file_name(*parts):
    """A safe download name from workspace / table names."""
    text = '-'.join(p for p in parts if p)
    text = re.sub(r'[^A-Za-z0-9._-]+', '_', text).strip('._-')
    return text[:80] or 'er-diagram'
