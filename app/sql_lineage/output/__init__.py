"""Output step: every file a scan writes besides lineage.db.

    report.py    report.html, one offline page (report_template.html + Cytoscape)
    json_out.py  lineage.json, the whole graph plus the analysis results
    neo4j.py     neo4j/ with nodes.csv, edges.csv, load.cypher and README.txt

build_report(graph, result) returns report.html as a string, without writing
a file (Data Explorer sends it as a download).
"""

from sql_lineage.output.json_out import write_json
from sql_lineage.output.neo4j import write_neo4j
from sql_lineage.output.report import build_report, write_report

__all__ = ["write_all", "build_report", "write_report", "write_json", "write_neo4j"]


def write_all(graph, result, out):
    """Write every output file into folder `out`. Returns the names written."""
    write_report(graph, result, out / "report.html")
    write_json(graph, result, out / "lineage.json")
    write_neo4j(graph, out / "neo4j")
    return ["report.html", "lineage.json", "neo4j/"]
