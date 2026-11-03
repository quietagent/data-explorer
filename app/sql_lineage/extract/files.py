"""Walks the scanned folder and reads each script's bytes and text.

Only files whose ending is in settings.SCRIPT_EXTENSIONS are taken, in every
subfolder except settings.SKIP_FOLDERS. Files are returned sorted by path.
More than settings.MAX_FILES of them is an error; a file bigger than
settings.MAX_FILE_MB is not read and gets REASON_TOO_BIG as its problem.
"""

import hashlib
from pathlib import Path

from sql_lineage import settings
from sql_lineage.extract.model import ScriptFile


def find_scripts(folder):
    """Return the script paths under `folder`, sorted, as Path objects."""
    folder = Path(folder)
    found = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if any(part in settings.SKIP_FOLDERS for part in relative.parts[:-1]):
            continue
        if path.suffix.lower() in settings.SCRIPT_EXTENSIONS:
            found.append(path)
    if len(found) > settings.MAX_FILES:
        raise ValueError(f"{folder} holds {len(found)} script files, more than MAX_FILES = {settings.MAX_FILES}; "
                         "scan a smaller folder, or raise MAX_FILES in settings.py")
    return sorted(found, key=lambda p: p.relative_to(folder).as_posix().lower())


def read_script(folder, path):
    """Read one script. Returns (ScriptFile, text or None when it can't be decoded or is too big)."""
    relative = Path(path).relative_to(folder).as_posix()
    kind = settings.SCRIPT_EXTENSIONS[Path(path).suffix.lower()]
    size = Path(path).stat().st_size
    if size > settings.MAX_FILE_MB * 1024 * 1024:
        reason = settings.REASON_TOO_BIG.format(mb=settings.MAX_FILE_MB)
        return ScriptFile(path=relative, kind=kind, size=size, sha256="", encoding=None, problem=reason), None
    data = Path(path).read_bytes()
    encoding, text = decode(data)
    script = ScriptFile(
        path=relative,
        kind=kind,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        encoding=encoding,
        problem=None if encoding else settings.REASON_NOT_DECODED,
    )
    return script, text


def decode(data):
    """Decode bytes with the first encoding in settings.ENCODINGS that works.

    Returns (encoding name, text with \\n line ends), or (None, None).
    A file starting with a UTF-16 byte order mark (settings.UTF16_BOMS) is read as
    UTF-16. Otherwise a result holding a NUL character is refused: it is a UTF-16
    file without a mark, or a binary file.
    """
    for mark, name in settings.UTF16_BOMS.items():
        if data.startswith(mark):
            try:
                text = data[len(mark):].decode(name)
            except UnicodeDecodeError:
                return None, None
            if "\x00" in text:
                return None, None
            return name, text.replace("\r\n", "\n")
    for encoding in settings.ENCODINGS:
        name = encoding
        if encoding == "utf-8" and data.startswith(b"\xef\xbb\xbf"):
            name = "utf-8-sig"
        try:
            text = data.decode(name)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            continue
        return name, text.replace("\r\n", "\n")
    return None, None
