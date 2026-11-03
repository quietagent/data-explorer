from sqlalchemy import text

STOCK_BY_WAREHOUSE = text(
    "SELECT w.name, p.sku, i.quantity_on_hand "
    "FROM inventory i "
    "JOIN warehouse w ON w.warehouse_id = i.warehouse_id "
    "JOIN product p ON p.product_id = i.product_id"
)


def low_stock(engine, level):
    with engine.connect() as con:
        rows = con.execute(STOCK_BY_WAREHOUSE).fetchall()
        low = con.execute(text(
            "SELECT w.city, p.name FROM inventory i "
            "JOIN warehouse w ON w.warehouse_id = i.warehouse_id "
            "JOIN product p ON p.product_id = i.product_id "
            "WHERE i.quantity_on_hand < :level"), {"level": level}).fetchall()
        counted = con.execute(text("""
            SELECT i.last_counted_on, w.capacity_m3, p.unit_price
            FROM inventory i, warehouse w, product p
            WHERE w.warehouse_id = i.warehouse_id
              AND p.product_id = i.product_id
        """)).fetchall()
        con.executemany("UPDATE inventory SET reorder_level = ? WHERE product_id = ?", [])
    return rows, low, counted
