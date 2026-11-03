-- Demo telecom model for Data Explorer.
-- Part 1: tables with their primary and foreign keys.
-- Part 2: a few queries; their JOINs reveal relationships that are not declared.
-- All data in the matching telecom.db and csv/ files is made up.

-- ===== Part 1: tables =====

CREATE TABLE branches (
    branch_id      INTEGER PRIMARY KEY,
    branch_name    VARCHAR(80) NOT NULL,
    city           VARCHAR(40) NOT NULL,
    region         VARCHAR(20) NOT NULL,
    opened_date    DATE
);

CREATE TABLE customers (
    customer_id    INTEGER PRIMARY KEY,
    full_name      VARCHAR(120) NOT NULL,
    national_id    VARCHAR(10) NOT NULL,
    email          VARCHAR(120),
    phone          VARCHAR(15),
    city           VARCHAR(40),
    segment        VARCHAR(20) NOT NULL,
    branch_id      INTEGER REFERENCES branches (branch_id),
    created_at     DATE NOT NULL
);

CREATE TABLE accounts (
    account_id         INTEGER PRIMARY KEY,
    customer_id        INTEGER NOT NULL,
    parent_account_id  INTEGER,
    account_number     VARCHAR(12) NOT NULL UNIQUE,
    account_type       VARCHAR(12) NOT NULL,
    status             VARCHAR(12) NOT NULL,
    opened_date        DATE NOT NULL,
    credit_limit       DECIMAL(10,2),
    CONSTRAINT fk_accounts_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_accounts_parent FOREIGN KEY (parent_account_id) REFERENCES accounts (account_id)
);

CREATE TABLE plans (
    plan_id        INTEGER PRIMARY KEY,
    plan_name      VARCHAR(60) NOT NULL,
    plan_type      VARCHAR(10) NOT NULL,
    monthly_fee    DECIMAL(8,2) NOT NULL,
    data_gb        INTEGER,
    voice_minutes  INTEGER,
    is_active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE subscriptions (
    subscription_id  INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts (account_id),
    plan_id          INTEGER NOT NULL REFERENCES plans (plan_id),
    msisdn           VARCHAR(12) NOT NULL,
    start_date       DATE NOT NULL,
    end_date         DATE,
    status           VARCHAR(12) NOT NULL
);

CREATE TABLE devices (
    device_id        INTEGER PRIMARY KEY,
    subscription_id  INTEGER NOT NULL REFERENCES subscriptions (subscription_id),
    imei             VARCHAR(15) NOT NULL,
    brand            VARCHAR(30),
    model            VARCHAR(40),
    activated_date   DATE
);

CREATE TABLE invoices (
    invoice_id     INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL,
    invoice_date   DATE NOT NULL,
    due_date       DATE NOT NULL,
    total_amount   DECIMAL(10,2) NOT NULL,
    status         VARCHAR(10) NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts (account_id)
);

CREATE TABLE invoice_lines (
    invoice_line_id  INTEGER PRIMARY KEY,
    invoice_id       INTEGER NOT NULL REFERENCES invoices (invoice_id),
    subscription_id  INTEGER REFERENCES subscriptions (subscription_id),
    description      VARCHAR(120) NOT NULL,
    quantity         INTEGER NOT NULL,
    amount           DECIMAL(10,2) NOT NULL
);

-- payments.account_id is copied from the invoice; no foreign key is declared for it
CREATE TABLE payments (
    payment_id     INTEGER PRIMARY KEY,
    invoice_id     INTEGER NOT NULL REFERENCES invoices (invoice_id),
    account_id     INTEGER NOT NULL,
    branch_id      INTEGER REFERENCES branches (branch_id),
    payment_date   DATE NOT NULL,
    amount         DECIMAL(10,2) NOT NULL,
    method         VARCHAR(15) NOT NULL
);

-- tickets.branch_id is the handling branch; no foreign key is declared for it
CREATE TABLE tickets (
    ticket_id        INTEGER PRIMARY KEY,
    customer_id      INTEGER NOT NULL REFERENCES customers (customer_id),
    subscription_id  INTEGER REFERENCES subscriptions (subscription_id),
    branch_id        INTEGER,
    opened_at        TIMESTAMP NOT NULL,
    closed_at        TIMESTAMP,
    category         VARCHAR(20) NOT NULL,
    priority         VARCHAR(8) NOT NULL,
    status           VARCHAR(12) NOT NULL
);

-- ===== Part 2: queries =====

-- Revenue per plan
SELECT p.plan_name, COUNT(DISTINCT s.subscription_id) AS subscription_count, SUM(il.amount) AS revenue
FROM invoice_lines il
JOIN subscriptions s ON s.subscription_id = il.subscription_id
JOIN plans p ON p.plan_id = s.plan_id
GROUP BY p.plan_name
ORDER BY revenue DESC;

-- Overdue invoices with the customer and their branch
SELECT c.full_name, b.branch_name, i.invoice_id, i.due_date, i.total_amount
FROM invoices i
JOIN accounts a ON a.account_id = i.account_id
JOIN customers c ON c.customer_id = a.customer_id
LEFT JOIN branches b ON b.branch_id = c.branch_id
WHERE i.status = 'overdue';

-- Amount paid per account (payments.account_id has no declared foreign key)
SELECT a.account_number, SUM(p.amount) AS paid
FROM payments p
JOIN accounts a ON a.account_id = p.account_id
GROUP BY a.account_number;

-- Open tickets of customers who have a suspended account (matched on customer_id)
SELECT t.ticket_id, t.category, a.account_number
FROM tickets t
JOIN accounts a ON a.customer_id = t.customer_id
WHERE t.status = 'open' AND a.status = 'suspended';

-- Devices on active subscriptions
SELECT d.imei, d.brand, d.model
FROM devices d
WHERE d.subscription_id IN (SELECT s.subscription_id FROM subscriptions s WHERE s.status = 'active');

-- Billed and paid per customer
WITH billed AS (
    SELECT a.customer_id, SUM(i.total_amount) AS billed
    FROM invoices i
    JOIN accounts a ON a.account_id = i.account_id
    GROUP BY a.customer_id
), paid AS (
    SELECT a.customer_id, SUM(p.amount) AS paid
    FROM payments p
    JOIN invoices i ON i.invoice_id = p.invoice_id
    JOIN accounts a ON a.account_id = i.account_id
    GROUP BY a.customer_id
)
SELECT c.customer_id, c.full_name, bl.billed, COALESCE(pd.paid, 0) AS paid
FROM customers c
JOIN billed bl ON bl.customer_id = c.customer_id
LEFT JOIN paid pd ON pd.customer_id = c.customer_id;
