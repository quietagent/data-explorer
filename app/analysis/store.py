"""What session 3's features save in data/explorer.db (layout version 3).

    relation_checks   the last result of checking each relationship against the data: coverage,
                      orphan counts, cardinality, evidence. Example orphan keys are part of it;
                      those of personal columns are masked before they are saved.
    value_links       links suggested by value overlap, and what the user decided: 'suggested',
                      'accepted' (it then is a relationship of the kind "values") or 'rejected'
                      (hidden, and remembered so it is not suggested again). Numbers only -
                      never a value.
    sql_history       the last query texts of the SQL tab. Text only; never a result.

"Clear saved statistics" empties relation_checks and the undecided suggestions. Decisions and the
query history are settings of the workspace and stay.

Layout version 4 adds the Scripts tab's table (written by app/analysis/scripts.py):

    script_scans      the last scan of a scripts folder, one row per workspace: the folder, the dialect,
                      when, the summary counts, and the file that holds the graph
                      (lineage/<workspace id>.db under the data folder). Names and SQL text only.
"""

from __future__ import annotations

import json
from datetime import datetime

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS relation_checks (
    workspace_id  TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    rel_key       TEXT NOT NULL,          -- FROM_TABLE(COLUMNS)>TO_TABLE(COLUMNS), upper case
    result        TEXT NOT NULL,          -- JSON: status, coverage, orphans, null %, cardinality, evidence
    checked_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, rel_key)
);
CREATE TABLE IF NOT EXISTS value_links (
    workspace_id  TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    rel_key       TEXT NOT NULL,
    from_table    TEXT NOT NULL,          -- table ids as the merged model has them
    from_columns  TEXT NOT NULL,          -- JSON list
    to_table      TEXT NOT NULL,
    to_columns    TEXT NOT NULL,          -- JSON list
    status        TEXT NOT NULL,          -- suggested / accepted / rejected
    confidence    REAL,                   -- the share of the child's values found in the parent
    cardinality   TEXT,
    evidence      TEXT NOT NULL,          -- JSON: counts and texts, never a value
    found_at      TEXT NOT NULL,
    decided_at    TEXT,
    PRIMARY KEY (workspace_id, rel_key)
);
CREATE TABLE IF NOT EXISTS sql_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id  TEXT NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    source_id     TEXT,
    sql           TEXT NOT NULL,          -- the statement as typed. Never its result.
    outcome       TEXT,                   -- ok / blocked / failed
    ran_at        TEXT NOT NULL
);
"""


SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS script_scans (
    workspace_id  TEXT PRIMARY KEY REFERENCES workspaces (id) ON DELETE CASCADE,
    folder        TEXT NOT NULL,          -- the scanned folder (only read, never written)
    dialect       TEXT NOT NULL,          -- oracle / tsql / postgres / mysql / sqlite
    scanned_at    TEXT NOT NULL,
    summary       TEXT NOT NULL,          -- JSON: files, statements, unreadable, tables, joins ...
    lineage_file  TEXT NOT NULL           -- the graph, relative to the data folder: lineage/<workspace id>.db
);
"""


def migrate_v3(con):
    """Layout 2 -> 3: the tables above, and a note on table_stats for a profile that did not finish."""
    con.executescript(SCHEMA_V3)
    if 'attempt' not in {row[1] for row in con.execute('PRAGMA table_info(table_stats)')}:
        con.execute('ALTER TABLE table_stats ADD COLUMN attempt TEXT')      # JSON: status, message, at


def _now():
    return datetime.now().isoformat(timespec='seconds')


def rel_key(from_table, from_columns, to_table, to_columns):
    """One relationship's key in the tables above: independent of letter case."""
    return (f'{from_table.upper()}({",".join(c.upper() for c in from_columns)})>'
            f'{to_table.upper()}({",".join(c.upper() for c in to_columns)})')


class AnalysisStore:
    def __init__(self, db):
        self._db = db                   # Workspaces._db: a context manager giving a sqlite3 connection

    # ------------------------------------------------------- relationship checks
    def save_check(self, workspace_id, key, result):
        with self._db() as con:
            con.execute('INSERT INTO relation_checks (workspace_id, rel_key, result, checked_at) VALUES (?, ?, ?, ?) '
                        'ON CONFLICT (workspace_id, rel_key) DO UPDATE SET result = excluded.result, '
                        'checked_at = excluded.checked_at', (workspace_id, key, json.dumps(result), _now()))

    def checks(self, workspace_id):
        """{rel_key: result with 'checked_at'}"""
        with self._db() as con:
            rows = con.execute('SELECT rel_key, result, checked_at FROM relation_checks WHERE workspace_id = ?',
                               (workspace_id,)).fetchall()
        return {r['rel_key']: {**json.loads(r['result']), 'checked_at': r['checked_at']} for r in rows}

    def drop_checks(self, workspace_id, keep=None):
        """Forget results; with `keep`, only those of relationships that no longer exist."""
        with self._db() as con:
            if keep is None:
                return con.execute('DELETE FROM relation_checks WHERE workspace_id = ?', (workspace_id,)).rowcount
            gone = [r[0] for r in con.execute('SELECT rel_key FROM relation_checks WHERE workspace_id = ?',
                                              (workspace_id,)).fetchall() if r[0] not in keep]
            for key in gone:
                con.execute('DELETE FROM relation_checks WHERE workspace_id = ? AND rel_key = ?', (workspace_id, key))
            return len(gone)

    def mask_check(self, workspace_id, key, masker):
        """Rewrite one saved result (a column of it became personal). masker(result) -> result."""
        with self._db() as con:
            row = con.execute('SELECT result FROM relation_checks WHERE workspace_id = ? AND rel_key = ?',
                              (workspace_id, key)).fetchone()
            if row is None:
                return False
            con.execute('UPDATE relation_checks SET result = ? WHERE workspace_id = ? AND rel_key = ?',
                        (json.dumps(masker(json.loads(row['result']))), workspace_id, key))
        return True

    # ------------------------------------------------------------- value links
    def links(self, workspace_id, status=None):
        sql, args = 'SELECT * FROM value_links WHERE workspace_id = ?', [workspace_id]
        if status:
            sql, args = sql + ' AND status = ?', args + [status]
        with self._db() as con:
            rows = con.execute(sql + ' ORDER BY confidence DESC, rel_key', args).fetchall()
        return [{'key': r['rel_key'], 'from': r['from_table'], 'from_columns': json.loads(r['from_columns']),
                 'to': r['to_table'], 'to_columns': json.loads(r['to_columns']), 'status': r['status'],
                 'confidence': r['confidence'], 'cardinality': r['cardinality'], 'evidence': json.loads(r['evidence']),
                 'found_at': r['found_at'], 'decided_at': r['decided_at']} for r in rows]

    def suggest(self, workspace_id, key, from_table, from_columns, to_table, to_columns, confidence, cardinality,
                evidence):
        """Record a link found by value overlap. A link the user already decided on keeps its
        decision; its numbers are brought up to date."""
        with self._db() as con:
            con.execute(
                'INSERT INTO value_links (workspace_id, rel_key, from_table, from_columns, to_table, to_columns, status, '
                "confidence, cardinality, evidence, found_at) VALUES (?, ?, ?, ?, ?, ?, 'suggested', ?, ?, ?, ?) "
                'ON CONFLICT (workspace_id, rel_key) DO UPDATE SET confidence = excluded.confidence, '
                'cardinality = excluded.cardinality, evidence = excluded.evidence, found_at = excluded.found_at',
                (workspace_id, key, from_table, json.dumps(list(from_columns)), to_table, json.dumps(list(to_columns)),
                 confidence, cardinality, json.dumps(evidence), _now()))

    def decide(self, workspace_id, key, status):
        """accepted / rejected / suggested (undo). Returns True if the link exists."""
        with self._db() as con:
            return bool(con.execute('UPDATE value_links SET status = ?, decided_at = ? WHERE workspace_id = ? AND rel_key = ?',
                                    (status, None if status == 'suggested' else _now(), workspace_id, key)).rowcount)

    def drop_suggestions(self, workspace_id, keys=None):
        """Forget suggestions nobody decided on (all of them, or the given ones)."""
        with self._db() as con:
            if keys is None:
                return con.execute("DELETE FROM value_links WHERE workspace_id = ? AND status = 'suggested'",
                                   (workspace_id,)).rowcount
            for key in keys:
                con.execute("DELETE FROM value_links WHERE workspace_id = ? AND rel_key = ? AND status = 'suggested'",
                            (workspace_id, key))
            return len(keys)

    def drop_links(self, workspace_id, keys):
        """Forget links whose tables or columns no longer exist, whatever was decided about them."""
        with self._db() as con:
            for key in keys:
                con.execute('DELETE FROM value_links WHERE workspace_id = ? AND rel_key = ?', (workspace_id, key))

    # --------------------------------------------------------------- SQL history
    def add_query(self, workspace_id, source_id, sql, outcome, keep):
        """Remember a query text (never its result) and keep only the last `keep` of the workspace."""
        with self._db() as con:
            con.execute('DELETE FROM sql_history WHERE workspace_id = ? AND sql = ? AND COALESCE(source_id, \'\') = ?',
                        (workspace_id, sql, source_id or ''))
            con.execute('INSERT INTO sql_history (workspace_id, source_id, sql, outcome, ran_at) VALUES (?, ?, ?, ?, ?)',
                        (workspace_id, source_id, sql, outcome, _now()))
            con.execute('DELETE FROM sql_history WHERE workspace_id = ? AND id NOT IN ('
                        'SELECT id FROM sql_history WHERE workspace_id = ? ORDER BY id DESC LIMIT ?)',
                        (workspace_id, workspace_id, int(keep)))

    def queries(self, workspace_id):
        with self._db() as con:
            rows = con.execute('SELECT id, source_id, sql, outcome, ran_at FROM sql_history WHERE workspace_id = ? '
                               'ORDER BY id DESC', (workspace_id,)).fetchall()
        return [dict(r) for r in rows]

    def clear_queries(self, workspace_id):
        with self._db() as con:
            return con.execute('DELETE FROM sql_history WHERE workspace_id = ?', (workspace_id,)).rowcount
