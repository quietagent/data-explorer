"""Data profiling and data-quality checks.

    queries.py    every statement, built with SQLAlchemy Core: quoted identifiers, bound values,
                  each engine's own sampling clause (SAMPLE BLOCK / TABLESAMPLE / LIMIT)
    data.py       DatabaseData (ReadOnlyDatabase.connect(), so the guard, the read-only session and
                  the timeout), SampleData (the in-memory copy of a table's one read) and FileData
                  (a file in memory)
    profiler.py   read_table(): the ONE read of a table or of a sample of it; profile_copy(): row
                  count, column statistics, top values, histograms - all from the copy;
                  mask_result(); sample_rows() and live_values() for what is never stored
    personal.py   which columns look personal (by name, by values) and how values are masked.
                  The rules themselves are data: personal_rules.json
    store.py      saved results in data/explorer.db (never rows of data)
    quality.py    data-quality findings, worked out from the saved statistics
    service.py    Profiling: background jobs, the per-table API answers, marks, the
                  show-personal switch (memory only)

This package's __init__ imports nothing: app.workspace imports store.py for the layout, and
service.py imports app.workspace.

app.analysis builds on: service.Profiling.targets() (which source a merged table's data is read
from, with that source's own table and column names), profiler.read_table() (the same sampling),
data.DatabaseData.run() (one guarded statement at a time) and data.FileData.
"""
