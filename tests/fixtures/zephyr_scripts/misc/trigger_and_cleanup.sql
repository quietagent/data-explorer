-- A trigger body is kept whole and listed as not read yet.
CREATE TRIGGER trg_order_audit AFTER INSERT ON sales_order
BEGIN
    INSERT INTO order_audit (order_id, changed_on) VALUES (NEW.order_id, DATE('now'));
END;

CREATE INDEX idx_order_date ON sales_order (order_date);

DELETE FROM payment
WHERE order_id IN (SELECT order_id FROM sales_order WHERE status = 'Cancelled');
