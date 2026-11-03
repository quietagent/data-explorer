-- ﬂ «·ÊÃ «·„‰ Ã« : Â–« «·„·› „Õ›ÊŸ » —„Ì“ Windows-1256
-- Product catalog. This file is saved in Windows-1256.

-- Statement 1: a schema-qualified and a quoted table name.
SELECT cat.name AS category, p.sku, SUM(oi.quantity) AS sold
FROM main.order_item oi
JOIN "product" p ON p.product_id = oi.product_id
JOIN category cat ON cat.category_id = p.category_id
GROUP BY cat.name, p.sku;

-- Statement 2: USING, and a subquery in FROM followed back to its table.
SELECT ps.supplier_id, s.company_name, cheap.sku
FROM product_supplier ps
JOIN supplier s USING (supplier_id)
JOIN (SELECT product_id, sku FROM product WHERE unit_price < 20) cheap
  ON cheap.product_id = ps.product_id;
