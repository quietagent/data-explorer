# Data Explorer

A local web app for exploring a SQL database: which tables exist, what is in them, and how they
relate. It runs on your own PC and reads databases **read-only**.

To maintain or change the code, see [MANUAL.md](MANUAL.md): where things are, where data is saved, and how to change common things.

- **Sources:** SQL files (DDL and queries), a live database, or CSV / Excel exports. A workspace can
  combine several, for example SQL files for the structure plus CSV exports.
- **Engines:** Oracle, SQL Server, PostgreSQL, MySQL / MariaDB, SQLite.
- **What it does:** the interactive **ER graph** with table details and export to draw.io and PNG;
  **profiling** (row counts, column statistics, top values, histograms, sample rows) and
  **data-quality findings**; **relationship checks** on the data (coverage, orphan rows, real
  cardinality, links suggested by values); a **read-only SQL tab**; a **report** as one HTML file
  or an Excel workbook; and a **Scripts** tab that finds the joins across the team's `.sql` and `.py`
  scripts. Personal data is masked everywhere.

> **Company data never leaves the PC.** The server listens on `127.0.0.1` only and refuses any request
> that does not come from this PC. There are no cloud calls, no telemetry and no CDN: the JavaScript
> libraries are bundled in `static/vendor/`.

## Install and run

You need Windows and **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/), tick
"Add python.exe to PATH").

```bat
run.bat --demo
```

`run.bat` creates a virtual environment in `.venv` the first time, installs the packages (this needs
internet access once), starts the server and opens your browser. It installs in two steps:

1. `requirements.txt` - what the app cannot start without. If this fails, `run.bat` stops and says so.
2. `requirements-drivers.txt` - the four database drivers, **one at a time**. A driver that cannot be
   installed only gives a warning: the app still starts, and that engine is shown as
   "driver not installed". To try again later: `.venv\Scripts\python -m pip install <driver>`.

| Command | What it does |
| --- | --- |
| `run.bat` | Start with your saved workspaces |
| `run.bat --demo` | Start with the demo workspace (sample telecom data, nothing from your company) |
| `run.bat --port 8010` | Use another port (default 8000; if it is busy the next free one is used) |
| `run.bat --no-browser` | Start without opening the browser |

To stop the app, close its window or press Ctrl+C in it.

Without `run.bat` (any OS): `python -m venv .venv`, install `requirements.txt` and
`requirements-drivers.txt` into it, then `.venv\Scripts\python -m app --demo`.

**No internet on the work PC?** On a PC that has internet and the same Python version, run
`pip download -r requirements.txt -r requirements-drivers.txt -d wheels`, copy the `wheels` folder
across, and install with
`.venv\Scripts\python -m pip install --no-index --find-links wheels -r requirements.txt -r requirements-drivers.txt`.

## Using it

1. **Add a source** (left panel):
   - **SQL files** - upload `.sql` files or give a file / folder path. `CREATE TABLE` / `ALTER TABLE`
     give tables, columns, types and declared keys; queries add the relationships implied by their JOINs.
   - **Live database** - fill in the connection form and press **Test connection** first. It reports the
     server version, how many tables it sees, the schemas you can pick, and which protections are active.
   - **CSV / Excel files** - upload files or give a folder path. Each file, and each sheet of a workbook,
     becomes a table. A workbook with one sheet is named after the file; with several sheets each table
     is `<file>.<sheet>`, so `Sheet1` of two workbooks stay apart. Types are detected from the data;
     relationships are inferred from column names (for example `orders.customer_id` -> `customers.id`,
     also when `customers` has a declared key with another name).
     One upload may be at most 500 MB (`DATA_EXPLORER_MAX_UPLOAD_MB`); for bigger exports give the
     folder path instead - the files are then read in place, without a copy.
2. **Read the graph** (centre). Tables are boxes and relationships are lines:
   **solid = declared foreign key, dashed = inferred, dotted = a link you accepted from value overlap**.
   The label is the column pair, with `1` and `N` at the ends when the direction is known (`1` and `1`
   for a one-to-one relationship).
   - Click a table to highlight it and its neighbours. Click a line to jump to the other table.
   - **Focus** shows only the tables within N hops of the selected table - use it on big schemas.
     Double-clicking a table focuses on it.
   - The checkboxes hide or show each kind of relationship (declared / from joins / from values / from
     names), and the confidence filter hides weak guesses.
   - Zoom with the mouse wheel or the buttons, drag tables, press **Relayout** to tidy up.
   - In the hierarchy layout **a line is at least as long as its label**: the tables stand as far apart
     as the label between them needs, so a long column pair is not written over the boxes (in views of
     up to 45 lines - above that only the lines of the selected table show a label). **No line runs
     through the box of a table that is not one of its ends**: a table in the way is moved aside in its
     row or column (in very dense views a few can stay). A table that points at itself has a loop on its
     top edge, with its label standing level beside the loop.
   - A dashed line **from names** is a guess from the column names alone: a column named like the key
     of another table (`customer_id` -> `customers.id`), or with the same name as a column that is
     **unique by itself** in exactly one other table - its one-column primary key, or a column under a
     UNIQUE constraint or a unique index (`dealer_code` in a table of subscriptions and in the table of
     dealers). Two columns that are both not unique are never linked by their name (two tables that each
     have an `effective_from` date), and a column whose name says it is personal (an e-mail address, a
     phone or service number) is not linked to a unique column of that name: a customer's e-mail is not
     an employee's. A source that was
     added before 13 Oct 2026 gets these links when it is read again (the round arrow on the source).
3. **Read the details** (right panel): columns with type, PK / FK badges and nullability, the table's
   relationships (click one to jump), and where each piece of information came from. `PK?` and `FK?`
   mean "inferred, not declared"; `unique` marks a column that a UNIQUE constraint or a unique index
   makes unique (a primary key is just `PK`).
4. **Export**: the whole diagram or the current view (filters and focus applied) as `.drawio`
   (open in draw.io desktop) or `.png`. A diagram too large for one PNG is refused with a message:
   export it as `.drawio`, or use focus mode or the schema filter to export part of it. A one-to-one
   relationship is one-to-one in both files: "1" at both ends in the PNG, and a single bar instead of
   the crow's foot in draw.io.
5. **Profile** (centre, second tab): what is in the tables - see [Profiling](#profiling).
6. **Quality** (third tab): findings about the data - see [Data-quality findings](#data-quality-findings).
7. **Relations** (fourth tab): is each relationship true in the data? - see
   [Relationship checks](#relationship-checks).
8. **SQL** (fifth tab): run your own read-only query - see [SQL tab](#sql-tab).
9. **Report** (sixth tab): one file to pass on - see [Report](#report).

Searching the table list also searches column names. Press `/` to jump to the search box. After
profiling, the table list shows each table's row count; `~` marks an estimate and `>` a table that has
at least that many rows and was not counted further.

### Light and dark theme

The switch in the header has three choices: **System** (the default: the app follows the light or dark
mode of Windows, also when you change it while the app is open), **Light** and **Dark**. Your choice is
remembered in this browser - nowhere else - and other open tabs of the app follow it. The ER graph and
the charts change with it at once; the tables stay where you put them and the zoom is kept.

**What you export does not follow the theme.** The `.drawio` and `.png` diagrams and the HTML and Excel
reports are always light: they are made to print and to pass on.

Every colour pair of both themes is checked for contrast (text 4.5:1, lines and icons 3:1):
`python -m scripts.contrast` prints the table.

### Small windows

The app is laid out for a window 1366 px wide and adapts to narrower ones (for example Windows display
scaling of 125% or 150%, or a window that fills half the screen):

- A list that is wider than its panel - the tables and columns of the Profile tab, the relationships -
  first makes its number columns narrower, then **scrolls sideways inside its own box**. Its name
  column stays in view while you scroll, and a name is never squeezed: it wraps after an underscore and
  shows the whole name on hover when a part is cut.
- The tab bar and the tool bars wrap to a second row instead of hiding anything.
- The two buttons at the ends of the tab bar hide the side panels and give the centre the whole
  window. Below about 850 px with both side panels open, hide one.

## Profiling

Profiling needs data, so it works on tables that come from a **live database** or from **CSV / Excel
files**. A table known only from SQL files says "structure only, no data to profile".

Press **Profile all** (or open a table, or tick some in the list, and press **Profile selected**).
The work runs in the background: a progress bar shows the table being read, **Cancel** stops it before
its next step, and what was finished is kept. You can close the browser tab - the job goes on, and the
progress is back when you reopen the page. Jobs do not survive closing the app itself. A workspace runs
one job at a time: profiling or a relationship check, not both.

**What one table costs the database.** At most:

1. **one row-count step** - the engine's own estimate, read from its catalog with one statement for the
   whole schema: Oracle `ALL_TABLES.NUM_ROWS`, PostgreSQL `pg_class.reltuples` (for a partitioned table:
   the sum over its partitions), SQL Server `sys.partitions`, MySQL `information_schema.TABLES.TABLE_ROWS`.
   An estimate is shown with `~`. SQLite keeps no estimates: there every table is counted instead, one
   `COUNT(*)` a table, each under the statement timeout;
2. **one read** - the columns that can be profiled, for the whole table when it is small and for a sample
   when it is big, streamed into an in-memory DuckDB copy. Large and binary columns (LOB, BLOB, CLOB,
   `bytea`, XML, JSON, SQL Server's `(MAX)` types ...) are not fetched: only whether they hold a value.

Every statistic, top value, histogram and the scan for personal values is then worked out **from that
copy**. No statement per column is sent to the database, nothing is retried column by column, and
nothing is retried after a timeout. The copy lives in memory only: DuckDB has no temporary directory
here, so what does not fit in `DATA_EXPLORER_MEMORY_MB` fails instead of spilling to disk.

| The table | What is read | Row count shown |
| --- | --- | --- |
| up to 100,000 rows (`DATA_EXPLORER_SAMPLE_ROWS`) | the whole table | exact - the read reached its end |
| bigger, Oracle, up to 1 GB on disk (`DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB`) or of unknown size | `SAMPLE (p) SEED (n)` - a random sample of **rows**: every block is read once, the sample is even | `~` the estimate |
| bigger, Oracle, above that size | `SAMPLE BLOCK (p) SEED (n)` - only `p` % of the table is read, but it is a **block sample: a few stretches of the table, may not represent all of it** | `~` the estimate |
| bigger, PostgreSQL | `TABLESAMPLE SYSTEM (p) REPEATABLE (n)` - a random sample of blocks | `~` the estimate |
| bigger, SQL Server | `TABLESAMPLE (p PERCENT) REPEATABLE (n)` - a random sample of pages | `~` the estimate |
| bigger, SQLite | about 100,000 rows **spread over the table, not random**: 1,000 stretches of 100 rows at even distances, read by row number - no scan of the table | exact - it was counted |
| bigger, MySQL / MariaDB, any view, a SQLite table made `WITHOUT ROWID` | `LIMIT n` - **the first 100,000 rows, not random** | `~` the estimate (SQLite: the count) |
| size not known | the first 100,001 rows | exact if fewer came back; otherwise `>100K` |

- `p` is chosen so that the sample holds about 100,000 rows. A sampled read stops at 200,000 rows
  whatever the estimate said, so a stale estimate cannot make the app read a huge table.
- **Oracle samples rows up to a size limit, and blocks above it.** `SAMPLE BLOCK` reads only a few
  percent of a table, but what it returns is a few runs of neighbouring blocks: on the 2,000,000-row
  test table 34 seeds gave between 0 and 200,000 rows, from a median of 16 of the table's 200
  stretches. For a table loaded in time order that is a few time windows, not a cross-section. Row
  sampling, `SAMPLE (p)`, gave 99,196 to 100,569 rows from all 200 stretches every time - at the cost
  of reading every block of the table once. So:
  - a table **up to 1 GB on disk** is sampled by rows. The limit is `DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB`
    (default `1024`); with `0` every table whose size is known is sampled by blocks.
  - a table **above the limit** is sampled by blocks, and wherever that sample is shown - the Profile
    tab, a relationship's evidence, the report's sampling notes - it is called what it is: **"block
    sample: a few stretches of the table, may not represent all of it"**. Null percentages, top values
    and coverage measured on it can be off.
  - the size comes from the catalog statement that also gives the row estimates (`ALL_TABLES`: blocks
    x 8 KB, or rows x average row length, whichever is larger). A table **whose size is not known** is
    sampled by rows; the table's time budget still protects it.
  - a block sample that comes back nearly empty (under a quarter of the target) is read **once** more
    with a higher percent. That is the only second read there is.
- PostgreSQL's `TABLESAMPLE SYSTEM` and SQL Server's `TABLESAMPLE` pick single pages all over the table
  (measured the same way: 94,065 to 107,072 rows from 196 to 200 stretches, and 93,683 to 106,005 rows
  from 193 to 199). `python -m scripts.sample_spread` repeats the measurement on the test databases.
- **An estimate of 0, -1 or NULL means "not known"** (a table that was never analysed, or just created).
  The app then reads up to 100,001 rows: fewer back is the exact count; otherwise the table is shown as
  `>100K` ("more than 100,000, count unknown") and profiled on those rows. **On an engine that keeps
  estimates it never runs `COUNT(*)` just because one is missing.**
- **SQLite has no estimates at all, so its tables are counted**: one `COUNT(*)` a table in **Row counts
  only** and before a profile, each under the statement timeout (0.4 seconds for the 67 tables and 4.5
  million rows of the demo database). A count that does not finish leaves the table at `>100K`, and the
  job, the table's page and the report say so.
- **A big SQLite table is read spread over the whole table**, not on its first rows: its row numbers
  are cut into 1,000 equal parts and the first 100 rows of each are read, by row number, which is as
  fast as reading the first rows. A table filled in time order is then seen from its first month to its
  last (the first 100,000 rows of the demo's 2,000,000 usage records were six weeks of two years). It is
  labelled "spread over the table, not random" wherever a sample is named. A part gives at most its
  share, so where the row numbers lie in a bunch the sample would come out small: then the first rows
  are read, and labelled so. Views and tables made `WITHOUT ROWID` keep the first rows.
- **Exact count** (a button on the table) is the only full `COUNT(*)` that profiling runs on Oracle, SQL
  Server, PostgreSQL and MySQL, and only when you press it. It runs under the statement timeout.
- **An exact count is kept.** "Profile again", "Profile all" and "Row counts only" never replace a count
  that was taken exactly with "more than 100,000": it stays, with the time it was taken (the table's
  page says "counted ..."), until a newer exact count or the catalog's own figure replaces it. The note
  "The table has more than 100,000 rows; how many is not known" is only shown while that is all that is
  known.
- **Row counts only** does the row-count step alone; a table without an estimate is then counted no
  further than 100,001 rows (SQLite: exactly, as above). In the Profile tab's list the **Table** and
  **Rows** headings sort the list, both ways.
- **Each table has a time budget**, 120 seconds (`DATA_EXPLORER_TABLE_BUDGET`), for its read and its
  statistics together. When it is used up the table is marked **timed out** and the job moves on to the
  next table. What an earlier profile saved for it stays.
- CSV / Excel tables cost no database anything. They are profiled whole up to 1,000,000 rows
  (`DATA_EXPLORER_SAMPLE_THRESHOLD`) and on a random sample of 100,000 rows above that.

Every profile says whether it was sampled, how, and on how many rows; the table shows a "Sampled" note.
On a sample the null percentages are estimates, and distinct counts, top values and duplicates are those
of the sample.

**For each column:** rows checked, empty count and %, distinct count; minimum and maximum; the average
for numbers; the shortest and longest length for text; the 10 most frequent values with their counts;
a histogram (numbers: about 12 equal ranges; dates: by month, or by year when they span more than three
years). Click a column to see its top values and histogram. Below the columns are the table's first 20
rows, read live each time (one small statement).

**Files are read as text.** Every column of a CSV file is read as text and then given the first type
that *all* its values can be read as - whole number, number, true / false, date, timestamp - so a bad
row far down a file cannot fail the load, and a code such as `0550000001` or `007` keeps its leading
zeros. A column where most, but not all, values are numbers stays text and gets a "mixed types" finding.
Column types shown after adding a source come from the file's first 20,000 rows.

### Personal data

Columns that look personal are **masked everywhere by default** - in sample rows, top values, minimum
and maximum, in the SQL tab's results and in reports: `05******67`, `a***@d***.com` (the first and last
characters stay). **Values of four characters or fewer, birth dates and any text that looks like a date
keep nothing**: `****`, `**/**/****`. Counts, null % and distinct counts are still shown.

**Pay is hidden completely.** A column whose name holds salary / salaries, wage / wages, bonus / bonuses,
compensation or remuneration (`salary`, `base_salary`, `HourlyWage`, `annual_bonus`) keeps nothing of
its values, not even their length: every value is `*****`, so 9,000 and 185,000 look the same. For such
a column the Profile tab and the report show counts, null % and distinct counts only - no minimum,
maximum, average, top values, histogram or text lengths - and its sample values are `*****`. In the SQL
tab the column and anything built from it (`AVG(salary)`, `MAX(salary)`, `salary * 12`, a `CASE` on it,
under any alias, through a subquery or a `UNION`) is `*****`; `COUNT(salary)` is a number of rows and
stays visible. **`pay`, `allowance` and `deduction(s)` are pay only in a table of staff or pay** - one
whose own name holds `HR`, payroll, payslip, salary, wage, employee, `emp`, staff, personnel or worker:
`HR_PAYROLL_RUN.GROSS_PAY`, `NET_PAY`, `OTHER_DEDUCTIONS`, `HR_SALARY_HISTORY.HOUSING_ALLOWANCE`. Not
there either: a period or a date (`PAY_MONTH`, `pay_date`, `pay_period_start`) and a data allowance
(`allowance_gb`, `data_allowance`). In any other table these words say too little - `PAY_TYPE` of a
tariff plan, an allowance of a bundle - and `payment` and `commission` are never pay: payment is a
business table. `compensation` in a table of complaints, disputes, claims or tickets is money paid to
a customer and is shown. Mark a column yourself where your database means something else. What masking
cannot do: a query can still *use* a hidden column in `WHERE`, `ORDER BY` or `GROUP BY` (`SELECT
COUNT(*) FROM staff WHERE salary > 100000` answers with a count), so the SQL tab is for people who may
see the data.

**A name is read together with the name of its table**, which says what the table holds:

| The column | In a table of | It is |
| --- | --- | --- |
| `gross_pay`, `housing_allowance`, `other_deductions` | staff or pay (`hr_payroll_run`, `employee_pay`) | pay, hidden completely; anywhere else: shown |
| `compensation` | complaints, disputes, claims, tickets, cases, refunds | shown - paid to a customer (in a table of staff it stays pay) |
| `dealer_name`, `grade_name`, `family_name` - `<x>_name` | x itself (`sal_dealer`, `hr_pay_grade`, `prd_product_family`) | the name of that thing: shown |
| the same `<x>_name` | people (customer, employee, subscriber, contact, user, member, account ...) | a person's name: masked. So are `first_name`, `last_name`, `full_name` ... wherever they stand, and `contact_name` in `sal_dealer` |
| `building_no`, `unit_no`, `additional_no`, `postal_code` | addresses (`crm_address`) | address: masked. In a table of customers or shops a postal code is shown, like its city and district |
| `latitude`, `longitude` (`lat`, `lng`, `lon`) | people or addresses | address: masked - where a person lives. In a network table (`net_site`) it is a site's place: shown |
| `account_holder`, `card_holder` | any | a person's name |
| `puk_code`, `router_mac`, `mac_address` | any | a device identifier, like IMEI and ICCID |

What a table holds is read from the words of its name (`names.table_kinds` in the rules file, below).
**A table of people that the list does not know shows its `<x>_name`** - `pilot_name` in `pilot` would
be hidden, `broker_name` in `broker` is shown: add the word to `people`, or mark the column. A table
called just `family` shows `family_name`.

A column is treated as personal when

- its **name** says so - see the table below;
- its **values** look like it, in up to 1,000 rows of the table's in-memory copy;
- **you mark it**: click the "Mark" / "Personal" badge of a column. Your marks are saved per workspace
  and win over the automatic rules in both directions.

The SQL tab judges every column of a result by the same three rules, for the table column it comes
from: see [What is masked in a result](#what-is-masked-in-a-result).

Whether a column is personal is decided **at the moment a table's statistics are saved**, with your
marks as they are then: a mark made while a job is running is honoured. Marking a column later masks
what was already saved for it at once. Un-marking cannot bring the values back (masking is one-way):
profile the table again. Quality findings never quote a value of a column that is personal now.
When the rules gain a category (as with pay), statistics saved before are never shown unmasked, and
what is stored for them is masked the first time the workspace is opened after the app starts. The
other way round - a column the rules no longer treat as personal, like `dealer_name` in a table of
dealers - the statistics saved for it stay masked until the table is profiled again; the Profile tab
says so.

**Show personal values** (top right of the Profile tab) reads the real values again from the source and
shows them unmasked, with a warning banner, until you switch it off. For a table that is one read of
its personal columns - the same sample its profile was made on - and the result is kept in memory while
the switch is on, so opening the table again reads nothing. The switch is held in the app's memory
only: it is never saved, every start of the app begins masked, and switching it off drops the values.
What is on disk stays masked. Reports are always masked, whatever the switch says.

#### The rules are a file you can extend: `app/profiling/personal_rules.json`

Every name rule and value rule is data, not code. Edit the file and restart the app; a wrong edit stops
the app at start with a message that names the problem. `DATA_EXPLORER_PERSONAL_RULES=<path>` makes the
app read another copy of the file (for example one your team keeps outside the project folder).

| Key | What it holds |
| --- | --- |
| `categories` | The text shown for each category ("Looks like phone number"). A category needs a text here. |
| `names.words` | Per category, the words that name it. A column name is split into lower-case words first (`FullName`, `full_name` and `FULL-NAME` all give `full`, `name`); the column is in the category when one of its words, or all of them joined, is in the list. |
| `names.word_groups` | Rules that need one word from **every** group: `[["calling", "called"], ["number", "no"]]` matches `calling_number` and `CALLED_NO`. `unless_table_has` skips the rule when the table's own name holds one of the listed words (`line_number` is a phone line, except in `invoice_lines`). `only_in_tables` / `not_in_tables` name kinds of table (see `table_kinds`) in which alone the rule counts, or in which it does not: `pay`, `allowance`, `deduction` only in `staff` tables. Without a table to go by, an `only_in_tables` rule says nothing. `unless_column_has` skips the rule when the column also holds one of these words (`allowance_gb`), `unless_followed_by` when nothing but these words follows the rule's word (`pay_month`, `pay_date`; `net_pay`, `monthly_pay` and `pay_per_month` stay pay). |
| `names.table_kinds` | What a table holds, told by the words of its own name, singular or plural: `people`, `address`, `staff`, `customer_case`. `hr_employee` is a table of people and of staff, `crm_address` one of addresses. Add your own words (a table of `broker`s is a table of people), or a kind of your own for a rule of your own. |
| `names.word_exceptions` | A word of `names.words` that does not count for its category in some kinds of table: `compensation` is not pay in a `customer_case` table (`only_in_tables`) - unless that table is a `staff` table too (`not_in_tables`). |
| `names.own_name` | `<x>_name` in a table whose own name holds x is the name of that thing: `dealer_name` in `sal_dealer`. `not_in_tables`: never in these kinds of table (`people`). `not_for_words`: words that go with a person's name and never stand for a thing (`first`, `last`, `full`, `middle` ...). Take the key out and every `<x>_name` is a person's unless x is one of `thing_words`. |
| `names.about_words` | A last word that makes the column a fact about the thing, not the thing: `email_verified`, `phone_type`. |
| `names.key_words`, `names.kept_with_key_word` | A last word that makes it a key into another table (`address_id`, `card_key`), and the categories that stay personal even then (`national_id`). |
| `names.thing_words` | `<thing>_name` is the name of a thing, not of a person: `plan_name`, `branch_name`. A bare `name` or `nm` column is personal unless its table is a table of such things. |
| `values.rules` | Checked in order; the first rule that fits names the column. `patterns` are Python regular expressions that must match the whole value; `share` is how much of the column's non-empty values must match **any** of the rule's patterns; `normalise` names what is done to a value first. `not_for_families` and `not_for_name_endings` switch a rule off for some columns. |
| `values.normalisers` | `remove` (a regular expression of characters to drop) and `replace` (pairs of pattern and replacement, applied once each). |
| `values.min_values` | Fewer non-empty values than this say nothing about a column (3). |
| `masking` | `fully_masked_up_to` (4 characters), `fully_masked_categories` (`birth_date`, `salary`: every letter and digit is hidden), `length_hidden_categories` (`salary`: nothing stays, not even the length - every value is `*****`, and no top values or text lengths are kept) and `date_like`: regular expressions of text that is masked completely. |

What ships in the file:

- **Names**: name, first / last / full name, `cust_nm`, `nm`, `fname`, `lname`; msisdn, mobile, `mob`,
  `mob_no`, phone, tel, fax; `a_number`, `b_number`, `anumber`, `bnumber`, calling / called / caller /
  callee / other / dialled / originating / terminating number, `service_number`, `subscriber_no`,
  `subscriber_number`, `contact_no`, `contact_number`, `line_number`; `imsi`, `imei`, `iccid`,
  `sim_serial`, `puk`, `mac`; email; national_id, nid, iqama, id_number; passport; address, street; dob /
  birth; iban; card; account / card holder;
  **pay**: salary, salaries, wage, wages, bonus, bonuses, compensation, remuneration - and pay,
  allowance, deduction in a table of staff or pay (never: payment, commission).
- **Names that need their table** (the table above): pay words in `staff` tables; postal code and
  building / unit / additional / apartment / floor number in `address` tables; latitude and longitude
  in `people` and `address` tables. About 150 words tell a table of people.
- **Values**: e-mail addresses (80%); Saudi IBANs (80%); **phone numbers (50%)** - Saudi mobiles
  (`05xxxxxxxx`, `9665xxxxxxxx`, `5xxxxxxxx`), Saudi landlines (`01x` - `07x` and nine digits in all
  after the zero) and international numbers (`+` or `00`, then 8 to 15 digits) count together; 10-digit
  national ID / Iqama numbers starting with 1 or 2 (80%).
- Before a value is matched as a phone number its spaces, dashes, dots and brackets are removed, `00966`
  becomes `+966` and `+966 0` becomes `+966`: `+966 (0) 55 123 4567` is read as `+966551234567`.
- The national-ID rule is **not** applied to date and timestamp columns, nor to columns whose name ends
  in `_ts`, `_time` or `epoch`: ten-digit numbers there are seconds, not IDs.

## Data-quality findings

The **Quality** tab lists findings worked out from the saved statistics, grouped by severity. Filter
by table and by rule; click a finding to open its table and column in the Profile tab.

| Rule | Found when | Severity |
| --- | --- | --- |
| Empty table | the table has no rows | medium |
| Column with no values | every row of the column is empty | medium |
| Constant column | every row that has a value holds the same one | low |
| Mostly empty column | more than 50% of the rows are empty (`DATA_EXPLORER_HIGH_NULL`, e.g. `0.7`) | low; medium from 95% |
| No primary key | none is declared and no column looks like one by its name | medium |
| Duplicate values in a key | the declared one-column key, or the column that looks like the key, repeats a value | high |
| Mixed types in a file column | a text column of a CSV / Excel file holds mostly numbers, or mostly dates | medium |

"No primary key" needs no data; the others appear once a table is profiled. A finding on a sampled table
is marked "sample", and the mark's tooltip says how the sample was taken (the first rows, or spread over
the table): a "Constant column" found on the first 100,000 rows of a table is a weaker finding than one
found on rows from all over it.

## Relationship checks

The **Relations** tab measures every relationship of the workspace on the data - declared keys, links
inferred from joins and from names, and links you accepted - with one column or several:

| Shown | What it means |
| --- | --- |
| **Coverage** | of the child rows that have a key, the share whose key exists in the parent. Never "100%" while a row is missing: 420 orphan rows of 2,000,373 are ">99.9%" |
| **Orphans** | child rows whose key is not in the parent. A count that is only a lower bound is written with the "greater than or equal" sign in front (at least 40) and a tag that says why: "5,000 of 37,026 keys looked up", "first 100,000 rows". The evidence lists up to 20 example keys, those with the most rows first; keys of a personal column are saved and shown masked |
| **Empty keys** | child rows whose key is empty (they are neither covered nor orphans) |
| **Cardinality** | 1-1, 1-N or N-N as the catalog and the data show it, and whether that is **proven** |
| **Measured on** | all rows of the child table, or a **sample** of it - a sampled result always says so |

Press **Check all**, or tick relationships and press **Check selected**. Click a relationship to see
its evidence: how many keys were looked up, what makes the parent key unique, why the cardinality is
what it is, and what the check cost. The list sorts by any column (click a heading) and filters by
table or column, by kind, and by result (with orphans, not checked, on a sample, cardinality not proven).

**What a check costs the database** - the same rules as profiling, in the same kind of background job
(progress, **Cancel**, one job per workspace across profiling and this):

- **the database counts the relationship itself, over the whole child table**, when child and parent
  are tables of the same database and their key columns are of the same kind: ONE read-only statement
  (an anti-join, `NOT EXISTS`) gives the rows, the empty keys and the orphan rows - exactly, however
  big the table is - and one more gives the example keys, only when there are orphans. Both run under
  the statement timeout and the relationship's time budget. A number is never compared with text
  inside the database (an error on some engines, a silent conversion on others): such a pair is looked
  up as below. `DATA_EXPLORER_RELATION_WHOLE_TABLE=0` switches the count off;
- the **child table is read once per job**: only its key columns, whole when it is small and with
  profiling's sampling when it is big. It says how often a key repeats in the child (the cardinality);
- otherwise - key columns of different kinds, files, a count that fails or runs out of time - the
  **parent is asked "which of these keys do you have?"**: bound `IN` lists of 500 keys, at most 5,000
  distinct keys per relationship (`DATA_EXPLORER_RELATION_KEYS`; files look every key up). What is found
  on a sample, or on some of the keys, is a **lower bound, and the list row itself says so** ("at least",
  and the tag of the table above); the evidence has the full sentence, and "Measured on" says what was
  really measured;
- a **date key is compared in the form its parent column stores**: a day on engines with a DATE type, a
  day and a time for timestamps and Oracle's DATE, and the stored text on SQLite, which has no date type;
- whether the **parent key is unique** comes from the catalog first (primary key, unique constraint,
  unique index). Only without such proof is the parent asked once for a duplicate; if that does not
  answer in time the result says "unique not proven". A sampled distinct count is never taken as proof;
- each relationship has a **time budget** of 60 seconds (`DATA_EXPLORER_RELATION_BUDGET`). Out of time
  gives **"not checked (timeout)"**; nothing is tried again on the whole table;
- both tables must be in the **same source**; otherwise "not checked: tables are in different sources".
  A table known only from SQL files has no data: "not checked: ... has no data".

**Find links by values** looks for links nobody declared: pairs of columns of the same kind (numbers
with numbers, text with text) where the values of one are found in a column of another table that is a
key or proven unique. The pairs are chosen **by their values**, not by their names:

- **a first look at every pair.** Each table is read once, lightly - about 2,000 rows of the columns
  that could point somewhere - and 30 different values of each column are looked up in each key they
  might point at, the values of all those columns in one statement per key. On the demo database that
  is 11,193 pairs, of which 11 share values. **On Oracle** that light read takes whole blocks
  (`SAMPLE BLOCK`), so only those blocks are read: a sample of rows (`SAMPLE (p)`) reads every block
  of the table. Blocks can come back with few rows or none (a small table, or a table written in long
  runs): when a column then has fewer than 3 values, the table is read once more the usual way, and the
  job's notes say which tables were read twice;
- **the full check for the pairs that share values** (at least half of the 30): up to 1,000 distinct
  values, at most 200 pairs per run (`DATA_EXPLORER_RELATION_PAIRS`). A pair is suggested from 80%
  overlap (`DATA_EXPLORER_RELATION_MIN_OVERLAP`) when the names give a hint, and only from 95% without
  one - and never for small whole numbers, which match every key by accident;
- **what counts as a hint in the names.** The word that names what a column points at (`customer` in
  `billing_customer_id`), not a word inside the name of something else (the `order` in `sort_order`) and
  not the first word that several tables share (the `SUB` of `SUB_DEVICE` and `SUB_SIM_CARD` does not
  make `active_subs` point at them);
- **a key that counts up from about 1** (1, 2, 3 ... 48,979) holds every count and every sort order by
  accident: a column of numbers is only suggested to such a key when its name points at that table.

Nothing is added by itself:

- **Accept** makes the suggestion a relationship of the kind "from values": a dotted line in the graph
  and in both exports, listed with its own checks - and it is **checked at once**, so the list and the
  report do not show it as "never checked" until the next "Check all". It keeps the share it was
  suggested with, as measured: a suggestion of 98.8% is "values 98.8%" in the Kind column, the graph's
  right panel and the report, never a rounded 99%.
- **Reject** hides it, and it is not suggested again.
- **Undo** (under "Accepted links" / "Rejected links") puts one back among the suggestions.

## SQL tab

The **SQL** tab runs one query of your own against a source of the workspace - a live database, or
CSV / Excel files (read by DuckDB; every table of the source is there under its name). SQL files hold
no data, so they cannot be queried.

- **One `SELECT` or `WITH` statement.** Press **Run** or Ctrl+Enter.
- **At most 1,000 rows come back.** The app fetches 1,001 and stops; when there are more, the result
  says "more rows exist". To know how many, ask with `COUNT(*)`.
- **The statement timeout applies**: 30 seconds (`DATA_EXPLORER_SQL_TIMEOUT`), then the query is stopped.
- **Personal columns are masked** in the result unless "Show personal values" is on. A result column
  is judged by the table column it comes from, the way the Profile tab judges that column - see
  [What is masked in a result](#what-is-masked-in-a-result).
- **Export CSV** saves the result exactly as shown - the same rows, masked values still masked. Text a
  spreadsheet would run as a formula gets a leading apostrophe.
- **The last 50 query texts** of the workspace are kept (text only, with whether it ran, was blocked or
  failed); click one to put it back in the editor. **Results are never saved**: the last few live in
  memory for the CSV export and are gone when the app stops - at once, for unmasked ones, when the
  switch goes off.

**Every statement is checked before anything is sent**, and what is sent is the checked text with its
comments removed. The check reads the statement the way its engine does (Oracle `q'[...]'` strings,
`[name]` on SQL Server, backticks on MySQL), so that nothing can hide in a string or a comment. Refused:

| Refused | Examples |
| --- | --- |
| anything that is not one `SELECT` / `WITH` | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, DDL, `GRANT`, `EXEC`, `CALL`, PL/SQL blocks, `COPY`, `ATTACH`, a second statement after `;` |
| `SELECT` that writes or locks | `SELECT ... INTO`, `FOR UPDATE` / `FOR SHARE`, `LOCK IN SHARE MODE`, SQL Server lock hints (`UPDLOCK`, `XLOCK`, `TABLOCKX` ...) |
| sequences | `NEXTVAL`, `NEXT VALUE FOR` - a sequence changes when it is read |
| functions that are not on the list | only built-in functions that read their arguments are allowed: the list is `app/analysis/sql_functions.json`, per engine. A function with a schema or package in front of it (`dbms_lock.sleep`, `dbo.my_function`) is always refused. **Your own functions are refused because a function can change data** - in Oracle even inside a read-only transaction, through an autonomous transaction (the tests prove that on a real server) |
| Oracle: a name that is not a column where it stands | Oracle calls a function that takes no argument **without brackets** (`SELECT my_function FROM dual`). See [Oracle: how names are checked](#oracle-how-names-are-checked) |
| PostgreSQL: `t.name` where `name` is not a column of `t` | PostgreSQL calls `name(t)` for `t.name` and `(t).name`. See [PostgreSQL: table.name](#postgresql-tablename) |

To allow one more built-in function, add it to `app/analysis/sql_functions.json` and restart, or set
`DATA_EXPLORER_SQL_FUNCTIONS=name,oracle:other_name` for this PC only. Add only functions that read
their arguments; never one your own database defines unless you have read its code.

### What is masked in a result

A result column is **followed back to the table column it comes from** and judged the way the Profile
tab judges that column, so the two tabs mask the same columns. `store.name` is the name of a shop and
is shown; `employee.first_name` is a person's and is masked, whatever the query calls it.

| The result column is | It is masked when |
| --- | --- |
| a column as it is: `first_name`, `e.first_name AS who`, a column of `SELECT *` - also through a subquery or a `WITH` query | the Profile tab masks that column of that table: by its name together with its table's (a bare `name` is a person's unless the table is a table of things), by what profiling found in its values, or by your mark |
| built from columns: a function, `CASE`, arithmetic, `COALESCE`, an aggregate - `UPPER(first_name)`, `AVG(salary)` | one of the columns it is built from is masked. A subquery inside it counts with everything it reads, its conditions too |
| `COUNT(...)` and nothing else | never: it is a number of rows, whatever it counts |
| a column of a `UNION` / `INTERSECT` / `EXCEPT` | one of the branches puts a masked column into it; a count only when it is one in every branch |
| not resolved for sure | its own name looks personal - without a table to go by, so every `name` does - or a column of that name is marked personal in any table, or is hidden in any table of the workspace because of what that table is (`gross_pay`, `latitude`), or the statement builds it from such names. It masks too much rather than too little |

- **Your mark wins, for that table's column and no other.** "Personal" on `store.name` masks it in
  every query that shows it, also built into something else, and leaves `product.name` alone. "Not
  personal" on `employee.first_name` shows it, and its values are then not looked at again.
- **What is followed**: aliases in `FROM` and `JOIN`; names with their schema in front; quoted names;
  subqueries in `FROM`; `WITH` queries; every branch of a `UNION`; `SELECT *` and `t.*`, which are
  expanded from the structure the source read. A column without a table in front of it is looked for in
  the tables of its own `SELECT`. Names are compared as the engine keeps them: in upper case on
  Oracle, in lower case on PostgreSQL (a quoted name as written), without regard to case elsewhere.
- **Not resolved for sure**, and then judged by names as before:
  a table the source has not read (another schema, a view when views were not included, a function in
  `FROM`); a name without its table that two tables of the `FROM` clause both have; a `WITH` query that
  reads itself or one further down its list; `SELECT *` of a join with `USING` or `NATURAL`, where the
  engine merges columns; an expression with a word the app does not know (a named window, `COLLATE`, a
  column alias used as a value); and the whole result when the column names the engine returns do not
  match what was worked out - a table that has changed since the source was read: refresh the source.
  Writing the table in front of a column (`s.name`) resolves most of these.
- **After that the values are still looked at**: a column that is not masked by these rules is masked
  when its values look like phone numbers, e-mail addresses, national IDs or IBANs - but not a column
  you marked "not personal".
- **Point at the heading of a masked column to see why**: `customer.first_name: looks like name`,
  `Built from employee.salary (looks like salary / pay)`, `store.name: marked personal by you`; for a
  column that was not resolved, `Looks like name`. Point at "N personal columns masked" for all of them
  at once, a line each; a column the statement renamed is listed with both names
  (`who: employee.last_name: looks like name`).

### Each engine's own reserved words

To judge names (the two sections below) and to follow a result column to its table (the section
above), the app reads a statement: its `FROM` clauses with their tables and aliases, and the names its
select lists give. It takes an unquoted word for a table or an alias **when the engine does** - each
engine has its own reserved words (`RESERVED` in `app/sqlguard.py`). `public`, `session`, `comment`,
`level`, `resource` and `size` are reserved on Oracle and plain names elsewhere (SQL Server reserves
`public` too), so `FROM public.customers` and `FROM customers session` are read on PostgreSQL as
PostgreSQL reads them. Until 13 Oct 2026 Oracle's list was used for every engine.

The lists were asked of the engines themselves, word by word: `SELECT 1 FROM <word>` must answer
"there is no such table", and `FROM <a table> <word>` must make the word that table's alias.

| Engine | Words it does not take for a name | Asked of |
| --- | --- | --- |
| Oracle | 119 - the list of before, unchanged | Oracle 23 |
| PostgreSQL | 101 - exactly what `pg_get_keywords()` calls reserved or "type or function name" | 16.15 |
| MySQL | 264 - `INFORMATION_SCHEMA.KEYWORDS`' reserved ones, and two that MySQL 8.0 still reserved | 8.4.11 |
| SQL Server | 179 | 2022 (16.0) |
| SQLite | 71 | 3.43 |
| DuckDB (files) | 110 - five of them called reserved by its own list although it takes them for names | 1.5 |

`python -m scripts.reserved_words` asks them again (SQLite and DuckDB inside Python, the others in the
test containers) and holds the answers against the lists; the tests do the same. A word too many in a
list costs a refusal - write the name in quotes. A word too few would let the app take for a table
what the engine reads as syntax, so words are added, and none is taken out without asking the engine.

Nothing is weaker for it. The check for names runs **behind** every other rule - writes, locks,
sequences, functions, a second statement - and can only refuse more; it judges names on Oracle and
PostgreSQL only, and on Oracle nothing changed. For masking, a table under the alias `size` or in the
schema `public` is now followed on every engine (before, its columns were judged by their names alone).

### Oracle: how names are checked

On Oracle `SELECT x FROM dual` **calls** a function `x` if there is one - no brackets needed - and a
function can write even inside a read-only transaction. So the check works out, for every name, which
table it belongs to, in its own part of the query (each `SELECT` at its own bracket level, each branch
of a `UNION` / `INTERSECT` / `MINUS` on its own):

| What you wrote | Refused because | Write it like this |
| --- | --- | --- |
| a bare name that is not a column of a table in that `SELECT`'s `FROM` - `SELECT balance FROM customers` | no table there has that column, so it could be a function | check the table; a subquery in an expression may also use the columns of the query around it |
| a column of a table outside the loaded schemas - `SELECT owner FROM all_tables` | the app does not know that table's columns | with its table alias: `SELECT t.owner FROM all_tables t` |
| a table alias or the name of a `WITH` query used alone - `SELECT c FROM customers c` | an alias only stands in front of a dot | `c.city`, `big.total` |
| a column alias outside `ORDER BY` - `SELECT city town FROM customers WHERE town = 'x'`, `GROUP BY town`, `HAVING n > 1` | a column alias is only known in the `ORDER BY` of its own `SELECT` | repeat the expression: `WHERE city = 'x'`, `GROUP BY city`, `HAVING COUNT(*) > 1` |
| an alias, or a column in a subquery in `FROM`, taken from the tables beside it | a subquery in `FROM` only sees itself | join instead, or write `LATERAL ( ... )` |
| any name that the database can call - a column `STATUS` when a function `STATUS` exists | the name could be the function | with its table alias: `s.status`. The message says `t.status` |
| an alias called like a function, package, type or schema - `FROM customers hr`, `SELECT 1 AS status` | "rename the alias" | another alias |
| a table called like one of those, in front of a dot | the name could be a package or schema | give the table an alias |
| a quoted name that is not a column - `SELECT "MY_FUNCTION" FROM dual` | quotes do not stop the call | - |
| a word of SQL used as a name where it is not a column - `SELECT first FROM t`, `SELECT year FROM t` | Oracle reserves few words: a function can be called `FIRST`, `YEAR`, `VALUE`, `CASE` | fine when `t` has that column; an alias behind such a column needs `AS` (`SELECT value AS v`) |
| `TRUE` / `FALSE` as values | not reserved before Oracle 23 | `1 = 1`, or compare with a column |

What still works as before: `ORDER BY` a column alias (also inside an expression: `ORDER BY UPPER(town)`),
`alias.column`, `schema.table` in `FROM` and `schema.table.column`, a `WITH` query in `FROM` and as a
qualifier, a subquery in `FROM` with an alias and `alias.column` outside it, `CONNECT BY` with `LEVEL`,
`ROWNUM`, `SYSDATE`, `SYSTIMESTAMP`, `CURRENT_DATE`, `(+)`, `CASE`, window functions, `FETCH FIRST`.

**The names of what can be called come from the database.** When an Oracle source is added or refreshed
the app reads them once: every `FUNCTION`, `PROCEDURE`, `PACKAGE`, `TYPE` and `OPERATOR` the account
can see in `ALL_OBJECTS`, the synonyms in `ALL_SYNONYMS` that lead to one of those (public ones
included, and every synonym over a database link), and the schema names in `ALL_USERS` - one
statement, under the statement timeout, kept with the source's structure. **If that read fails, the SQL
tab says that it cannot run queries on that source yet, and why; it does not fall back to a weaker
check.** Press the source's refresh arrow to read the list again. The list is as old as the last
refresh: after a function was created, refresh the source. (A statement is also refused without the
list when the name is simply not a column where it stands - that part needs no list.)

Not followed, and refused with a message that says so: `PIVOT` / `UNPIVOT`, `MATCH_RECOGNIZE`, the
`MODEL` clause, flashback (`AS OF`), `JSON_TABLE` / `XMLTABLE` column lists, named windows.

`python -m scripts.call_proof oracle` asks the test database itself: for 33 statements it prints what
the check says and whether Oracle really called the function (each call leaves a row in a log table).

### PostgreSQL: table.name

On PostgreSQL `SELECT t.name FROM t` calls a function `name(t)` when `t` has no column `name` - so does
`(t).name`. In a read-only transaction such a function cannot write (unless it uses `dblink`), but it
still runs. So in `t.name`, `t` must be a table or an alias of that `SELECT` (or of the query around
it, for a subquery in an expression), and `name` must be a column the source knows that table has -
for a subquery or `WITH` query: one of the columns its select list gives.

- A table **outside the loaded schemas** has no known columns: `SELECT c.relname FROM pg_class c` is
  refused. Write the column without the table (`SELECT relname FROM pg_class` - a bare name is never a
  call on PostgreSQL), or add that schema to the source.
- A function in `FROM` needs its column list to be used with a dot: `generate_series(1, 3) AS g(n)`,
  then `g.n`; or use the bare name `g`.
- `( ... ).name` is only accepted as `(table).column`.
- **`FROM public.customers` is read as PostgreSQL reads it**: a schema, a table or an alias may be
  called `public`, `session`, `comment`, `level`, `resource` or `size` - words that only Oracle
  reserves. A word PostgreSQL itself reserves (`user`, `end`, `analyse`, `window` ...) needs quotes
  here as on the server. See [Each engine's own reserved words](#each-engines-own-reserved-words).
- `x::character varying` and `x::double precision` do not name their column `varying` or `precision`:
  a subquery's column has to be given a name with `AS` before it is used with a dot.

**What this check cannot see** - which is why it stands in front of the real protections (the read-only
transaction where the engine has one, the timeout, and a database account that cannot write) and does
not replace them:

- **What a view or a synonym does behind its name.** `SELECT * FROM some_view` is a plain read to the
  check; if the view calls a function of your database, that function runs.
- **What a listed built-in function does on your server** if someone has replaced it or wrapped it.
- **A function created after the source was last refreshed** is not on the list of what can be called.
  It is still refused wherever its name is not a column of a table in scope; where a column of that
  name is in scope, Oracle reads the column, not the function.
- **Which table an unqualified name in `FROM` really is.** `FROM customers` is taken for the table of
  that name the source knows; if the account's own schema holds another `CUSTOMERS`, the columns the
  check believes in are those of the wrong table.
- **How heavy a query is.** A join without a condition runs on the server until the timeout stops it.
- **The driver's own statements** when a connection opens (see [Read-only rules](#read-only-rules)).
- **Masking follows the table a column is read from, not what stands behind it.** A view is a table
  to the app. A personal column that a *view* gives under another name is judged as the view's own
  column: masked when that name says so, when its values look personal or when you mark it - and there
  is no value rule for people's names. A view called like a thing (`store_list`) with a column `name`
  is taken for a list of things, in the Profile tab and in the SQL tab alike.
- **Which table an unqualified name in `FROM` is** matters for masking too: `FROM store` is judged as
  the `store` the source knows.

For files the same check runs, and DuckDB is also cut off from every file but the source's own before
the statement starts (`read_csv`, `COPY`, `ATTACH`, `INSTALL` and `LOAD` could reach nothing even past
the check).

## Report

The **Report** tab makes one file to pass on - to a manager, a DBA, a colleague who does not have the app:

| Format | What you get |
| --- | --- |
| **HTML** | One self-contained file: its styles are inside it, the ER diagram is embedded as an image, and it holds no script and no link to anything outside itself. It opens in any browser and prints cleanly. |
| **Excel** (`.xlsx`) | The same content as sheets: Summary, Tables, Columns, Quality, Relationships, Accepted links, Sampling notes, ER diagram. Header rows are frozen and have filters. Text that comes from the data is always written as text, never as a formula. |

Both hold the same sections: a **summary** (sources, tables, columns, relationships by kind, when the
profiles were read), the **ER image**, the **tables with their row counts**, the **column profiles**
(empty %, distinct, minimum, maximum, the five most frequent values), the **quality findings**, the
**relationship checks** (coverage, orphan rows, empty keys, cardinality with its evidence, example keys
without a parent) with the **links you accepted**, and **notes on sampling** (which tables were read on
a sample, how, and on how many rows).

- **The relationship checks are printed as the Relations tab prints them**: the same numbers, the same
  rounding (never "100%" with orphan rows), and "at least" with its reason when a count is a lower bound.
- **A report is always masked.** It is made to be passed on, so it does not look at the "Show personal
  values" switch at all: with the switch on, the report is the same as with it off. A column that counts
  as personal now, but whose statistics were saved before it did, is masked in the report as well.
- **A report reads nothing from your sources.** It is built from what is saved: the structure, the saved
  statistics and the saved relationship checks. So profile first, run the relationship check, then make
  the report; a table that was not profiled says "not profiled".
- **Scope:** the whole workspace, or the **current view** - the same tables and relationships the ER
  graph shows (the kinds of relationship ticked, the confidence filter, the schema filter, and N hops
  around the focused table).
- **A browser download only.** The file is built in memory and handed to the browser; the app writes
  nothing into the project folder or anywhere else. It lands where your browser saves downloads.
- A diagram too large for one readable image is left out of the report with a note that says so; make
  the report for the current view instead, or export the diagram as `.drawio`.

## Scripts tab

The **Scripts** tab reads a folder of the team's `.sql` and `.py` scripts and finds every table, column
and join they use. It runs the SQL Lineage engine (`app\sql_lineage\`, a copy of
`C:\Projects\SQL_Lineage`). The scripts are **read as text and never run**, nothing is written into
their folder, and no database is opened for it.

1. Enter the **folder** (every `.sql` and `.py` file under it, subfolders included), choose the
   **dialect** - Oracle, SQL Server (T-SQL), PostgreSQL, MySQL or SQLite - and press **Scan**. When the
   workspace has exactly one database source, its engine's dialect is chosen for you. The scan runs in
   the background with a progress bar and **Cancel**, and like profiling it is the workspace's one job.
2. The **schema** the scripts are checked against is the workspace model: its tables, columns, primary
   keys and **declared** relationships. Links the app inferred from names or joins, and links you
   accepted from values, are not declared keys. A workspace without tables scans without a schema.
   - **Names written without their schema.** A table of a database source carries its schema (an
     Oracle owner, a MySQL database: `SALES.ORDERS`), and scripts usually leave it out. A name written
     with a schema is taken as written. A name without one is the model's table of that name without a
     schema, else the one table of that name in any schema (`orders` is `SALES.ORDERS` everywhere in the
     tab). When several schemas have it, the **default schema** of the workspace's database source
     decides; otherwise the name is listed as not certain under Unreadable ("in 2 schemas: HR, SALES"),
     never guessed. The default schema is the account's own, kept when the source is read: a source
     added before this version has it after its next reload (the round arrow). With no database source,
     or several, there is none. The scan's line under its title shows it.
   - **Views.** A database source read without **Include views** has none of its views in the model.
     When some names of the scripts are not in the model, a note above the results says so: "15 names
     are not in the workspace model. ... was read without views: if some of them are views, tick
     Include views on the source, reload it and scan again."
3. After a scan the tab shows what was scanned and when, the numbers, **Rescan**, **Forget scan** and
   **Download report**, and seven views:

| View | What it shows |
| --- | --- |
| **Joins** | Every distinct join: its tables and columns, its types (`where` is a comma join, `merge` a MERGE ... ON), how many statements and scripts use it, whether a foreign key is declared for it, and flags (derived through a CTE, a condition that is not plain equality, a conflict). Sortable, with a filter; a row opens its statements and lines |
| **Conflicts** | The same two tables joined on different columns in different places. One of them is often a bug |
| **Suggested views** | Joins that repeat together (in 3 or more statements of 2 or more scripts), each with a draft `CREATE VIEW` to copy and the statements that could use it. Nothing is run or changed |
| **Subquery links** | Columns tied through `IN`, `NOT IN`, `EXISTS` or a correlated subquery. They are not joins, but they count for impact |
| **Joins without a key** | Joins the model does not declare as a foreign key |
| **Impact** | Pick a table or a column: every statement that uses it ("Direct"), then level by level the tables that the scripts reached write and the statements that read them, each with its script and line |
| **Unreadable** | Every file and statement that could not be read for sure (SQL built at run time, PL/SQL blocks ...): file, line, reason, and its text as written |

- A table name in these lists is a link when the table is in the model: it selects the table in the app.
- **Limits:** at most 5,000 script files per folder; a file over 5 MB is not read and is listed as
  unreadable (`MAX_FILES` and `MAX_FILE_MB` in `app\sql_lineage\settings.py`).
- **Download report** gives SQL Lineage's own offline report: one HTML file with a graph and an impact
  picker, built in memory and handed to the browser as a download.
- **Saved:** the last scan of each workspace, in `data\lineage\` and `data\explorer.db`: names and the
  SQL text of the scripts, never data. A new scan replaces it; **Forget scan** and deleting the workspace
  delete it.
- The tab gives what the engine's command line gives for the same folder, dialect and database file
  (`lineage.bat scan <folder> --dialect <d> --schema <file>` in `C:\Projects\SQL_Lineage`);
  `tests/test_scripts_api.py` checks that.

## Connecting to a database

Use the **Live database** tab of "Add source". The password is asked for each session; it is kept in
memory only while the app is running and is **never saved**. "Save this connection" stores everything
except the password.

| Engine | Driver (installed by `run.bat`) | What to enter | Notes |
| --- | --- | --- | --- |
| **Oracle** | `oracledb`, thin mode | Host, port 1521, **service name**, user, password | **No Oracle client or Instant Client is needed.** If you only have a SID, write `sid:ORCL` in the service name box. Set the schema filter: a read-only user's own schema is usually empty. Thin mode cannot connect when the database enforces Oracle Native Network Encryption (error `DPY-3001`); tell us if you hit that. |
| **SQL Server** | `pymssql` | Host, port 1433, database, user, password | SQL logins, or a Windows account written as `DOMAIN\user` with its password (no password-less Windows sign-in). For a named instance write `host\instance` and leave the port empty. Schema filter example: `dbo, sales`. |
| **PostgreSQL** | `psycopg` 3 | Host, port 5432, database, user, password | Empty schema filter = `public` (your default schema). |
| **MySQL / MariaDB** | `PyMySQL` | Host, port 3306, database, user, password | A MySQL "database" is its schema; leave the schema filter empty. |
| **SQLite** | built into Python | Full path of the `.db` file | The file is opened read-only. No user or password. |

**Schema filter:** a comma-separated list such as `SALES, BILLING`. Leave it empty to read the account's
default schema. After a successful **Test connection** the available schemas are listed - click one to
add it.

If a driver could not be installed on a PC, the app still starts; that engine is marked
"driver not installed" in the list.

### Use a read-only database account

The app only reads, but the safest setup is an account that *cannot* write. Ask your DBA for an account
that can only connect and read the tables you need (the test containers of `scripts\live_db.ps1` are
set up exactly like this), for example:

| Engine | A read-only account has |
| --- | --- |
| Oracle | `CREATE SESSION`, and `SELECT` (or `READ`) on the tables or a role that has it. The `ALL_*` catalog views then show exactly those tables. |
| SQL Server | membership of `db_datareader`, plus `VIEW DEFINITION` on the database |
| PostgreSQL | `CONNECT` on the database, `USAGE` on the schema, `SELECT` on its tables (or the `pg_read_all_data` role) |
| MySQL | `SELECT` and `SHOW VIEW` on the database |

## Read-only rules

These hold for every connection the app opens to a database, and the tests check them:

1. **Only reads are sent.** Every statement the app sends - the catalog reads of SQLAlchemy's
   inspector, the profiling reads and the relationship checks - passes a guard (`app/security.py`)
   before it reaches the database. The guard allows `SELECT` / `WITH` queries and catalog reads (`SHOW`,
   `DESCRIBE`, a few read-only SQLite `PRAGMA`s), and blocks everything else: `INSERT`, `UPDATE`,
   `DELETE`, `MERGE`, DDL, `GRANT`, procedure calls, `SELECT ... INTO`, `SELECT ... FOR UPDATE`, and
   more than one statement at a time. **A statement you type in the SQL tab passes a stricter guard
   of its own instead** (`app/sqlguard.py`, see [SQL tab](#sql-tab)); exactly the text that guard
   passed is sent, on the same read-only connection. What is **not seen by the guard** is listed under
   the rules below.
2. **The app never commits.** A commit raises an error. Every use ends with a rollback, then the
   connection is closed (connections are not kept open or pooled).
3. **Read-only session where the engine has one:** `SET TRANSACTION READ ONLY` (Oracle, PostgreSQL),
   `SET SESSION TRANSACTION READ ONLY` (MySQL), read-only file mode plus `PRAGMA query_only` (SQLite).
   **If the engine refuses that setting, the connection is refused** with a message that says so - it
   is never used without read-only mode. SQL Server has no such session mode; there the guard and the
   read-only account do the work.
4. **Statement timeout:** 30 seconds by default (set `DATA_EXPLORER_TIMEOUT` to change it).
5. **Passwords** are held in memory for the session only - never written to disk, never logged, and
   removed from error messages. The key icon on a database source forgets its password at once.

Those session settings are the only non-query statements the app sends; each is matched exactly by the
guard.

**What the guard does not see.** The guard checks the statements the app itself sends. When a
connection opens, the database driver and SQLAlchemy also send a few statements of their own through
raw cursors, before the app's first statement - for example PyMySQL's `SET NAMES` and `SET AUTOCOMMIT`,
pymssql's session options (`SET ARITHABORT ON` ...), and version or settings look-ups such as
`SELECT version()`. These are not seen by the guard. They are fixed by the driver version, set session
options or read server settings, and do not touch your tables; `HANDOFF.md` lists what was observed on
each engine, and the live tests compare each server's own statement log with that list. This is one
more reason to use a read-only database account: it holds whatever the guard cannot see.

## What is stored on the PC

| Where | What |
| --- | --- |
| `data/explorer.db` | Workspaces, their sources and saved connection profiles. For each source: its settings and the *structure* it read (table and column names, types, keys; for a database, the account's default schema). After profiling: row counts and **column statistics** with the time they were read, and your marks of personal columns. After a relationship check: its **results**, and what you decided about suggested links. From the SQL tab: the **texts** of the last 50 queries. **No passwords, never rows of data, never a query result.** |
| `data/uploads/` | Copies of files you **upload**. Giving a path instead reads the files in place, with no copy. Removing the source or the workspace deletes its copies. |
| `data/lineage/` | The Scripts tab's last scan of each workspace: the names and SQL text of the scripts it read, never data. Forget scan or deleting the workspace deletes it. |
| `data/demo/` | The demo data (made up). |

Deleting `data/explorer.db`, `data/uploads/` and `data/lineage/` resets the app. They are ignored by git.
Set the environment variable `DATA_EXPLORER_DATA` to keep them in another folder.

About the saved statistics:

- **Sample rows are never saved.** They are read from the source each time a table is opened.
- Statistics contain values: a column's minimum, maximum and 10 most frequent values. For **personal
  columns these are saved masked only**; histograms are not kept for them at all, and for pay columns
  no top values either.
- The **colour theme** you chose is kept in the browser (its local storage for this address), not in
  these files.
- **Clear saved statistics** (Profile tab) deletes the row counts and statistics of the workspace from
  the file, and with them the relationship-check results and the suggestions nobody decided on.
  Removing a source or a workspace deletes theirs too.
- A relationship check saves numbers, texts and up to 20 **example keys of orphan rows**. Those of a
  personal column are saved masked only. Accepted and rejected links, and the query history, are
  settings of the workspace: they stay until you undo them, clear the history or delete the workspace.
- **Reports and CSV exports are browser downloads.** The app builds them in memory and writes them
  nowhere; they land where your browser saves downloads.
- Excel sheets, and text files that are not UTF-8, are read into memory to be profiled; UTF-8 CSV files
  are read in place. Nothing is copied to disk, and DuckDB's temporary files are switched off.
- The file's layout has a version (4 now). A file written by an earlier version of the app is upgraded
  in place the first time this version opens it; nothing in it is lost.

### Settings

Set these environment variables before `run.bat` to change a default:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_EXPLORER_DATA` | `data` | Folder for `explorer.db`, uploads and the Scripts tab's scans |
| `DATA_EXPLORER_TIMEOUT` | `30` | Statement timeout in seconds, for every statement the app sends |
| `DATA_EXPLORER_SAMPLE_ROWS` | `100000` | A database table with more rows is profiled on a sample of about this many; a smaller one is read whole |
| `DATA_EXPLORER_ORACLE_ROW_SAMPLE_MAX_MB` | `1024` | Oracle: a table up to this size on disk is sampled by rows (even; reads every block once), a bigger one by blocks (cheap; "a few stretches of the table") |
| `DATA_EXPLORER_SAMPLE_THRESHOLD` | `1000000` | A CSV / Excel table with more rows is profiled on a random sample |
| `DATA_EXPLORER_TABLE_BUDGET` | `120` | Seconds one table may take to profile before it is marked "timed out" |
| `DATA_EXPLORER_MEMORY_MB` | `2048` | Memory for the in-memory copies; what does not fit fails instead of using the disk |
| `DATA_EXPLORER_HIGH_NULL` | `0.5` | A column is "mostly empty" above this share of empty rows |
| `DATA_EXPLORER_PERSONAL_RULES` | (the file in the app) | Another copy of `personal_rules.json` to read |
| `DATA_EXPLORER_RELATION_BUDGET` | `60` | Seconds one relationship check may take before it is "not checked (timeout)" |
| `DATA_EXPLORER_RELATION_WHOLE_TABLE` | `1` | `0`: a relationship is never counted inside the database; its keys are always read and looked up |
| `DATA_EXPLORER_RELATION_KEYS` | `5000` | Distinct child keys looked up in the parent per relationship, where the database does not count it itself |
| `DATA_EXPLORER_RELATION_LOOK_PAIRS` | `50000` | Column pairs that get a first look per run of "Find links by values", the likeliest by their names first |
| `DATA_EXPLORER_RELATION_PAIRS` | `200` | Column pairs measured in full per run of "Find links by values": those that share values in the first look |
| `DATA_EXPLORER_RELATION_MIN_OVERLAP` | `0.8` | A pair is suggested from this share of values found |
| `DATA_EXPLORER_SQL_TIMEOUT` | the statement timeout | Seconds a query of the SQL tab may run |
| `DATA_EXPLORER_SQL_FUNCTIONS` | (none) | Extra function names the SQL tab accepts: `name,oracle:other_name` |
| `DATA_EXPLORER_MAX_UPLOAD_MB` | `500` | Largest upload |
| `DATA_EXPLORER_LINEAGE_SOURCE` | `..\SQL_Lineage\sql_lineage` | Where `scripts\sync_lineage.py` copies the SQL Lineage engine from (for maintainers; see MANUAL.md) |

## Demo data

`data/demo/` holds a small telecom-style model: 10 tables (customers, accounts, plans, subscriptions,
devices, invoices, invoice_lines, payments, tickets, branches), about 1,400 made-up rows.

- `telecom.db` - SQLite database with 13 declared foreign keys
- `telecom.sql` - the same tables as DDL, plus six queries with JOINs
- `csv/*.csv` - one export per table

Two links are deliberately not declared, to show the inferred kinds: `payments.account_id` (found from
a JOIN) and `tickets.branch_id` (found from its name). `python -m app.demo --force` rebuilds the files.

## The two command-line scripts

`tools/sql_to_drawio.py` and `tools/sql_to_erd.py` are the original scripts, unchanged:

```bat
.venv\Scripts\python tools\sql_to_drawio.py data\demo\telecom.sql -o telecom.drawio
.venv\Scripts\python tools\sql_to_erd.py data\demo\telecom.sql -o telecom.png
```

The app uses its own copy of their parser, inference, layout and render code in `app/er/`.

## Tests

```bat
.venv\Scripts\python -m pytest
```

The tests marked `live` run against real Oracle, SQL Server, PostgreSQL and MySQL servers and are
skipped when those are not running, so the command above works without Docker. Among them is the
**hostile battery** (`tests/hostile.py`): 331 statements that would write, lock or run code, sent
through the SQL tab's runner as an account that *can* write; every one must be refused and the data,
the sequence, the locks and the log of the functions that are called without brackets must be
unchanged afterwards.

Scripts that ask the test databases directly (they need the containers below):

| Command | What it prints |
| --- | --- |
| `python -m scripts.statement_counts` | how many statements profiling sends to each database, per table |
| `python -m scripts.call_proof oracle` / `postgresql` | for each "call without brackets": what the check says, and whether the server really called the function |
| `python -m scripts.sample_spread` | how even a sample of the 2,000,000-row table is, per engine, over 34 seeds |
| `python -m scripts.demo_report` | saves the demo workspace's report (HTML and Excel) to `data\checks\` to look at by hand; needs no container |
| `python -m scripts.contrast` | the contrast of every colour pair of the page, in the light and the dark theme; needs no container |
| `python -m scripts.reserved_words` | which words each engine does not take for a table or an alias, held against the lists in `app/sqlguard.py`; SQLite and DuckDB need no container (`--list` prints the words) |

`tests/fixtures/zephyr/` holds a small fictional database (Zephyr Outdoor Supply: `schema.sql`,
`seed.sql`, 17 tables) with an answer key that was checked by hand; `tests/test_zephyr.py` builds it as
a temporary SQLite file and checks the app against that key. It also walks all 97 columns through the
SQL tab - by name, under another name, through `SELECT *` - and holds each against the Profile tab, and
sends 75 statements that try to show a personal column some other way. `tests/test_lineage.py` tests
the reading of statements alone (which table column a result column comes from), per engine.

`tests/fixtures/rimal/schema.sql` is the schema of the Rimal Telecom demo database (fictional; 67
tables, 515 columns), a copy of `C:\Projects\Demo_Database\schema.sql`. `tests/test_rimal.py` runs the
name rules over every column of it - 55 are hidden by their names - and each rule of the table under
[Personal data](#personal-data) with cases of its own, in both tabs. Three of its tests read the real
data, `C:\Projects\Demo_Database\out\rimal_telecom_small.db` (read-only), and skip themselves when
that file is not there: with the data the Profile tab hides 57 columns - two number ranges by their
values - and the SQL tab agrees on all 515. `RIMAL_DEMO_DIR` names another folder.

`tests/test_dry_run.py` holds the tests of the round after the demo dry run (13 Oct 2026): row counts and
spread samples on SQLite, links from names and by values, the two diagram exports and the graph's
layout, on small tables made on the spot. Its second half reads the Rimal Telecom demo databases
themselves - `C:\Projects\Demo_Database\out\rimal_telecom_medium.db` (4.5 million rows) and
`rimal_telecom_large.db` (10 million), read-only, with the file's SHA-256 compared before and after -
and skips itself where they are not on the PC. `DRY_RUN_DEMO_DIR` names another folder.

`tests/fixtures/zephyr_scripts/` are SQL Lineage's own test scripts (11 files), copied unchanged.
`tests/test_scripts_api.py` scans them in a workspace whose source is the Zephyr database and requires
the same summary, joins, conflicts, groups and impact as the engine run directly with `--schema` on the
same file. `tests/test_scripts_sync.py` checks that `app\sql_lineage\` equals `C:\Projects\SQL_Lineage\sql_lineage`
byte for byte, and skips itself when that folder is not on the PC. `tests/fixtures/scripts_without_owner/`
are three small Oracle scripts that leave out the owner of their tables; `tests/test_scripts_schemas.py`
finds every table they name in a model whose tables carry a schema, and once more on the Docker Oracle.

Some tests run functions of the page with Node.js - in `tests/test_frontend.py` (among them the graph's
layout with the bundled Cytoscape and dagre: no line through another table's box), `tests/test_dry_run.py`
(the room made for labels) and `tests/test_relations.py` (a share printed like the server prints it) -
and skip themselves when Node.js is not installed.

### Test databases in Docker

`scripts\live_db.ps1` runs local copies of the four server engines in Docker (Docker Desktop is started
if needed), each with the demo telecom schema, a 2,000,000-row table, a schema of awkward cases
(`dx_extra`: orphans, a two-column key, keys that are dates and timestamps, a table without statistics,
large columns, a sequence,
functions that write - on Oracle also ones the read-only account may call, with public synonyms
`X_PING`, `STATUS`, `ONE` and `X_PKG`, which exist nowhere but in this throw-away container; and for
the SQL tab's masking a table of shops with a column `name` and a table of staff with `first_name` and
`salary`; on PostgreSQL also a table `public.x_guests`, for `FROM public.<table>`) and a read-only
account:

```powershell
scripts\live_db.ps1 up        # start the four containers and seed them (first time: a few minutes)
scripts\live_db.ps1 test      # run the live tests
scripts\live_db.ps1 status    # what is running and reachable
scripts\live_db.ps1 stop      # stop them; their data is kept, "start" brings them back in seconds
scripts\live_db.ps1 remove    # delete the containers and their data
scripts\live_db.ps1 images    # the images, their size, and the command to delete them
```

If PowerShell refuses to run the script: `powershell -ExecutionPolicy Bypass -File scripts\live_db.ps1 up`.
The containers listen on `127.0.0.1` only, on their own ports (Oracle 11521, SQL Server 11433,
PostgreSQL 15432, MySQL 13306). You can also point the app at them by hand: the connection settings and
the throw-away passwords are at the top of `scripts\live_engines.py`. Starting the SQL Server container
sets `ACCEPT_EULA=Y`, which accepts Microsoft's licence terms for SQL Server (Developer edition).

## Project layout

```text
run.bat  requirements.txt  requirements-drivers.txt  PLAN.md  HANDOFF.md
app\
  __main__.py     start-up: python -m app [--demo] [--port] [--no-browser]
  main.py         FastAPI app and the /api routes
  security.py     localhost guard, upload limit, the read-only guard for the app's own statements,
                  error messages without values
  sqlguard.py     the strict guard for SQL a person types (the SQL tab)
  jobs.py         background jobs: one per workspace, progress, cancel
  model.py        the common schema model, merging, inference from names
  workspace.py    workspaces, sources and profiles in data\explorer.db (layout version 4)
  export.py       .drawio and .png export
  demo.py         builds the demo data
  sources\        sqlfiles.py, database.py, files.py - one adapter per kind of source; duck.py (DuckDB
                  in memory, locked down)
  er\             tokenizer, parser, inference, layout, renderers (from the two scripts)
  profiling\      queries.py (SQLAlchemy Core), data.py (database / DuckDB readers), profiler.py (the
                  one read per table), personal.py and personal_rules.json (flagging and masking),
                  store.py, quality.py, service.py
  analysis\       relations.py (relationship checks, links by values), sqlrunner.py and
                  sql_functions.json (the SQL tab), lineage.py (which table columns a result column
                  comes from, for masking), report.py (HTML and Excel), scripts.py (the Scripts tab),
                  store.py
  sql_lineage\    the SQL Lineage engine: a copy of C:\Projects\SQL_Lineage\sql_lineage, never edited here
static\           index.html, theme.js (the colour theme, loaded first), app.js, profile.js (Profile,
                  Quality), analysis.js (Relations, SQL, Report), scripts.js (Scripts), styles.css (every
                  colour is a variable with a light and a dark value), vendor\ (Cytoscape, its layouts, Chart.js)
scripts\          live_db.ps1, live_engines.py - the Docker test databases; statement_counts.py,
                  call_proof.py, sample_spread.py, demo_report.py, contrast.py, reserved_words.py (see Tests);
                  sync_lineage.py (copies the SQL Lineage engine into app\sql_lineage\)
tools\            the two original scripts
data\demo\        demo database, .sql file and CSV exports
tests\            pytest; fixtures\zephyr\ (a fictional database with a hand-checked answer key),
                  fixtures\rimal\ (the schema of the Rimal Telecom demo database),
                  fixtures\zephyr_scripts\ (SQL Lineage's test scripts, for the Scripts tab)
```

## Troubleshooting

- **"driver not installed"** next to an engine - run `run.bat` again with internet access, or
  `.venv\Scripts\python -m pip install <driver>`.
- **"Blocked by the read-only guard: ..."** - the app refused to send a statement. It should not happen
  for catalog reads; please report the statement shown in the message.
- **No tables after connecting** - the account's default schema is probably empty. Press
  **Test connection**, pick a schema, and add the source again.
- **The page looks stale after an update** - reload it (Ctrl+F5).
- **"Password needed to refresh"** on a source - the app was restarted, or you pressed the key icon
  ("forget password") on that source; press the refresh arrow and enter the password again. Profiling
  asks for it by itself.
- **"... refused read-only mode, so the connection was closed without being used"** - the database did
  not accept the read-only session setting, and the app does not connect without it. Ask your DBA; the
  message contains the engine's own error.
- **A table shows "structure only, no data to profile"** - it is known from SQL files only. Add the live
  database or the exported files to the same workspace.
- **A column's values stay masked after you un-marked it** - profile the table again; masking what was
  saved cannot be undone.
- **A table says "timed out"** in the Profile tab - its one read and its statistics did not fit in the
  table's time budget (120 seconds), so the table was skipped and nothing was tried again. What an
  earlier profile saved for it is still shown. Raise `DATA_EXPLORER_TABLE_BUDGET`, or lower
  `DATA_EXPLORER_SAMPLE_ROWS` so that less is read.
- **A relationship says "not checked (timeout)"** - the check did not finish within 60 seconds
  (`DATA_EXPLORER_RELATION_BUDGET`). Usually the parent has no index on the key that is looked up.
- **"not checked: tables are in different sources"** - the child and the parent come from two sources of
  the workspace (say a database and a CSV export). A check never compares across sources.
- **SQL tab: "... is not on the list of functions that may be used here"** - only listed built-in
  functions are accepted. See [SQL tab](#sql-tab) for how to add one, and why your own are refused.
- **SQL tab on Oracle: "... is not a column of the tables in this part of the query"**, **"... is the name
  of something that can be called"** or **"rename the alias"** - Oracle calls a function without
  brackets, so every name has to be a column where it stands. The table in
  [Oracle: how names are checked](#oracle-how-names-are-checked) says how to write each case; most
  often: put the table alias in front (`t.name`), or add the table's schema to the source.
- **SQL tab: "The SQL tab cannot run queries on this Oracle source yet"** - the list of what the
  database can call was not read (the source was added by an earlier version, or the read failed: the
  message says why). Press the source's refresh arrow. The account needs to see `ALL_OBJECTS`,
  `ALL_SYNONYMS` and `ALL_USERS`, which every Oracle account does; if the read times out, raise
  `DATA_EXPLORER_TIMEOUT`.
- **SQL tab on PostgreSQL: "... is not a column of ... as this source knows it"** - `t.name` calls a
  function `name(t)` there when `t` has no such column. See [PostgreSQL: table.name](#postgresql-tablename).
- **SQL tab: "This result is no longer in memory"** when exporting - run the query again; results are
  kept in memory only, a few at a time.
