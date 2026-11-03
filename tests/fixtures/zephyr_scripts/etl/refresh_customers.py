import sqlite3


def refresh(con, cities):
    cur = con.cursor()
    sql = """
        UPDATE customer
        SET city = TRIM(city)
        WHERE city LIKE ' %'
    """
    cur.execute(sql)

    sql = "DELETE FROM loyalty_card WHERE points = 0"
    cur.execute(sql)

    # Level 3 of the chain: a report table fed from the second mart.
    cur.execute("""
        INSERT INTO report_top_customers (customer_id, lifetime_value)
        SELECT customer_id, lifetime_value
        FROM mart_customer_value
        WHERE lifetime_value > 1000
    """)

    query = "SELECT customer_id FROM customer WHERE 1 = 1"
    for city in cities:
        query += " AND city = ?"
    cur.execute(query, cities)
    print("Update finished")
    con.commit()


if __name__ == "__main__":
    refresh(sqlite3.connect("zephyr.db"), ["Riyadh"])
