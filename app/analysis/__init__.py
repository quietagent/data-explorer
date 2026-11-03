"""Session 3: is the model true in the data, a query of your own, and a report. What is where:

    relations.py        Relations: check every relationship on the data (coverage, orphan rows, empty
                        keys, real cardinality) and look for links by value overlap; accept / reject
    sqlrunner.py        SqlRunner: run one read-only statement typed by a person against a database or
                        a files source; mask the result; CSV; the query history
    lineage.py          origins(): which columns of which tables each column of a result comes from, so
                        that the SQL tab masks by table as the Profile tab does. Reads the statement
                        with the guard's own reader; what it cannot follow for sure is "not known"
    sql_functions.json  the functions such a statement may call, per engine (everything else is refused)
    report.py           Report: one self-contained HTML file or an Excel workbook, always masked
    store.py            AnalysisStore: what all of this saves in data/explorer.db (layout version 3) -
                        check results, decisions about suggested links, query texts. Never a result

What they build on:

    app.sqlguard                        the strict guard for typed SQL (security.check_read_only_sql(strict=True))
    app.jobs                            one background job per workspace, shared with profiling
    app.profiling.profiler.read_table   the one sampled read of a table, used for the child of a relationship
    app.profiling.service.Profiling     targets (which source holds a table's data), marks of personal columns,
                                        masked_columns() for the report
    app.model.RANK['values']            the kind of an accepted link

This module imports nothing, so that importing app.analysis costs nothing.
"""
