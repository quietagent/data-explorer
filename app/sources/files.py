"""Exported files source: each CSV file, and each sheet of an Excel workbook, is a table.

A CSV file's table is named after the file. A workbook with one sheet is named
after the file too; with several sheets each table is "<file>.<sheet>" (the file
name becomes the table's schema), so sheets called "Sheet1" in two workbooks stay
apart. Every table's origin records the file's path relative to the source
folder, and the sheet, so the data can be read again for profiling.

Column types are detected in memory. A file is read as text (every column
VARCHAR) into an in-memory DuckDB, and each column gets the first type that every
one of its values can be read as: a bad row far down the file cannot fail the
load, and a code such as 0550000001 keeps its leading zero. Types come from the
first FILE_TYPE_ROWS rows of a file. Nothing is written anywhere while a source
is added: no temporary file, no copy. Relationships are inferred from the column
names (app.model.infer_by_names), which reuses the pk_like / guess_one logic of
the ER scripts.

Nothing here needs the network: DuckDB's extension auto-install and auto-load
are switched off.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from pathlib import Path

from .. import config
from ..model import Origin, SchemaModel, finalize_model
from ..security import sanitize_error
from . import SourceError

CSV_EXT = ('.csv', '.tsv', '.txt')
EXCEL_EXT = ('.xlsx', '.xlsm')
DATA_EXT = CSV_EXT + EXCEL_EXT
MAX_FILES = 2000


def source_root(path):
    """The folder that the paths stored in a files source are relative to."""
    p = Path(str(path).strip().strip('"')).expanduser()
    return p if p.is_dir() else p.parent


def relative_path(file, root):
    """`file` relative to the source folder, with '/' separators (the file name if it is elsewhere)."""
    try:
        return Path(file).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(file).name


def collect_data_files(path):
    """A data file, or every CSV / Excel file under a folder."""
    p = Path(str(path).strip().strip('"')).expanduser()
    if p.is_dir():
        files = sorted(f for f in p.rglob('*') if f.is_file() and f.suffix.lower() in DATA_EXT
                       and not f.name.startswith(('~$', '.')))
    elif p.is_file():
        files = [p]
    else:
        raise SourceError(f'Not found: {p}')
    if not files:
        raise SourceError(f'No CSV or Excel files found in {p}')
    if len(files) > MAX_FILES:
        raise SourceError(f'{len(files)} files found; the limit is {MAX_FILES}. Point to a smaller folder.')
    return files


def open_duckdb():
    """An in-memory DuckDB that cannot spill to disk, download or load extensions (app.sources.duck)."""
    from .duck import open_memory
    return open_memory()


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d') if value.time() == time(0) else value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))                          # Excel stores whole numbers as 5.0
    return str(value)


def sheet_rows(ws):
    """The rows of a worksheet as lists of text, header first: blank rows above the header are
    skipped and the header row sets the width. Shared by type detection and by profiling."""
    width = None
    for row in ws.iter_rows(values_only=True):
        if width is None:
            if all(v is None or str(v).strip() == '' for v in row):
                continue                                # blank rows above the header
            while row and row[-1] is None:              # the header row sets the width
                row = row[:-1]
            width = len(row)
        cells = [_cell(v) for v in row[:width]]
        yield cells + [''] * (width - len(cells))


def sheet_names(path):
    """The sheets of a workbook that hold something, in order."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return [ws.title for ws in wb.worksheets if next(iter(sheet_rows(ws)), None)]
    finally:
        wb.close()


def describe(path, sheet=None, rows=None):
    """Column names and types of one CSV file or Excel sheet, detected in memory from its first rows.
    Returns ([(column, type)], encoding if it is not UTF-8, True if the file has more rows than were looked at)."""
    from ..profiling.data import FileData            # imported late: profiling.data imports this module
    data = FileData(path, sheet, limit=rows or config.FILE_TYPE_ROWS).open()
    try:
        utf8 = (data.encoding or 'utf-8').lower().replace('-', '') in ('utf8', 'utf8sig')
        return list(data.columns), None if utf8 else data.encoding, data.more
    finally:
        data.close()


def read_files(files, label='', source_id=None, root=None):
    """Read the structure of CSV / Excel files into one SchemaModel. Returns (model, warnings).

    root: the source folder; each table's origin stores its file's path relative to it.
    """
    files = [Path(f) for f in files]
    if not files:
        raise SourceError('No CSV or Excel files were given.')
    if root is None:
        root = Path(os.path.commonpath([f.resolve().parent for f in files]))
    model, warnings = SchemaModel(), []
    sampled = f'types detected from the first {config.FILE_TYPE_ROWS:,} rows.'
    for f in files:
        ext = f.suffix.lower()
        rel = relative_path(f, root)
        try:
            if ext in EXCEL_EXT:
                sheets = sheet_names(f)
                for sheet in sheets:
                    # one sheet: the file's name; several: "<file>.<sheet>" (the file is the schema)
                    name, schema = (f.stem, None) if len(sheets) == 1 else (sheet, f.stem)
                    detail = f'{rel} · sheet {sheet}'
                    columns, _, more = describe(f, sheet)
                    _add_table(model, name, schema, columns, Origin('file', source_id, label, detail, path=rel, sheet=sheet))
                    if more:
                        warnings.append(f'{detail}: {sampled}')
                if not sheets:
                    warnings.append(f'{rel}: no data found.')
            elif ext in CSV_EXT:
                columns, encoding, more = describe(f)
                origin = Origin('file', source_id, label, rel, path=rel)
                if encoding:
                    origin.encoding = encoding
                    warnings.append(f'{rel}: not UTF-8, read as {encoding}.')
                if more:
                    warnings.append(f'{rel}: {sampled}')
                _add_table(model, f.stem, None, columns, origin)
            else:
                warnings.append(f'{rel}: not a CSV or Excel file, skipped.')
        except Exception as exc:                    # one bad file must not stop the others
            warnings.append(f'{rel}: could not be read ({_short(exc)})')
    if not model.tables:
        raise SourceError('No tables could be read. ' + ' '.join(warnings[:3]))
    finalize_model(model, names=True)
    return model, warnings[:50]


def _add_table(model, name, schema, columns, origin):
    t = model.table(name.strip(), schema.strip() if schema else None)
    t.add_origin(origin)
    for column, type_name in columns:
        t.add_column(column, 'file', type=type_name)    # nullable is unknown until the data is profiled


def _short(exc):
    """An error as it may be stored with the source: one line, nothing quoted from the file's rows."""
    return sanitize_error(str(exc), limit=200) or type(exc).__name__
