"""The Scripts tab's engine: app/sql_lineage/ is a byte-for-byte copy of the SQL Lineage project's
sql_lineage folder, refreshed by scripts/sync_lineage.py (never edited in this project)."""

import re

import pytest

from app import config
from scripts.sync_lineage import engine_files, sqlglot_pin, sync

SOURCE = config.LINEAGE_SOURCE


def test_the_engine_copy_equals_the_source():
    if not SOURCE.is_dir():
        pytest.skip(f'the SQL Lineage source {SOURCE} is not on this PC (DATA_EXPLORER_LINEAGE_SOURCE)')
    source, copy = engine_files(SOURCE), engine_files(config.LINEAGE_COPY)
    assert sorted(copy) == sorted(source), 'run: .venv\\Scripts\\python.exe -m scripts.sync_lineage'
    for name, path in source.items():
        assert copy[name].read_bytes() == path.read_bytes(), f'{name} differs: run scripts\\sync_lineage.py'


def test_requirements_pin_the_sqlglot_version_of_sql_lineage():
    import sqlglot
    ours = sqlglot_pin(config.BASE_DIR / 'requirements.txt')
    assert re.fullmatch(r'\d+\.\d+\.\d+', ours)
    assert sqlglot.__version__ == ours
    theirs = config.LINEAGE_SOURCE.parent / 'requirements.txt'
    if not theirs.is_file():
        pytest.skip(f'{theirs} is not on this PC')
    assert sqlglot_pin(theirs) == ours


def test_sync_adds_changes_and_removes_files_and_leaves_pycache_out(tmp_path):
    source, copy = tmp_path / 'source', tmp_path / 'copy'
    for name, data in {'__init__.py': b'', 'a.py': b'A = 1\r\n', 'sub/b.py': b'B = 2\n',
                       '__pycache__/a.cpython-312.pyc': b'\x00'}.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_bytes(data)
    assert sync(source, copy) == {'added': ['__init__.py', 'a.py', 'sub/b.py'], 'changed': [], 'removed': []}
    assert not (copy / '__pycache__').exists()
    assert (copy / 'a.py').read_bytes() == b'A = 1\r\n'                 # line endings kept
    (source / 'a.py').write_bytes(b'A = 3\n')
    (source / 'sub' / 'b.py').unlink()
    (source / 'sub').rmdir()
    (source / 'c.py').write_bytes(b'C = 4\n')
    assert sync(source, copy) == {'added': ['c.py'], 'changed': ['a.py'], 'removed': ['sub/b.py']}
    assert sorted(engine_files(copy)) == ['__init__.py', 'a.py', 'c.py'] and not (copy / 'sub').exists()
    assert sync(source, copy) == {'added': [], 'changed': [], 'removed': []}


def test_sync_refuses_a_folder_that_is_not_the_engine(tmp_path):
    with pytest.raises(SystemExit, match='not the sql_lineage folder'):
        sync(tmp_path, tmp_path / 'copy')
