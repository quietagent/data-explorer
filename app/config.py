"""Paths and settings. Everything stays on this PC."""

import os
from pathlib import Path

APP_NAME = 'Data Explorer'
APP_VERSION = '0.3.0'

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / 'static'
DEMO_DIR = BASE_DIR / 'data' / 'demo'

# Workspaces, connection profiles and uploaded files live here (never passwords).
DATA_DIR = Path(os.environ.get('DATA_EXPLORER_DATA') or BASE_DIR / 'data')

# The server only ever listens on the loopback address. This is not configurable.
HOST = '127.0.0.1'
PORT = 8000


def _int(name, default):
    return int(os.environ.get(name) or default)


# Limits for queries the app sends to a live database.
STATEMENT_TIMEOUT_S = _int('DATA_EXPLORER_TIMEOUT', 30)
CONNECT_TIMEOUT_S = 10

# A PNG larger than this many pixels, or longer than this on one side (matplotlib's own limit is
# 65,536 px), is rendered at a lower resolution. Below PNG_MIN_DPI the text is unreadable, so the
# export is refused instead with a message pointing at .drawio and focus mode.
PNG_MAX_PIXELS = 60_000_000
PNG_MAX_SIDE = 65_000
PNG_MIN_DPI = 40

# The largest request the app accepts, which caps one upload (all its files together).
# Bigger exports are better read in place: give the file or folder path instead of uploading.
MAX_UPLOAD_BYTES = _int('DATA_EXPLORER_MAX_UPLOAD_MB', 500) * 1024 * 1024

# ---- Profiling ----
# What one table may cost a database: one row-count step (the catalog's estimate, or the bounded
# read below) and ONE read of a sample. Everything else is worked out from an in-memory copy.
#
# A database table with up to this many rows is read whole; a bigger one is read on a sample of
# about this many rows (Oracle SAMPLE, PostgreSQL / SQL Server TABLESAMPLE; others: the first
# rows). A table whose size is not known is read up to this many rows + 1.
SAMPLE_TARGET_ROWS = _int('DATA_EXPLORER_SAMPLE_ROWS', 100_000)
# Oracle: a table up to this size on disk is sampled by ROWS - SAMPLE (p): every block is read once,
# and the sample is even. A bigger one is sampled by BLOCKS - SAMPLE BLOCK (p): only p % of the
# table is read, but the sample is a few stretches of it and is labelled so wherever it is shown.
# The size comes from the catalog (blocks, or rows x average row length); unknown counts as small.
ORACLE_ROW_SAMPLE_MAX_BYTES = _int('DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB', 1024) * 1024 * 1024
ORACLE_BLOCK_BYTES = 8192           # the usual block size; a read-only account cannot ask for the real one
# SQLite has no sampling clause and no row estimates. Its tables are counted exactly (one COUNT(*) each, under the
# statement timeout), and a table with more rows than the sample size is read on a sample SPREAD over its row
# numbers: short stretches of this many rows at even distances - 1,000 stretches of 100 rows for 100,000 rows.
# That is as fast as reading the first rows, and it shows the whole of a table that was filled in time order.
SAMPLE_STRETCH_ROWS = 100
SAMPLE_MIN_STRETCHES = 20           # a small sample size still gives this many stretches (of fewer rows each)
SAMPLE_CAP_FACTOR = 2               # a sampled read stops at this many times the target, whatever the estimate said
SAMPLE_RETRY_BELOW = 0.25           # Oracle SAMPLE BLOCK: one retry with a higher percent when it gives less than this share
# A CSV / Excel table with more rows than this is profiled on a random sample of SAMPLE_TARGET_ROWS
# rows. (Files cost no database anything; the limit only keeps them in memory.)
SAMPLE_THRESHOLD_ROWS = _int('DATA_EXPLORER_SAMPLE_THRESHOLD', 1_000_000)
# The time one table may take, reading and statistics together. When it is used up the table is
# marked "timed out" and the job moves on. Nothing is retried against the database.
TABLE_BUDGET_S = _int('DATA_EXPLORER_TABLE_BUDGET', 120)
# The in-memory copies never use the disk: what does not fit in this much memory fails instead.
MEMORY_LIMIT_MB = _int('DATA_EXPLORER_MEMORY_MB', 2048)
READ_CHUNK_ROWS = 10_000            # rows moved from the database into the in-memory copy at a time
PROFILE_TOP_VALUES = 10             # most frequent values kept per column
PROFILE_HISTOGRAM_BINS = 12         # bars of a number histogram
PROFILE_SAMPLE_ROWS = 20            # sample rows shown for a table (read live, never saved)
PROFILE_STATS_CHUNK = 20            # columns per statistics statement (on the in-memory copy)
PROFILE_VALUE_MAX_CHARS = 120       # longer text values are cut in top values, min and max
PERSONAL_SCAN_ROWS = 1000           # rows of the in-memory copy checked for values that look personal
FILE_TYPE_ROWS = 20_000             # rows of a file used to detect its column types when a source is added
# Data-quality rules
HIGH_NULL_SHARE = float(os.environ.get('DATA_EXPLORER_HIGH_NULL') or 0.5)      # "high-null column" above this
MIXED_TYPE_SHARE = 0.8              # a text column in a file is "mostly numbers / dates" from this share

# ---- Relationship checks ----
RELATION_CHECK_BUDGET_S = _int('DATA_EXPLORER_RELATION_BUDGET', 60)     # per relationship; then "not checked (timeout)"
# Child and parent in the same database, key columns of the same kind: the database counts the rows, the empty
# keys and the orphan rows itself, over the whole child table - ONE anti-join statement under the statement
# timeout. 0 switches that off: every relationship is then measured on what the app reads and looks up.
RELATION_WHOLE_TABLE = (os.environ.get('DATA_EXPLORER_RELATION_WHOLE_TABLE') or '1').strip().lower() not in ('0', 'no', 'off', 'false')
RELATION_IN_CHUNK = 500             # keys per bound IN list (Oracle's limit is 1,000)
# Distinct child keys looked up per relationship, where the database does not count it itself (files, key
# columns of different kinds, a count that ran out of time). A result on some of the keys says "at least".
RELATION_MAX_KEYS = _int('DATA_EXPLORER_RELATION_KEYS', 5000)
RELATION_ORPHAN_EXAMPLES = 20       # example keys shown for orphans
# "Find links by values". Every pair of columns that could be a link gets a first look: RELATION_PROBE_VALUES
# different values of the child column - from one small read of RELATION_PROBE_ROWS rows of its table - are
# looked up in the parent key, the values of all the columns that might point at one key in one go. Only the
# pairs that share values go on to the full check, at most RELATION_MAX_PAIRS of them a run.
RELATION_PROBE_ROWS = 2000
RELATION_PROBE_VALUES = 30
RELATION_LOOK_PAIRS = _int('DATA_EXPLORER_RELATION_LOOK_PAIRS', 50_000)    # pairs that get a first look, the likeliest by name first
RELATION_MAX_PAIRS = _int('DATA_EXPLORER_RELATION_PAIRS', 200)          # column pairs fully measured per run for value links
RELATION_SUGGEST_VALUES = 1000      # distinct child values looked up per pair
RELATION_SUGGEST_MIN_OVERLAP = float(os.environ.get('DATA_EXPLORER_RELATION_MIN_OVERLAP') or 0.8)

# ---- SQL runner ----
SQL_MAX_ROWS = 1000                 # rows shown; one more is read to be able to say "more rows exist"
SQL_TIMEOUT_S = _int('DATA_EXPLORER_SQL_TIMEOUT', STATEMENT_TIMEOUT_S)
SQL_HISTORY = 50                    # query texts kept per workspace (text only, never results)
SQL_MAX_CHARS = 20_000              # the longest statement accepted
# Extra function names the SQL runner accepts, besides app/analysis/sql_functions.json:
#   DATA_EXPLORER_SQL_FUNCTIONS=my_func,oracle:other_func     (dialect:name limits it to one engine)
SQL_EXTRA_FUNCTIONS = os.environ.get('DATA_EXPLORER_SQL_FUNCTIONS') or ''

# ---- Scripts tab (the SQL Lineage engine) ----
# app/sql_lineage/ is a copy of the engine, never edited here. scripts/sync_lineage.py refreshes it from this
# folder: by default the sibling project, C:\Projects\SQL_Lineage\sql_lineage next to this one.
LINEAGE_SOURCE = Path(os.environ.get('DATA_EXPLORER_LINEAGE_SOURCE') or BASE_DIR.parent / 'SQL_Lineage' / 'sql_lineage')
LINEAGE_COPY = BASE_DIR / 'app' / 'sql_lineage'

# Features that later sessions switch on. The UI reads this to enable its tabs.
FEATURES = {'er_graph': True, 'profile': True, 'quality': True,
            'relations': True, 'sql': True, 'report': True, 'scripts': True}
