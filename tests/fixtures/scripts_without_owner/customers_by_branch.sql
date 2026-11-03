-- Customers per branch. The tables are named without their owner, as scripts usually do.
SELECT b.branch_name, COUNT(*) AS customers
FROM customers c
JOIN branches b ON b.branch_id = c.branch_id
GROUP BY b.branch_name
ORDER BY customers DESC;
