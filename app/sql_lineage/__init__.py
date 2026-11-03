"""SQL Lineage: reads a folder of .sql and .py scripts and builds a graph of the
tables, columns and joins they use. It never runs a script and never connects to
a database. See README.md for the commands and PLAN.md for the design.

The work happens in four steps, one subpackage each:

    extract   folder -> scripts and statements (text with line numbers)
    parse     statement text -> tables, columns, joins and unknowns (sqlglot)
    graph     facts -> nodes and edges, saved in lineage.db
    analysis  graph -> join catalog, conflicts, repeated groups, impact
    output    graph and analysis -> report.html, lineage.json, Neo4j export
"""

__version__ = "0.1.0"
