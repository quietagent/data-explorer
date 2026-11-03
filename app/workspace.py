"""Workspaces: a named set of sources and the merged model they produce.

Saved in data/explorer.db (SQLite): workspaces, their sources, and connection
profiles. For each source the app keeps its settings and the structure it read
(table and column names, types, keys) - never data rows, and never a password.
Database passwords live in this process's memory only and are gone when the
app stops.

The file's layout has a version (PRAGMA user_version). Version 1 is session 1's
three tables; version 2 adds the saved profiling results (app/profiling/store.py);
version 3 adds relationship checks, value links and the SQL history
(app/analysis/store.py); version 4 adds the Scripts tab's last scan per workspace
(script_scans, same file). Opening an older file upgrades it in place; nothing in
it is changed or lost.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import demo
from .analysis.store import SCHEMA_V4, migrate_v3
from .model import SchemaModel, finalize_model
from .profiling.store import SCHEMA_V2
from .sources import SourceError
from .sources.database import ConnectionProfile, ReadOnlyDatabase
from .sources.files import collect_data_files, read_files, source_root
from .sources.sqlfiles import collect_sql_files, read_sql_files

SCHEMA_VERSION = 4
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS workspaces (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    kind          TEXT NOT NULL,          -- sql_files / database / files
    label         TEXT NOT NULL,
    settings      TEXT NOT NULL,          -- JSON; a database profile has no password field
    snapshot      TEXT NOT NULL,          -- JSON; the structure this source produced
    warnings      TEXT NOT NULL,          -- JSON list
    created_at    TEXT NOT NULL,
    refreshed_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    settings    TEXT NOT NULL,            -- JSON; engine, host, port, database, user, schema filter
    created_at  TEXT NOT NULL
);
"""

# version -> what it adds: a script of CREATE TABLE IF NOT EXISTS statements, or a function (connection)
MIGRATIONS = {1: SCHEMA_V1, 2: SCHEMA_V2, 3: migrate_v3, 4: SCHEMA_V4}

KINDS = ('sql_files', 'database', 'files')


class NotFound(Exception):
    pass


class NeedsPassword(Exception):
    """A database source has to be re-read but its password is no longer in memory."""


def _now():
    return datetime.now().isoformat(timespec='seconds')


def new_id():
    return secrets.token_hex(6)


def remove_folder(folder, tries=20):
    """Delete a folder of uploaded copies and say whether it is gone. On Windows a file that is
    still being read cannot be deleted, so this tries again for a moment instead of giving up."""
    folder = Path(folder)
    for attempt in range(tries):
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            return True
        time.sleep(0.1 * (attempt + 1))
    return not folder.exists()


class Workspaces:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'explorer.db'
        self.uploads_dir = self.data_dir / 'uploads'
        self._passwords = {}            # (workspace id, source id) -> password. Memory only.
        self._models = {}               # workspace id -> merged SchemaModel
        self._lock = threading.RLock()
        self.on_change = []             # callbacks (workspace id, removed source id or None, deleted), after a change
        self.on_remove = []             # callbacks (workspace id, source id or None for the whole workspace),
                                        # BEFORE it is removed: running jobs are stopped and sealed here
        self.extend_model = []          # callbacks (workspace id, merged model): add what is not in any source
        self._migrate()

    def _migrate(self):
        """Bring data/explorer.db up to SCHEMA_VERSION, one version at a time."""
        with self._db() as con:
            version = con.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SourceError(f'{self.db_path} was written by a newer version of Data Explorer '
                                  f'(layout {version}; this version knows {SCHEMA_VERSION}).')
            for step in range(version + 1, SCHEMA_VERSION + 1):
                if callable(MIGRATIONS[step]):
                    MIGRATIONS[step](con)
                else:
                    con.executescript(MIGRATIONS[step])
                con.execute(f'PRAGMA user_version = {step}')

    @contextmanager
    def _db(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        con.execute('PRAGMA secure_delete = ON')        # deleted or replaced values are overwritten, not left behind
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------ workspaces
    def list_workspaces(self):
        with self._db() as con:
            rows = con.execute(
                'SELECT w.*, (SELECT COUNT(*) FROM sources s WHERE s.workspace_id = w.id) AS source_count '
                'FROM workspaces w ORDER BY w.updated_at DESC, w.name').fetchall()
        return [dict(r) for r in rows]

    def create_workspace(self, name, workspace_id=None):
        name = (name or '').strip() or 'Untitled workspace'
        workspace_id = workspace_id or new_id()
        with self._db() as con:
            con.execute('INSERT INTO workspaces (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)',
                        (workspace_id, name[:120], _now(), _now()))
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id):
        with self._db() as con:
            row = con.execute('SELECT * FROM workspaces WHERE id = ?', (workspace_id,)).fetchone()
            if row is None:
                raise NotFound('Workspace not found.')
            sources = con.execute('SELECT * FROM sources WHERE workspace_id = ? ORDER BY position',
                                  (workspace_id,)).fetchall()
        return {**dict(row), 'sources': [self._public(s) for s in sources]}

    def rename_workspace(self, workspace_id, name):
        name = (name or '').strip()
        if not name:
            raise SourceError('A workspace needs a name.')
        with self._db() as con:
            done = con.execute('UPDATE workspaces SET name = ?, updated_at = ? WHERE id = ?',
                               (name[:120], _now(), workspace_id)).rowcount
        if not done:
            raise NotFound('Workspace not found.')
        return self.get_workspace(workspace_id)

    def delete_workspace(self, workspace_id):
        self.get_workspace(workspace_id)
        self._removing(workspace_id)                    # its running job is cancelled and may not save any more
        with self._db() as con:
            done = con.execute('DELETE FROM workspaces WHERE id = ?', (workspace_id,)).rowcount
        if not done:
            raise NotFound('Workspace not found.')
        with self._lock:
            self._models.pop(workspace_id, None)
            for key in [k for k in self._passwords if k[0] == workspace_id]:
                del self._passwords[key]
        remove_folder(self.uploads_dir / workspace_id)
        self._changed(workspace_id, deleted=True)

    def _public(self, row):
        """A source as the UI sees it: no snapshot, and never anything secret."""
        settings = json.loads(row['settings'])
        snapshot = json.loads(row['snapshot'])
        held = (row['workspace_id'], row['id']) in self._passwords
        needs = row['kind'] == 'database' and settings.get('engine') != 'sqlite' and not held
        return {'id': row['id'], 'kind': row['kind'], 'label': row['label'], 'settings': settings,
                'tables': len(snapshot.get('tables', [])),
                'relationships': len(snapshot.get('relationships', [])),
                'default_schema': snapshot.get('default_schema'),      # database sources; None until read again
                'warnings': json.loads(row['warnings']), 'created_at': row['created_at'],
                'refreshed_at': row['refreshed_at'], 'needs_password': needs, 'has_password': held}

    def _touch(self, con, workspace_id):
        con.execute('UPDATE workspaces SET updated_at = ? WHERE id = ?', (_now(), workspace_id))
        with self._lock:
            self._models.pop(workspace_id, None)

    def forget_model(self, workspace_id):
        """The merged model is built again when it is next asked for (a value link was accepted or dropped)."""
        with self._lock:
            self._models.pop(workspace_id, None)

    def _changed(self, workspace_id, removed_source=None, deleted=False):
        for callback in self.on_change:
            callback(workspace_id, removed_source, deleted)

    def _removing(self, workspace_id, source_id=None):
        for callback in self.on_remove:
            callback(workspace_id, source_id)

    # --------------------------------------------------------------- sources
    def upload_dir(self, workspace_id, source_id):
        return self.uploads_dir / workspace_id / source_id

    def add_source(self, workspace_id, kind, settings, label='', password=None, source_id=None):
        """Read a source and add it to the workspace. Returns the public source dict.

        settings: sql_files / files -> {'mode': 'path' | 'upload', 'path': ...}
                  database          -> a ConnectionProfile dict (no password)
        """
        if kind not in KINDS:
            raise SourceError('Unknown kind of source.')
        self.get_workspace(workspace_id)
        source_id = source_id or new_id()
        if kind == 'database':
            settings = ConnectionProfile.from_dict(settings).to_dict()
        label = (label or '').strip()[:120] or self._default_label(kind, settings)
        model, warnings = self._read(kind, settings, password, label, source_id)
        with self._db() as con:
            position = con.execute('SELECT COALESCE(MAX(position), 0) + 1 FROM sources WHERE workspace_id = ?',
                                   (workspace_id,)).fetchone()[0]
            con.execute('INSERT INTO sources (id, workspace_id, position, kind, label, settings, snapshot, '
                        'warnings, created_at, refreshed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (source_id, workspace_id, position, kind, label, json.dumps(settings),
                         json.dumps(model.to_dict()), json.dumps(warnings), _now(), _now()))
            self._touch(con, workspace_id)
        if kind == 'database' and password:
            with self._lock:
                self._passwords[(workspace_id, source_id)] = password
        return self._source(workspace_id, source_id)

    def refresh_source(self, workspace_id, source_id, password=None):
        """Read a source again (new files in the folder, changed tables in the database)."""
        with self._db() as con:
            row = con.execute('SELECT * FROM sources WHERE id = ? AND workspace_id = ?',
                              (source_id, workspace_id)).fetchone()
        if row is None:
            raise NotFound('Source not found.')
        settings = json.loads(row['settings'])
        if row['kind'] == 'database' and settings.get('engine') != 'sqlite':
            with self._lock:
                password = password or self._passwords.get((workspace_id, source_id))
            if not password:
                raise NeedsPassword('Enter the password to read this database again.')
        model, warnings = self._read(row['kind'], settings, password, row['label'], source_id)
        with self._db() as con:
            con.execute('UPDATE sources SET snapshot = ?, warnings = ?, refreshed_at = ? WHERE id = ?',
                        (json.dumps(model.to_dict()), json.dumps(warnings), _now(), source_id))
            self._touch(con, workspace_id)
        if row['kind'] == 'database' and password:
            with self._lock:
                self._passwords[(workspace_id, source_id)] = password
        self._changed(workspace_id)
        return self._source(workspace_id, source_id)

    def remove_source(self, workspace_id, source_id):
        with self._db() as con:
            found = con.execute('SELECT 1 FROM sources WHERE id = ? AND workspace_id = ?',
                                (source_id, workspace_id)).fetchone()
        if found is None:
            raise NotFound('Source not found.')
        self._removing(workspace_id, source_id)         # a job reading it is cancelled and may not save any more
        with self._db() as con:
            done = con.execute('DELETE FROM sources WHERE id = ? AND workspace_id = ?',
                               (source_id, workspace_id)).rowcount
            if done:
                self._touch(con, workspace_id)
        if not done:
            raise NotFound('Source not found.')
        with self._lock:
            self._passwords.pop((workspace_id, source_id), None)
        remove_folder(self.upload_dir(workspace_id, source_id))     # the copies of uploaded files
        parent = self.uploads_dir / workspace_id
        if parent.is_dir() and not any(parent.iterdir()):
            remove_folder(parent)
        self._changed(workspace_id, source_id)

    def forget_password(self, workspace_id, source_id):
        """Drop a database source's password from memory. Returns the public source dict."""
        with self._db() as con:
            found = con.execute('SELECT 1 FROM sources WHERE id = ? AND workspace_id = ?',
                                (source_id, workspace_id)).fetchone()
        if found is None:
            raise NotFound('Source not found.')
        with self._lock:
            self._passwords.pop((workspace_id, source_id), None)
        return self._source(workspace_id, source_id)

    def password(self, workspace_id, source_id):
        """The password held in memory for a database source, or None."""
        with self._lock:
            return self._passwords.get((workspace_id, source_id))

    def set_password(self, workspace_id, source_id, password):
        """Keep a database source's password in memory (never on disk) for this run of the app."""
        if password:
            with self._lock:
                self._passwords[(workspace_id, source_id)] = password

    def source_rows(self, workspace_id):
        """Every source of a workspace with its settings and the structure it read (for profiling)."""
        with self._db() as con:
            if con.execute('SELECT 1 FROM workspaces WHERE id = ?', (workspace_id,)).fetchone() is None:
                raise NotFound('Workspace not found.')
            rows = con.execute('SELECT id, kind, label, settings, snapshot FROM sources WHERE workspace_id = ? '
                               'ORDER BY position', (workspace_id,)).fetchall()
        return [{'id': r['id'], 'kind': r['kind'], 'label': r['label'], 'settings': json.loads(r['settings']),
                 'model': SchemaModel.from_dict(json.loads(r['snapshot']))} for r in rows]

    def _source(self, workspace_id, source_id):
        return next(s for s in self.get_workspace(workspace_id)['sources'] if s['id'] == source_id)

    @staticmethod
    def _default_label(kind, settings):
        if kind == 'database':
            return ConnectionProfile.from_dict(settings).describe()
        names = settings.get('files') or []
        if settings.get('mode') == 'upload' and names:
            return names[0] if len(names) == 1 else f'{names[0]} + {len(names) - 1} more'
        return Path(settings.get('path') or '').name or ('SQL files' if kind == 'sql_files' else 'Files')

    @staticmethod
    def _read(kind, settings, password, label, source_id):
        if kind == 'sql_files':
            return read_sql_files(collect_sql_files(settings.get('path') or ''), label, source_id)
        if kind == 'files':
            path = settings.get('path') or ''
            return read_files(collect_data_files(path), label, source_id, root=source_root(path))
        db = ReadOnlyDatabase(ConnectionProfile.from_dict(settings), password)
        try:
            return db.read_schema(label, source_id)
        finally:
            db.dispose()

    # ----------------------------------------------------------------- model
    def model(self, workspace_id):
        """The merged model of a workspace: every source folded into one, then inference."""
        with self._lock:
            cached = self._models.get(workspace_id)
        if cached is not None:
            return cached
        with self._db() as con:
            if con.execute('SELECT 1 FROM workspaces WHERE id = ?', (workspace_id,)).fetchone() is None:
                raise NotFound('Workspace not found.')
            rows = con.execute('SELECT snapshot FROM sources WHERE workspace_id = ? ORDER BY position',
                               (workspace_id,)).fetchall()
        merged = SchemaModel()
        for row in rows:
            merged.merge(SchemaModel.from_dict(json.loads(row['snapshot'])))
        for extend in self.extend_model:                # links the user accepted (kind "values")
            extend(workspace_id, merged)
        finalize_model(merged, names=True)
        with self._lock:
            self._models[workspace_id] = merged
        return merged

    # -------------------------------------------------------------- profiles
    def list_profiles(self):
        with self._db() as con:
            rows = con.execute('SELECT * FROM profiles ORDER BY name').fetchall()
        return [{'id': r['id'], 'name': r['name'], **json.loads(r['settings'])} for r in rows]

    def save_profile(self, name, settings):
        """Save connection settings under a name. The password is not part of a profile."""
        profile = ConnectionProfile.from_dict(settings)
        name = (name or '').strip()[:120] or profile.describe()
        with self._db() as con:
            con.execute('INSERT INTO profiles (id, name, settings, created_at) VALUES (?, ?, ?, ?) '
                        'ON CONFLICT (name) DO UPDATE SET settings = excluded.settings',
                        (new_id(), name, json.dumps(profile.to_dict()), _now()))
        return next(p for p in self.list_profiles() if p['name'] == name)

    def delete_profile(self, profile_id):
        with self._db() as con:
            done = con.execute('DELETE FROM profiles WHERE id = ?', (profile_id,)).rowcount
        if not done:
            raise NotFound('Profile not found.')

    # ------------------------------------------------------------------ demo
    def ensure_demo(self):
        """(Re)create the demo workspace: the SQLite database, the .sql file and the CSV exports."""
        folder = demo.build_demo()
        try:
            self.delete_workspace(demo.DEMO_WORKSPACE_ID)
        except NotFound:
            pass
        self.create_workspace(demo.DEMO_WORKSPACE_NAME, demo.DEMO_WORKSPACE_ID)
        self.add_source(demo.DEMO_WORKSPACE_ID, 'database',
                        {'engine': 'sqlite', 'database': str(folder / 'telecom.db')}, 'telecom.db (live SQLite)')
        self.add_source(demo.DEMO_WORKSPACE_ID, 'sql_files',
                        {'mode': 'path', 'path': str(folder / 'telecom.sql')}, 'telecom.sql (DDL + queries)')
        self.add_source(demo.DEMO_WORKSPACE_ID, 'files',
                        {'mode': 'path', 'path': str(folder / 'csv')}, 'csv exports')
        return demo.DEMO_WORKSPACE_ID
