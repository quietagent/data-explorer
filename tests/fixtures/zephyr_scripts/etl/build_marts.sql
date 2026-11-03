-- Level 1 of the chain: a mart table built from the order tables.
DROP TABLE IF EXISTS mart_order_totals;

CREATE TABLE mart_order_totals AS
SELECT o.order_id, o.customer_id, SUM(oi.quantity * oi.unit_price) AS order_total
FROM sales_order o
JOIN order_item oi ON oi.order_id = o.order_id
GROUP BY o.order_id, o.customer_id;

-- Level 2: a second mart fed by the first.
CREATE TABLE IF NOT EXISTS mart_customer_value (customer_id INTEGER, email TEXT, lifetime_value REAL);

INSERT INTO mart_customer_value (customer_id, email, lifetime_value)
SELECT c.customer_id, c.email, SUM(t.order_total)
FROM mart_order_totals t
JOIN customer c ON c.customer_id = t.customer_id
GROUP BY c.customer_id, c.email;
