"""Copy the SQL Lineage engine into the app: C:\\Projects\\SQL_Lineage\\sql_lineage -> app\\sql_lineage.

    .venv\\Scripts\\python.exe -m scripts.sync_lineage        (or: .venv\\Scripts\\python.exe scripts\\sync_lineage.py)

app/sql_lineage/ is a copy and is never edited by hand. To change the engine: edit it in its own project,
run that project's tests, run this, then run the app's tests. The source is the DATA_EXPLORER_LINEAGE_SOURCE
setting (LINEAGE_SOURCE in app/config.py), by default the sql_lineage folder of the sibling project.

Every file is copied byte for byte, __pycache__ folders left out. Files the source no longer has are
removed from the copy. It prints what changed, and warns when the two projects pin different sqlglot
versions. tests/test_scripts_sync.py checks that the copy equals the source.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if __package__ in (None, ''):               # run as a file: make "import app" work
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

SKIP_FOLDERS = ('__pycache__',)


def engine_files(folder):
    """{relative path with /: Path} for every file under folder, __pycache__ left out."""
    folder = Path(folder)
    found = {}
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if path.is_file() and not any(part in SKIP_FOLDERS for part in relative.parts):
            found[relative.as_posix()] = path
    return found


def sync(source, target):
    """Make the folder target equal to source. Returns {'added': [...], 'changed': [...], 'removed': [...]}."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if not (source / '__init__.py').is_file():
        raise SystemExit(f'{source} is not the sql_lineage folder of SQL Lineage (no __init__.py). '
                         'Set DATA_EXPLORER_LINEAGE_SOURCE to that folder.')
    if source == target or target.is_relative_to(source) or source.is_relative_to(target):
        raise SystemExit(f'the source {source} and the copy {target} must be two separate folders')
    new = engine_files(source)
    old = engine_files(target) if target.is_dir() else {}
    changes = {'added': [], 'changed': [], 'removed': []}
    for name, path in new.items():
        data = path.read_bytes()
        if name not in old:
            changes['added'].append(name)
        elif old[name].read_bytes() != data:
            changes['changed'].append(name)
        else:
            continue
        out = target / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    for name in sorted(set(old) - set(new)):
        old[name].unlink()
        changes['removed'].append(name)
    # Folders a removal left empty (deepest first).
    for folder in sorted((p for p in target.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(folder.iterdir()):
            folder.rmdir()
    return changes


def sqlglot_pin(requirements):
    """The version after 'sqlglot==' in a requirements file, or None."""
    path = Path(requirements)
    if not path.is_file():
        return None
    match = re.search(r'^\s*sqlglot==([\w.]+)', path.read_text(encoding='utf-8'), re.M)
    return match.group(1) if match else None


def main():
    source, target = config.LINEAGE_SOURCE, config.LINEAGE_COPY
    changes = sync(source, target)
    print(f'From {Path(source).resolve()}')
    print(f'to   {Path(target).resolve()}')
    for kind in ('added', 'changed', 'removed'):
        for name in changes[kind]:
            print(f'  {kind:<8} {name}')
    count = sum(len(names) for names in changes.values())
    print(f'{count} file(s) changed.' if count else 'No change: the copy already equals the source.')
    theirs = sqlglot_pin(Path(source).resolve().parent / 'requirements.txt')
    ours = sqlglot_pin(config.BASE_DIR / 'requirements.txt')
    if theirs and theirs != ours:
        print(f'Warning: SQL Lineage pins sqlglot=={theirs}, this app pins sqlglot=={ours}. '
              'Change requirements.txt to the same version.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
