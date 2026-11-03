-- تقرير المبيعات حسب العميل
-- Sales by customer. The Arabic comment above must not break reading.

-- Statement 1: explicit JOIN ... ON with aliases; group and order roles.
SELECT c.customer_id, c.last_name, SUM(oi.quantity * oi.unit_price) AS total
FROM customer c
JOIN sales_order o ON o.customer_id = c.customer_id
JOIN order_item oi ON oi.order_id = o.order_id
WHERE o.status = 'Delivered'
GROUP BY c.customer_id, c.last_name
ORDER BY total DESC;

/* Statement 2: comma joins, with the join conditions in WHERE.
   SELECT * FROM payment;  -- commented out, so not a statement */
SELECT c.email, o.order_date, oi.product_id
FROM customer c, sales_order o, order_item oi
WHERE o.customer_id = c.customer_id
  AND oi.order_id = o.order_id
  AND c.city = 'Riyadh; Jeddah';   -- a ; inside a string

-- SELECT * FROM customer WHERE 1 = 0;
