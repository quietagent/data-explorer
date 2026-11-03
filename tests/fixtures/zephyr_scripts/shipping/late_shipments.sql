-- Statement 1: the usual shipment join.
SELECT s.tracking_number, o.order_date
FROM shipment s
JOIN sales_order o ON o.order_id = s.order_id
WHERE s.delivered_on IS NULL;

-- Statement 2: joins on the wrong column, a conflict with statement 1.
SELECT s.carrier, o.status
FROM sales_order o
INNER JOIN shipment s ON s.shipment_id = o.order_id;

-- Statement 3: a date-range join, not on a key.
SELECT p.payment_id, s.shipment_id
FROM payment p
JOIN shipment s ON p.paid_on BETWEEN s.shipped_on AND s.delivered_on;
