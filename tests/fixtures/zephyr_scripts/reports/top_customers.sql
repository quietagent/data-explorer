-- Level 4 of the chain: reads the report table.
SELECT r.customer_id, c.email, r.lifetime_value
FROM report_top_customers r
JOIN customer c ON c.customer_id = r.customer_id
ORDER BY r.lifetime_value DESC;
