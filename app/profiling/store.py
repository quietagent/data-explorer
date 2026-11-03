"""Saved profiling results, in data/explorer.db (tables of layout version 2).

What is saved: row counts, and per column the statistics of the last profile, with a timestamp.
What is never saved: rows of data. The top values, minimum and maximum of personal columns are
masked before they get here (the service masks at the moment of saving, with the marks as they
are then), and mask_column() masks what is already stored when the user marks a column personal
afterwards.

The user's own "personal / not personal" marks live here too. They are settings, not statistics:
clear() keeps them.
"""

from __future__ import annotations

import json
from datetime import datetime

from .personal import mask_stats

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS table_stats (
    workspace_id    TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    table_key       TEXT NOT NULL,          -- the table's id in the merged model, upper case
    source_id       TEXT,                   -- the source the numbers were read from
    row_count       INTEGER,
    row_count_kind  TEXT,                   -- estimate (the engine's statistics) / exact (counted) /
                                            -- more_than (at least this many: the read stopped there)
    counted_at      TEXT,
    profile         TEXT,                   -- JSON: sampled or not, method, sample size, notes. NULL = only counted
    profiled_at     TEXT,
    PRIMARY KEY (workspace_id, table_key)
);
CREATE TABLE IF NOT EXISTS column_stats (
    workspace_id    TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    table_key       TEXT NOT NULL,
    column_key      TEXT NOT NULL,          -- column name, upper case
    position        INTEGER NOT NULL,
    stats           TEXT NOT NULL,          -- JSON: counts, min / max / avg, lengths, top values, histogram.
                                            -- Personal columns: values masked. Never rows of data.
    profiled_at     TEXT NOT NULL,
    PRIMARY KEY (workspace_id, table_key, column_key)
);
CREATE TABLE IF NOT EXISTS personal_marks (
    workspace_id    TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    table_key       TEXT NOT NULL,
    column_key      TEXT NOT NULL,
    personal        INTEGER NOT NULL,       -- 1 = marked personal by the user, 0 = marked not personal
    marked_at       TEXT NOT NULL,
    PRIMARY KEY (workspace_id, table_key, column_key)
);
"""


def _now():
    return datetime.now().isoformat(timespec='seconds')


class ProfileStore:
    def __init__(self, db):
        self._db = db                   # Workspaces._db: a context manager giving a sqlite3 connection

    # ------------------------------------------------------------ row counts
    def set_count(self, workspace_id, table_key, source_id, rows, kind):
        """Record a row count. Only an exact count replaces an exact count of the same source: an estimate
        does not, and "more than 100,000" - which is no count at all - never does."""
        with self._db() as con:
            row = con.execute('SELECT row_count_kind, source_id FROM table_stats WHERE workspace_id = ? AND table_key = ?',
                              (workspace_id, table_key)).fetchone()
            if row is None:
                con.execute('INSERT INTO table_stats (workspace_id, table_key, source_id, row_count, row_count_kind, '
                            'counted_at) VALUES (?, ?, ?, ?, ?, ?)', (workspace_id, table_key, source_id, rows, kind, _now()))
            elif kind == 'exact' or row['row_count_kind'] != 'exact' or row['source_id'] != source_id:
                con.execute('UPDATE table_stats SET source_id = ?, row_count = ?, row_count_kind = ?, counted_at = ? '
                            'WHERE workspace_id = ? AND table_key = ?',
                            (source_id, rows, kind, _now(), workspace_id, table_key))

    # --------------------------------------------------------------- profiles
    def save_profile(self, workspace_id, table_key, source_id, table, columns):
        """Replace a table's saved profile. columns: [(column name, stats dict)] in table order.

        The profile brings the row count it learnt on the way. When that is only "more than N" and an exact
        count of the same source is saved, the exact count stays, with the time it was taken: a profile
        never throws a count away. A newer exact count replaces it, and so does the catalog's own figure.
        """
        now = _now()
        summary = {k: v for k, v in table.items() if k not in ('row_count', 'row_count_kind')}
        rows, kind, counted = table.get('row_count'), table.get('row_count_kind'), now
        with self._db() as con:
            old = con.execute('SELECT source_id, row_count, row_count_kind, counted_at FROM table_stats '
                              'WHERE workspace_id = ? AND table_key = ?', (workspace_id, table_key)).fetchone()
            if (old is not None and kind == 'more_than' and old['row_count_kind'] == 'exact' and old['source_id'] == source_id
                    and (old['row_count'] or 0) >= (rows or 0)):
                rows, kind, counted = old['row_count'], 'exact', old['counted_at']
            con.execute('DELETE FROM table_stats WHERE workspace_id = ? AND table_key = ?', (workspace_id, table_key))
            con.execute('INSERT INTO table_stats (workspace_id, table_key, source_id, row_count, row_count_kind, '
                        'counted_at, profile, profiled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        (workspace_id, table_key, source_id, rows, kind, counted, json.dumps(summary), now))
            con.execute('DELETE FROM column_stats WHERE workspace_id = ? AND table_key = ?', (workspace_id, table_key))
            con.executemany('INSERT INTO column_stats (workspace_id, table_key, column_key, position, stats, profiled_at) '
                            'VALUES (?, ?, ?, ?, ?, ?)',
                            [(workspace_id, table_key, name.upper(), i, json.dumps(stats), now)
                             for i, (name, stats) in enumerate(columns)])

    def set_attempt(self, workspace_id, table_key, source_id, status, message):
        """Record that profiling a table did not finish: 'timed_out' or 'failed'. What an earlier
        profile saved for it stays; a later successful profile clears the note."""
        attempt = json.dumps({'status': status, 'message': message, 'at': _now()})
        with self._db() as con:
            done = con.execute('UPDATE table_stats SET attempt = ? WHERE workspace_id = ? AND table_key = ?',
                               (attempt, workspace_id, table_key)).rowcount
            if not done:
                con.execute('INSERT INTO table_stats (workspace_id, table_key, source_id, attempt) VALUES (?, ?, ?, ?)',
                            (workspace_id, table_key, source_id, attempt))

    def tables(self, workspace_id):
        """{table_key: {source_id, row_count, row_count_kind, counted_at, profile, profiled_at, attempt}}"""
        with self._db() as con:
            rows = con.execute('SELECT * FROM table_stats WHERE workspace_id = ?', (workspace_id,)).fetchall()
        out = {}
        for r in rows:
            row = dict(r)
            row['profile'] = json.loads(row['profile']) if row.get('profile') else None
            row['attempt'] = json.loads(row['attempt']) if row.get('attempt') else None
            out[r['table_key']] = row
        return out

    def columns(self, workspace_id, table_key=None):
        """{table_key: {column_key: stats}}; one table only when table_key is given."""
        sql, args = 'SELECT table_key, column_key, stats FROM column_stats WHERE workspace_id = ?', [workspace_id]
        if table_key is not None:
            sql, args = sql + ' AND table_key = ?', args + [table_key]
        out = {}
        with self._db() as con:
            for r in con.execute(sql + ' ORDER BY table_key, position', args).fetchall():
                out.setdefault(r['table_key'], {})[r['column_key']] = json.loads(r['stats'])
        return out

    # ------------------------------------------------------------------ marks
    def marks(self, workspace_id):
        """{(table_key, column_key): True / False} - the user's own decisions."""
        with self._db() as con:
            rows = con.execute('SELECT table_key, column_key, personal FROM personal_marks WHERE workspace_id = ?',
                               (workspace_id,)).fetchall()
        return {(r['table_key'], r['column_key']): bool(r['personal']) for r in rows}

    def set_mark(self, workspace_id, table_key, column_key, personal):
        """personal: True / False, or None to go back to the automatic rules."""
        with self._db() as con:
            con.execute('DELETE FROM personal_marks WHERE workspace_id = ? AND table_key = ? AND column_key = ?',
                        (workspace_id, table_key, column_key))
            if personal is not None:
                con.execute('INSERT INTO personal_marks (workspace_id, table_key, column_key, personal, marked_at) '
                            'VALUES (?, ?, ?, ?, ?)', (workspace_id, table_key, column_key, int(bool(personal)), _now()))

    def mask_column(self, workspace_id, table_key, column_key, category, compact=True):
        """Mask what is stored for a column that has just become personal. Returns True if it changed.
        compact=False: the caller masks several columns and compacts the file once at the end."""
        with self._db() as con:
            row = con.execute('SELECT stats FROM column_stats WHERE workspace_id = ? AND table_key = ? AND column_key = ?',
                              (workspace_id, table_key, column_key)).fetchone()
            if row is None:
                return False
            stats = json.loads(row['stats'])
            if stats.get('masked'):
                return False
            mask_stats(stats, category)
            stats['masked'], stats['personal'] = True, category
            con.execute('UPDATE column_stats SET stats = ? WHERE workspace_id = ? AND table_key = ? AND column_key = ?',
                        (json.dumps(stats), workspace_id, table_key, column_key))
        if compact:
            self.compact()              # the unmasked values must not linger in the file's free pages
        return True

    # --------------------------------------------------------------- clearing
    def clear(self, workspace_id):
        """Delete every saved row count and statistic of a workspace. Returns how many tables had some."""
        with self._db() as con:
            n = con.execute('DELETE FROM table_stats WHERE workspace_id = ?', (workspace_id,)).rowcount
            con.execute('DELETE FROM column_stats WHERE workspace_id = ?', (workspace_id,))
        self.compact()
        return n

    def drop_table(self, workspace_id, table_key):
        with self._db() as con:
            con.execute('DELETE FROM table_stats WHERE workspace_id = ? AND table_key = ?', (workspace_id, table_key))
            con.execute('DELETE FROM column_stats WHERE workspace_id = ? AND table_key = ?', (workspace_id, table_key))

    def drop_source(self, workspace_id, source_id):
        """Forget what was read from a source that is being removed."""
        with self._db() as con:
            keys = [r[0] for r in con.execute('SELECT table_key FROM table_stats WHERE workspace_id = ? AND source_id = ?',
                                              (workspace_id, source_id)).fetchall()]
            for key in keys:
                con.execute('DELETE FROM column_stats WHERE workspace_id = ? AND table_key = ?', (workspace_id, key))
            con.execute('DELETE FROM table_stats WHERE workspace_id = ? AND source_id = ?', (workspace_id, source_id))

    def compact(self):
        """Give the freed pages back to the file system, so cleared values are not left in the file."""
        try:
            with self._db() as con:
                con.commit()
                con.execute('VACUUM')
        except Exception:               # another connection is busy: secure_delete has zeroed the pages anyway
            pass
