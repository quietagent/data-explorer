-- What was paid on each invoice: an Oracle (+) outer join and NVL, the owner left out.
SELECT a.account_number, i.invoice_id, i.total_amount, NVL(SUM(p.amount), 0) AS paid
FROM accounts a, invoices i, payments p
WHERE i.account_id = a.account_id
  AND p.invoice_id (+) = i.invoice_id
GROUP BY a.account_number, i.invoice_id, i.total_amount;
