"""Order line report. Reads with pandas. The lineage tool reads this file, never runs it."""
import pandas as pd

LINES_SQL = """
SELECT o.order_id, o.order_date, c.last_name, oi.line_no, oi.quantity
FROM sales_order o
JOIN customer c ON c.customer_id = o.customer_id
JOIN order_item oi ON oi.order_id = o.order_id
"""


def load(con, start, table_name):
    lines = pd.read_sql(LINES_SQL, con)
    products = pd.read_sql(
        "SELECT oi.order_id, p.name, cat.name AS category "
        "FROM order_item oi "
        "JOIN product p ON p.product_id = oi.product_id "
        "JOIN category cat ON cat.category_id = p.category_id",
        con,
    )
    recent = con.execute(
        f"SELECT order_id, status FROM sales_order WHERE order_date >= '{start}'"
    ).fetchall()
    counts = con.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    print("Update finished")
    return lines, products, recent, counts
