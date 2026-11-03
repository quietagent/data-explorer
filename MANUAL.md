For how to use the app, see README.md. This file is for changing it.

## 1. How it runs

1. `run.bat` creates `.venv` if it is missing, installs `requirements.txt` and the drivers in `requirements-drivers.txt` when those files changed, then runs `.venv\Scripts\python.exe -m app` with your arguments (`--demo`, `--port`, `--no-browser`).
2. `main()` in `app/__main__.py` takes the first free port from 8000, builds the app with `create_app()` and starts uvicorn on 127.0.0.1 only. Then it opens the browser.
3. `create_app()` in `app/main.py` creates the services (`Workspaces`, `Jobs`, `Profiling`, `Relations`, `SqlRunner`, `Report`, `Scripts`) and holds every API route (`/api/...`), grouped by feature.
4. The browser gets `static/index.html` at `/`. The page loads its scripts from `/static` and talks only to `/api`. There is no build step: edit a file and reload the page.
5. Long work (profiling, relationship checks, scans of scripts) runs in a thread (`app/jobs.py`), one job per workspace; the page polls `/api/jobs/{id}`.

## 2. Where things are saved

| Where | Written by | What |
| --- | --- | --- |
| `data/explorer.db` | `app/workspace.py`, `app/profiling/store.py`, `app/analysis/store.py` | One SQLite file with everything the app keeps. Tables below. |
| `data/uploads/<workspace id>/<source id>/` | `_save_uploads()` in `app/main.py` | Copies of uploaded files. Deleted with their source or workspace. A source given as a path is read in place, with no copy. |
| `data/lineage/<workspace id>.db` | `Scripts._save()` in `app/analysis/scripts.py` | The Scripts tab's last scan of a workspace: the graph of the scripts (names and SQL text, never data), in the engine's layout (`app/sql_lineage/graph/store.py`). A new scan replaces it; deleted with the workspace and by "Forget scan". The scanned folder is only read. |
| `data/demo/` | `build_demo()` in `app/demo.py` | The demo database, `.sql` file and CSV files. Written only when missing, or by `python -m app.demo --force`. In git. |
| The browser's Downloads folder | the browser | Diagram exports (.drawio, .png), reports (.html, .xlsx), SQL results (.csv), the Scripts tab's report (.html). The app builds them in memory (`app/export.py`, `app/analysis/report.py`, `SqlRunner.csv()`, `build_report()` of the engine) and writes no file. |
| The browser's local storage | `static/theme.js`, `static/app.js` | `data-explorer.theme` (light/dark) and `data-explorer.workspace` (the last open workspace). |
| `data/checks/` | `scripts/demo_report.py` (its default folder) and hand checks | Not used by the app. Git-ignored. |
| `.venv/` | `run.bat` | Python packages, plus the marker files `requirements.installed` and `requirements-drivers.installed`. |

**Logs:** there are none. The console window shows the start message and uvicorn warnings only (`log_level='warning'`, `access_log=False` in `app/__main__.py`). **Never saved:** passwords (memory only, `Workspaces._passwords`), running jobs, the "show personal values" switch, sample rows, query results. Rows read from a source are held in an in-memory DuckDB with no temp folder (`open_memory()` in `app/sources/duck.py`).

`DATA_EXPLORER_DATA` moves `data/explorer.db`, `data/uploads/` and `data/lineage/` to another folder (`DATA_DIR` in `app/config.py`). The demo folder does not move.

### The tables in explorer.db

The layout version is `PRAGMA user_version` (4 now). `SCHEMA_VERSION` and `MIGRATIONS` in `app/workspace.py` upgrade an older file when the app opens it. Table names and column names (`table_key`, `column_key`) are stored in upper case; `table_key` is `SCHEMA.TABLE`, or just `TABLE` when there is no schema.

| Table | Created in | One row per | Holds |
| --- | --- | --- | --- |
| `workspaces` | `SCHEMA_V1`, `app/workspace.py` | workspace | id, name, created and updated time |
| `sources` | `SCHEMA_V1` | source of a workspace | kind (`sql_files`, `database`, `files`), label, `settings` JSON (never a password), `snapshot` JSON (the structure that was read: tables, columns, types, keys; a database source's `default_schema`), warnings |
| `profiles` | `SCHEMA_V1` | saved connection | `settings` JSON: engine, host, port, database, user, schema filter. No password |
| `table_stats` | `SCHEMA_V2`, `app/profiling/store.py` | counted or profiled table | `row_count`, `row_count_kind` (`estimate`, `exact`, `more_than`), `counted_at`, `profile` JSON (rows profiled, sampled or not, sample method, notes, seconds), `profiled_at`, `attempt` JSON (last run timed out or failed) |
| `column_stats` | `SCHEMA_V2` | column of a profiled table | **The profiling results.** `stats` JSON: `type`, `family`, `rows`, `nulls`, `distinct`, `min`, `max`, `avg`, `min_length`, `max_length`, `top` (up to 10 `[value, count]`), `histogram`, `personal`, `masked`. Values of personal columns are stored masked |
| `personal_marks` | `SCHEMA_V2` | column a user marked | `personal`: 1 = personal, 0 = not personal |
| `relation_checks` | `SCHEMA_V3`, `app/analysis/store.py` | checked relationship | `rel_key` (`FROM(COLS)>TO(COLS)`), `result` JSON: coverage, orphans, empty keys, cardinality, evidence |
| `value_links` | `SCHEMA_V3` | link found by values | tables and columns, `status` (`suggested`, `accepted`, `rejected`), `confidence`, `evidence` JSON |
| `sql_history` | `SCHEMA_V3` | query typed in the SQL tab | the text and its outcome (never the result); the last 50 per workspace |
| `script_scans` | `SCHEMA_V4`, `app/analysis/store.py` | workspace with a scan of scripts | `folder`, `dialect`, `scanned_at`, `summary` JSON (the counts), `lineage_file` (the graph, relative to the data folder) |

### Looking inside explorer.db

Use **DB Browser for SQLite** (free, sqlitebrowser.org): File > Open Database Read Only, then the "Execute SQL" tab. Or the `sqlite3` command:

```bat
sqlite3 -readonly data\explorer.db
```

Always open it read-only, and do not change it while the app runs. These three queries were run read-only on the real file. Change the workspace name and table to yours.

```sql
-- 1. Every counted or profiled table, per workspace
SELECT w.name AS workspace, t.table_key, t.row_count, t.row_count_kind,
       json_extract(t.profile, '$.rows_profiled') AS rows_profiled,
       json_extract(t.profile, '$.sampled') AS sampled, t.profiled_at
FROM table_stats t JOIN workspaces w ON w.id = t.workspace_id
ORDER BY w.name, t.table_key;

-- 2. The column statistics of one table
SELECT c.column_key,
       json_extract(c.stats, '$.type')     AS type,
       json_extract(c.stats, '$.rows')     AS rows_profiled,
       json_extract(c.stats, '$.nulls')    AS empty,
       json_extract(c.stats, '$.distinct') AS distinct_values,
       json_extract(c.stats, '$.min')      AS min_value,
       json_extract(c.stats, '$.max')      AS max_value,
       json_extract(c.stats, '$.personal') AS personal
FROM column_stats c JOIN workspaces w ON w.id = c.workspace_id
WHERE w.name = 'Zephyr test' AND c.table_key = 'CUSTOMER'
ORDER BY c.position;

-- 3. The most frequent values of one column
SELECT json_extract(v.value, '$[0]') AS value, json_extract(v.value, '$[1]') AS row_count
FROM column_stats c JOIN workspaces w ON w.id = c.workspace_id,
     json_each(c.stats, '$.top') AS v
WHERE w.name = 'Zephyr test' AND c.table_key = 'CUSTOMER' AND c.column_key = 'CITY';
```

## 3. File map

**Project folder:** `run.bat` (start), `requirements.txt` (packages), `requirements-drivers.txt` (database drivers), `pytest.ini` (test settings), `README.md` (user guide), `HANDOFF.md` and `PLAN.md` (notes from the build sessions), `PROMPT-*.md` (briefs for those sessions), `.claude/launch.json` (starts the demo from Claude Code).

| `app/` | What it does |
| --- | --- |
| `__init__.py` | Empty. Makes `app` a package |
| `__main__.py` | Start-up: arguments, free port, uvicorn, opens the browser |
| `main.py` | `create_app()`: every `/api` route and error handler; serves `static/` |
| `config.py` | Paths, limits and every setting |
| `security.py` | Local-only and upload-size middleware; `check_read_only_sql()` for every statement the app sends; error texts without values |
| `sqlguard.py` | The strict check of SQL typed in the SQL tab: one read-only statement, allowed functions only |
| `jobs.py` | Background jobs: one per workspace, progress, cancel |
| `model.py` | The common schema model (`SchemaModel`, `Table`, `Column`, `Relationship`), merging, links from names |
| `workspace.py` | Workspaces, sources, connection profiles; creates and upgrades `data/explorer.db` |
| `export.py` | Diagram export to .drawio and .png |
| `demo.py` | Builds `data/demo/` |

| `app/sources/` | What it does |
| --- | --- |
| `__init__.py` | `SourceError` |
| `sqlfiles.py` | .sql files to a model, with the parser in `app/er/` |
| `database.py` | Live database to a model. `ENGINES`; `ReadOnlyDatabase`: read-only session, timeout, the guard on every statement |
| `files.py` | CSV and Excel files to a model; column type detection |
| `duck.py` | The in-memory DuckDB, locked down (no disk, no extensions) |

| `app/profiling/` | What it does |
| --- | --- |
| `__init__.py` | What is where in this folder |
| `queries.py` | Every profiling statement (SQLAlchemy Core); the sampling clause per engine; row estimates |
| `data.py` | `DatabaseData`, `SampleData`, `FileData`: where rows are read from and held |
| `profiler.py` | `read_table()`: the one read of a table; `profile_copy()`: the statistics |
| `service.py` | `Profiling`: jobs, row counts, table profiles, personal marks, quality |
| `store.py` | `ProfileStore`: `table_stats`, `column_stats`, `personal_marks` |
| `quality.py` | The data-quality rules |
| `personal.py` | Which columns are personal, and how values are masked |
| `personal_rules.json` | The personal-data rules themselves (data, not code) |

| `app/analysis/` | What it does |
| --- | --- |
| `__init__.py` | What is where in this folder |
| `relations.py` | `Relations`: checks relationships on the data; finds links by values; accept and reject |
| `sqlrunner.py` | `SqlRunner`: the SQL tab (check, run, mask, CSV, history) |
| `sql_functions.json` | The functions the SQL tab allows, per engine |
| `lineage.py` | `origins()`: which table column each result column comes from, so the SQL tab can mask it. Nothing to do with the Scripts tab or SQL Lineage (tests: `tests/test_lineage.py`) |
| `scripts.py` | `Scripts`: the Scripts tab. Scans a folder of scripts with the SQL Lineage engine (a job), against the workspace model as schema (`schema_tables()`: declared keys only), with the one database source's default schema (`default_schema()`); the note when names are missing and a source was read without views (`views_note()`); impact, report, forget (tests: `tests/test_scripts_*.py`) |
| `report.py` | `Report`: the HTML and Excel report, built in memory |
| `store.py` | `AnalysisStore`: `relation_checks`, `value_links`, `sql_history`; and the layout of `script_scans` |

**`app/sql_lineage/`** is the SQL Lineage engine: a byte-for-byte copy of `C:\Projects\SQL_Lineage\sql_lineage`. Never edit it here (see "Change the SQL Lineage engine" in section 5). The app calls only its public functions: `scan_folder`, `save_graph`, `load_graph` (`graph`), `schema_from_tables` (`parse`), `analyze`, `impact` (`analysis`), `build_report` (`output`), and `settings`. Its own README and tests are in `C:\Projects\SQL_Lineage`.

| `app/er/` | Copies of the code in `tools/`; each docstring lists what the app changed |
| --- | --- |
| `__init__.py` | Package note |
| `tokenizer.py` | SQL tokenizer and keyword sets |
| `parser.py` | Reads DDL, and the links implied by joins in queries |
| `model.py` | The parse-time model (tables, columns, links) |
| `inference.py` | Which column is a key, and which way a link points |
| `layout.py` | Box layout shared by both renderers |
| `render_drawio.py` | Writes .drawio |
| `render_png.py` | Draws .png with matplotlib |

| `static/` | What it does |
| --- | --- |
| `index.html` | The one page: header, side panels, the seven tab views, dialogs |
| `theme.js` | Light/dark choice; loaded first |
| `app.js` | Core: helpers, workspaces, sources and their dialog, table list, ER graph, details panel, export, tabs |
| `profile.js` | Profile and Quality tabs |
| `analysis.js` | Relations, SQL and Report tabs |
| `scripts.js` | Scripts tab |
| `styles.css` | All styles. Every colour is a variable with a light and a dark value |
| `favicon.svg` | Browser tab icon |
| `vendor/` | Bundled libraries (Cytoscape and its layouts, Chart.js). Don't edit: tests check their hashes (see `vendor/VERSIONS.md`) |

| `scripts/` | What it does |
| --- | --- |
| `live_db.ps1` | Starts, seeds, stops the four Docker test databases |
| `live_engines.py` | What those databases hold, their ports and throw-away passwords |
| `statement_counts.py` | How many statements profiling sends per table (needs Docker) |
| `call_proof.py` | Which "calls without brackets" really run on Oracle and PostgreSQL (needs Docker) |
| `sample_spread.py` | How even a sample is, per engine (needs Docker) |
| `reserved_words.py` | Asks each engine which words are reserved; compares with `app/sqlguard.py` |
| `contrast.py` | Colour contrast of `static/styles.css`, light and dark |
| `demo_report.py` | Saves the demo report to `data/checks/` |
| `sync_lineage.py` | Copies the SQL Lineage engine into `app/sql_lineage/` and prints what changed |

**`tools/`**: `sql_to_drawio.py` (SQL to .drawio) and `sql_to_erd.py` (SQL to .png), the original command-line scripts. Keep them unchanged.

**`data/`**: `demo/` (the demo data, in git: `demo/telecom.db`, `demo/telecom.sql`, `demo/csv/`); `explorer.db` (all saved state, section 2); `uploads/` (copies of uploaded files); `lineage/` (the Scripts tab's scans); `checks/` (hand checks and scratch tools of the build sessions). All but `demo/` are git-ignored.

**`tests/`**: pytest, one `test_<area>.py` per area. `conftest.py` gives each test app its own empty data folder; `expected.py` holds the facts of the demo model; `hostile.py` holds the statements the SQL tab must refuse. `fixtures/zephyr/` is a small made-up database with a hand-checked answer key; `fixtures/rimal/` is the schema of the Rimal Telecom demo database; `fixtures/zephyr_scripts/` are SQL Lineage's own test scripts, unchanged. The Scripts tab: `test_scripts_api.py` (the same results as the engine run directly), `test_scripts_sync.py` (the copy equals the source), `test_scripts_frontend.py`, `test_scripts_schemas.py` (names written without their schema, the default schema, the views note; one test reads `fixtures/scripts_without_owner/` against the Docker Oracle).

## 4. Settings

All in `app/config.py`, read once when the app starts.

| Setting | Default | What it changes |
| --- | --- | --- |
| `DATA_EXPLORER_DATA` | `data` | Moves `data/explorer.db` and `data/uploads/` to another folder |
| `DATA_EXPLORER_TIMEOUT` | 30 | Seconds any statement the app sends to a database may run |
| `DATA_EXPLORER_MAX_UPLOAD_MB` | 500 | Largest upload (all files of one upload together) |
| `DATA_EXPLORER_SAMPLE_ROWS` | 100000 | A database table with more rows is profiled on a sample of about this many |
| `DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB` | 1024 | Oracle: tables up to this size are sampled by rows, bigger ones by blocks |
| `DATA_EXPLORER_SAMPLE_THRESHOLD` | 1000000 | A CSV or Excel table with more rows is profiled on a random sample |
| `DATA_EXPLORER_TABLE_BUDGET` | 120 | Seconds one table may take to profile, then "timed out" |
| `DATA_EXPLORER_MEMORY_MB` | 2048 | Memory for the in-memory DuckDB copies |
| `DATA_EXPLORER_HIGH_NULL` | 0.5 | Share of empty rows above which a column is "mostly empty" |
| `DATA_EXPLORER_RELATION_BUDGET` | 60 | Seconds one relationship check may take |
| `DATA_EXPLORER_RELATION_WHOLE_TABLE` | 1 | `0` stops the database from counting orphans over the whole child table |
| `DATA_EXPLORER_RELATION_KEYS` | 5000 | Child keys looked up per relationship when the database does not count it |
| `DATA_EXPLORER_RELATION_LOOK_PAIRS` | 50000 | Column pairs that get a first look in "Find links by values" |
| `DATA_EXPLORER_RELATION_PAIRS` | 200 | Column pairs measured in full in "Find links by values" |
| `DATA_EXPLORER_RELATION_MIN_OVERLAP` | 0.8 | Share of values found for a link to be suggested |
| `DATA_EXPLORER_SQL_TIMEOUT` | the `DATA_EXPLORER_TIMEOUT` value | Seconds a query of the SQL tab may run |
| `DATA_EXPLORER_SQL_FUNCTIONS` | (none) | Extra functions the SQL tab allows: `my_func,oracle:other_func` |
| `DATA_EXPLORER_LINEAGE_SOURCE` | `..\SQL_Lineage\sql_lineage` | The folder `scripts/sync_lineage.py` copies the engine from |

The Scripts tab's limits are the engine's: `MAX_FILES` (5,000 scripts) and `MAX_FILE_MB` (5) in `app/sql_lineage/settings.py`. Change them in SQL Lineage, then copy (section 5).

To set one, add a line such as `set DATA_EXPLORER_TIMEOUT=60` to `run.bat` before the line that starts Python, or make it a Windows environment variable (System Properties > Environment Variables, or `setx DATA_EXPLORER_TIMEOUT 60`) and open a new window. Restart the app after a change. A value that is not a number stops the app at start.

## 5. How to change common things

**Add or change a personal-data (masking) rule.** Edit `app/profiling/personal_rules.json`; no code change is needed. Words that make a column personal are under `names` > `words` > the category; two-word rules under `names` > `word_groups`; value patterns under `values` > `rules`; how values are hidden under `masking`. A new category also needs its text under `categories`. README, section "Personal data", explains each key. Restart the app: a bad file stops it with a message (`RulesError`, raised in `Rules._build()` in `app/profiling/personal.py`). Statistics saved under the old rules are masked in the file the next time the Profile tab loads (`Profiling.mask_saved()` in `app/profiling/service.py`). The SQL tab uses the same rules. If the file cannot express a rule, the code is `personal_by_name()`, `personal_by_values()` and `mask()` in `app/profiling/personal.py`; `rules_path()` there says which rules file is read. Tests: `tests/test_personal.py`, `tests/test_pay.py`, `tests/test_rimal.py`, `tests/test_zephyr.py`, `tests/test_session3_fixes.py`.

**Allow a new function in the SQL tab.** Add its name in lower case to `app/analysis/sql_functions.json`, under `all` (every engine) or under one engine: `oracle`, `mssql`, `postgresql`, `mysql`, `sqlite`, or `duckdb` (CSV and Excel sources). Type names that take brackets go under `types`. `load_functions()` in `app/sqlguard.py` reads the file at start; restart the app. Only add built-in functions that read their arguments and return a value. For one PC only, use `DATA_EXPLORER_SQL_FUNCTIONS` instead. Tests: `tests/test_sqlguard.py`, `tests/test_sqlrunner.py`; with Docker also `tests/test_live_sql.py`.

**Add a data-quality check.** In `app/profiling/quality.py`, add `'my_rule': 'Title shown to the user'` to `RULES`, and in `table_findings()` append `_finding(severity, table, column, 'my_rule', message, sampled)`; severity is `high`, `medium` or `low`. A rule only reads the saved statistics (`columns[COLUMN_NAME]` is the `stats` JSON above) and the model; it sends no query. Never quote a value of a personal column: test `personal(c.name, st)` as the `constant` rule does. A new statistic must be added in `profile_copy()` in `app/profiling/profiler.py` (its SQL is `stats_query()` in `app/profiling/queries.py`), and tables must be profiled again. The Quality tab and the report take the titles from `RULES`, so the front end needs no change. Tests: `tests/test_quality.py` (it lists every rule name: add yours), `tests/test_profiling.py`, `tests/test_report.py`, `tests/test_zephyr.py`.

**Change how links between tables are guessed.**

| Kind of link | Where | What to edit |
| --- | --- | --- |
| From column names | `infer_by_names()`, `_name_target()`, `_key_owners()` in `app/model.py` | Which name points at which key; `NAME_CONFIDENCE`; `GENERIC_KEYS` (names never linked by name). `_personal()` keeps personal columns out |
| From joins in .sql files | `app/er/parser.py`, `app/er/inference.py` | What a join means; its confidence is `JOIN_CONFIDENCE` in `app/model.py` |
| By values ("Find links by values") | `app/analysis/relations.py`: `candidates()`, `affinity()`, `pointer_words()`, `first_look()`, `try_pair()`, `serial()` | Which pairs are tried, the name hints, when a pair is suggested; `FIRST_LOOK_VALUES`, `FIRST_LOOK_SHARE`, `SERIAL_STARTS_BY`, `SERIAL_PROBES`, `SERIAL_DENSITY` |
| The first look's read | `look_values()` in `app/analysis/relations.py`; `block_sample` of `read_table()` in `app/profiling/profiler.py` | On Oracle it samples whole blocks (`SAMPLE BLOCK`: only those blocks are read, where `SAMPLE (p)` reads them all); a table whose blocks give a column fewer than `FIRST_LOOK_VALUES` values is read again the usual way, and the job's notes name it. The full check of a pair samples as profiling does |

Tests: `tests/test_model.py`, `tests/test_relations.py`, `tests/test_parser_demo.py`, `tests/test_zephyr.py`, `tests/test_dry_run.py`. A change in `app/er/` also fails `tests/test_er_scripts.py`, which holds it equal to the scripts in `tools/`: update that test.

**Change a sample size or a timeout.** If it has a setting (section 4), set that. Otherwise change the constant in `app/config.py`, for example `PROFILE_SAMPLE_ROWS` (20 sample rows shown), `SQL_MAX_ROWS` (1,000 rows shown), `CONNECT_TIMEOUT_S` (10 s), `RELATION_PROBE_ROWS`, `PERSONAL_SCAN_ROWS`. Every module reads these from `config`, so one edit is enough. Some tests check the defaults; update them with a new default: `tests/test_session3_fixes.py`, `tests/test_profiling.py`, `tests/test_relations.py`, `tests/test_sqlrunner.py`, `tests/test_quality.py`, `tests/test_session2_fixes.py`. Keep the "Settings" table in README in step.

**Add a database engine or driver.** Another driver for a known engine: change its entry in `ENGINES` in `app/sources/database.py` (`driver`, `module`, `pip`) and its line in `requirements-drivers.txt`. A new engine touches every place with a branch per engine:
- `app/sources/database.py`: `ENGINES`, `build_url()`, `connect_args()`, `SESSION_SETUP` or `TRANSACTION_SETUP` (the read-only session is required: a connection that refuses it is never used), `_on_connect()` (the timeout).
- `app/security.py`: `SETUP_STATEMENTS` must match those session statements exactly.
- `app/profiling/queries.py`: `PERCENT_SAMPLING` and a `@compiles(SampledTable, ...)` function (sampling), `estimate_query()` (row estimates), `LOB` (columns not read), `_length()`, `_as_double()`.
- `app/sqlguard.py`: `DIALECTS` and `RESERVED` (`scripts/reserved_words.py` asks an engine for its words); a new list in `app/analysis/sql_functions.json`.
- `app/analysis/relations.py`: `CASE_BLIND`, `key_kind()`; `app/analysis/lineage.py`: `EXACT_CASE`.
- The connection dialog reads the engine list from `/api/meta` (`engines_info()`), so the page needs no change.

Tests: `tests/test_sources.py`, `tests/test_readonly.py`, `tests/test_security.py`, `tests/test_sqlguard.py`. A test copy of the engine goes in `scripts/live_engines.py` and `scripts/live_db.ps1`, for `tests/test_live_engines.py` and `tests/test_live_sql.py`.

**Change the SQL Lineage engine (Scripts tab).** Never edit `app/sql_lineage/`. 1. Edit `C:\Projects\SQL_Lineage\sql_lineage`. 2. Run its tests there: `.venv\Scripts\python.exe -m pytest`, and `.venv\Scripts\python.exe scripts\score_key.py` (0 differences). 3. Here, run `.venv\Scripts\python.exe -m scripts.sync_lineage`: it copies every changed file and says which. 4. Run the app's tests; `tests/test_scripts_sync.py` fails while the copy differs. A new sqlglot version in SQL Lineage's `requirements.txt` needs the same pin in this `requirements.txt` (the sync warns). What the tab does with the engine's results is `app/analysis/scripts.py` and `static/scripts.js`.

**Table names written without their schema (Scripts tab).** The engine decides them (`Schema.resolve()` in `app/sql_lineage/parse/schema.py`; its README, "Table names written without their schema"). The app gives it the default schema of the workspace's one database source: `_read()` in `app/sources/database.py` sets `SchemaModel.default_schema` (`app/model.py`), which is saved in the source's `snapshot` and shown as `default_schema` by `_public()` in `app/workspace.py`. A source saved before 21 Oct 2026 has none until it is read again; the scan then finds a name by the one-schema rule alone. The views note is `views_note()` in `app/analysis/scripts.py`, shown by `renderScripts()` in `static/scripts.js`.

**Change text or a button in a tab.** Fixed text and buttons are in `static/index.html`, one `<div class="view" id="view-...">` per tab. Text made at run time is in the tab's script; search for the words. Tab names are `TABS` in `static/app.js`. Some texts come from Python: rule titles (`RULES` in `app/profiling/quality.py`), link kinds (`KIND_INFO` in `app/model.py`), engine names (`ENGINES`).

| Tab | View id | Script |
| --- | --- | --- |
| ER graph | `view-er_graph` | `static/app.js` |
| Profile, Quality | `view-profile`, `view-quality` | `static/profile.js` |
| Relations, SQL, Report | `view-relations`, `view-sql`, `view-report` | `static/analysis.js` |
| Scripts | `view-scripts` | `static/scripts.js` |

Rules of the page: build elements with `h(...)`, never `innerHTML` (a test refuses it); no inline `<script>` or `onclick` (the Content-Security-Policy in `app/security.py` blocks them); colours only as variables in `static/styles.css`, with a light and a dark value. Reload the page to see a change. Tests: `tests/test_frontend.py` (it looks for some ids and texts by name: update it when you rename one); after a colour change, `python -m scripts.contrast`.

## 6. Run the tests

Quick suite, without the Docker databases (the live tests skip themselves), about 5 to 6 minutes. Unset the settings of section 4 first: some tests check the defaults. One file or one test: `.venv\Scripts\python.exe -m pytest tests\test_quality.py -k high_null`.

```bat
.venv\Scripts\python.exe -m pytest
```

Full suite, with the four Docker databases (needs Docker Desktop; the first `up` takes a few minutes), about 9 minutes:

```bat
powershell -ExecutionPolicy Bypass -File scripts\live_db.ps1 up
.venv\Scripts\python.exe -m pytest
powershell -ExecutionPolicy Bypass -File scripts\live_db.ps1 stop
```

`scripts\live_db.ps1 test` runs only the live tests. A few tests also skip themselves when Node.js or the Rimal Telecom demo databases are not on the PC (README, section "Tests").
