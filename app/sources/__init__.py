"""Source adapters. Each one turns a source into the common SchemaModel (app.model).

    sqlfiles.py   .sql files  -> parser from the ER scripts
    database.py   live database -> SQLAlchemy inspector, read-only
    files.py      CSV / Excel exports -> DuckDB type detection
"""

import codecs


class SourceError(Exception):
    """A problem with a source that the user can fix (bad path, cannot connect, ...)."""


def read_text(path):
    """Read a text file whatever its encoding: UTF-8 (with or without BOM), UTF-16, or the PC's code page."""
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode('utf-16', errors='replace')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        import locale
        for enc in (locale.getpreferredencoding(False), 'cp1252'):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode('utf-8', errors='replace')
