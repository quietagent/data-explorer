"""Demo data: a small telecom-style model, so the app can be tried without company access.

    python -m app.demo            build data/demo/ (only what is missing)
    python -m app.demo --force    rebuild everything

Produces, from one definition:
    data/demo/telecom.db      SQLite database with declared keys and a few hundred rows per table
    data/demo/telecom.sql     the same model as DDL, plus a few JOIN queries
    data/demo/csv/*.csv       one export per table

All names, numbers and addresses are made up. The data is generated from a
fixed seed, so every build gives the same rows.

Two relationships are deliberately NOT declared as foreign keys, to show the
inferred kinds: payments.account_id (found from a JOIN in telecom.sql) and
tickets.branch_id (found from the column name).
"""

from __future__ import annotations

import csv
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

DEMO_WORKSPACE_ID = 'demo'
DEMO_WORKSPACE_NAME = 'Demo - telecom'
TABLES = ['branches', 'customers', 'accounts', 'plans', 'subscriptions', 'devices',
          'invoices', 'invoice_lines', 'payments', 'tickets']

DDL = """\
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
"""

QUERIES = """\
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
"""

SQL_HEADER = """\
-- Demo telecom model for Data Explorer.
-- Part 1: tables with their primary and foreign keys.
-- Part 2: a few queries; their JOINs reveal relationships that are not declared.
-- All data in the matching telecom.db and csv/ files is made up.

"""

FIRST = ['Ahmed', 'Mohammed', 'Abdullah', 'Khalid', 'Faisal', 'Saud', 'Omar', 'Yousef', 'Ibrahim', 'Hassan',
         'Sultan', 'Nasser', 'Waleed', 'Majed', 'Bandar', 'Fahad', 'Noura', 'Sara', 'Fatimah', 'Aisha',
         'Maha', 'Reem', 'Lama', 'Hind', 'Dana', 'Layla', 'Mona', 'Huda', 'Rana', 'Amal']
LAST = ['Al-Harbi', 'Al-Otaibi', 'Al-Qahtani', 'Al-Ghamdi', 'Al-Zahrani', 'Al-Shehri', 'Al-Dossari',
        'Al-Mutairi', 'Al-Anazi', 'Al-Shammari', 'Al-Subaie', 'Al-Maliki', 'Al-Amri', 'Al-Juhani',
        'Al-Rashid', 'Al-Tamimi', 'Al-Hazmi', 'Al-Saleh', 'Al-Khalifa', 'Al-Najjar']
CITIES = [('Riyadh', 'Central'), ('Jeddah', 'Western'), ('Dammam', 'Eastern'), ('Khobar', 'Eastern'),
          ('Mecca', 'Western'), ('Medina', 'Western'), ('Abha', 'Southern'), ('Tabuk', 'Northern')]
BRANCHES = [('Riyadh Olaya', 'Riyadh'), ('Riyadh Malaz', 'Riyadh'), ('Jeddah Corniche', 'Jeddah'),
            ('Jeddah Tahlia', 'Jeddah'), ('Dammam Centre', 'Dammam'), ('Khobar Mall', 'Khobar'),
            ('Mecca Aziziyah', 'Mecca'), ('Medina Quba', 'Medina')]
PLANS = [('Prepaid Basic', 'prepaid', 35, 5, 100), ('Prepaid Plus', 'prepaid', 65, 15, 300),
         ('Prepaid Data 30', 'prepaid', 99, 30, None), ('Postpaid 100', 'postpaid', 100, 20, 500),
         ('Postpaid 200', 'postpaid', 200, 60, 1500), ('Postpaid 400', 'postpaid', 400, 150, None),
         ('Family Share', 'postpaid', 320, 120, 3000), ('Business Team', 'postpaid', 250, 80, 2500),
         ('Business Unlimited', 'postpaid', 550, None, None), ('Data SIM 50', 'postpaid', 150, 50, 0)]
DEVICES = [('Apple', ['iPhone 13', 'iPhone 14', 'iPhone 15', 'iPhone 15 Pro']),
           ('Samsung', ['Galaxy A54', 'Galaxy S23', 'Galaxy S24', 'Galaxy Z Flip5']),
           ('Huawei', ['Nova 11', 'P60 Pro', '5G Router H138']), ('Xiaomi', ['Redmi Note 13', 'Xiaomi 13T']),
           ('Nokia', ['G42', 'C32'])]
LINE_ITEMS = [('Monthly plan fee', 1.0), ('Data add-on 10 GB', 0.3), ('International calls', 0.25),
              ('Roaming package', 0.4), ('Device instalment', 0.6), ('Late payment fee', 0.1)]


def _generate(rng):
    """Rows for every table, as {table: (column names, rows)}. Keys always point at real rows."""
    digits = lambda n: ''.join(rng.choice('0123456789') for _ in range(n))
    between = lambda a, b: a + timedelta(days=rng.randrange((b - a).days + 1))
    region = dict(CITIES)
    end = date(2026, 9, 30)
    data = {}

    branches = [(i, name, city, region[city], between(date(2012, 1, 1), date(2022, 12, 31)))
                for i, (name, city) in enumerate(BRANCHES, 1)]
    data['branches'] = (['branch_id', 'branch_name', 'city', 'region', 'opened_date'], branches)
    by_city = {}
    for b in branches:
        by_city.setdefault(b[2], []).append(b[0])

    customers, used = [], set()
    for i in range(1, 81):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        nid = None
        while nid is None or nid in used:
            nid = rng.choice('12') + digits(9)
        used.add(nid)
        city = rng.choice(CITIES)[0]
        email = None if rng.random() < 0.1 else f'{first}.{last.replace("Al-", "")}{i}@example.com'.lower()
        phone = None if rng.random() < 0.04 else '9665' + digits(8)
        branch = None if rng.random() < 0.05 else rng.choice(by_city.get(city) or [b[0] for b in branches])
        segment = rng.choices(['consumer', 'business', 'vip'], [0.72, 0.2, 0.08])[0]
        customers.append((i, f'{first} {last}', nid, email, phone, city, segment, branch,
                          between(date(2019, 1, 1), date(2025, 6, 30))))
    data['customers'] = (['customer_id', 'full_name', 'national_id', 'email', 'phone', 'city', 'segment',
                          'branch_id', 'created_at'], customers)

    accounts, numbers = [], set()
    owners = [c[0] for c in customers] + [rng.choice(customers)[0] for _ in range(20)]
    main_account = {}
    for i, cust in enumerate(owners, 1):
        c = customers[cust - 1]
        number = None
        while number is None or number in numbers:
            number = 'AC' + digits(8)
        numbers.add(number)
        kind = 'corporate' if c[6] == 'business' else rng.choices(['postpaid', 'prepaid'], [0.6, 0.4])[0]
        parent = main_account.get(cust) if kind == 'corporate' else None
        main_account.setdefault(cust, i)
        status = rng.choices(['active', 'suspended', 'closed'], [0.8, 0.12, 0.08])[0]
        limit = None if kind == 'prepaid' else float(rng.choice([500, 1000, 2000, 5000, 10000]))
        accounts.append((i, cust, parent, number, kind, status, between(c[8], date(2025, 9, 30)), limit))
    data['accounts'] = (['account_id', 'customer_id', 'parent_account_id', 'account_number', 'account_type',
                         'status', 'opened_date', 'credit_limit'], accounts)

    plans = [(i, n, t, float(fee), gb, minutes, 0 if i == 3 else 1)
             for i, (n, t, fee, gb, minutes) in enumerate(PLANS, 1)]
    data['plans'] = (['plan_id', 'plan_name', 'plan_type', 'monthly_fee', 'data_gb', 'voice_minutes',
                      'is_active'], plans)

    subscriptions, msisdns, subs_of = [], set(), {}
    open_accounts = [a for a in accounts if a[5] != 'closed']
    for i in range(1, 151):
        a = rng.choice(open_accounts)
        wanted = 'prepaid' if a[4] == 'prepaid' else 'postpaid'
        plan = rng.choice([p for p in plans if p[2] == wanted])
        msisdn = None
        while msisdn is None or msisdn in msisdns:
            msisdn = '9665' + digits(8)
        msisdns.add(msisdn)
        start = between(a[6], date(2026, 6, 30))
        status = rng.choices(['active', 'suspended', 'terminated'], [0.76, 0.09, 0.15])[0]
        stop = between(start, end) if status == 'terminated' else None
        subscriptions.append((i, a[0], plan[0], msisdn, start, stop, status))
        subs_of.setdefault(a[0], []).append(i)
    data['subscriptions'] = (['subscription_id', 'account_id', 'plan_id', 'msisdn', 'start_date', 'end_date',
                              'status'], subscriptions)

    devices = []
    for i, s in enumerate(rng.sample(subscriptions, 110), 1):
        brand, models = rng.choice(DEVICES)
        known = rng.random() > 0.06
        devices.append((i, s[0], digits(15), brand if known else None, rng.choice(models) if known else None,
                        between(s[4], end) if rng.random() > 0.1 else None))
    data['devices'] = (['device_id', 'subscription_id', 'imei', 'brand', 'model', 'activated_date'], devices)

    invoices, lines, payments = [], [], []
    plan_fee = {p[0]: p[3] for p in plans}
    sub_plan = {s[0]: s[2] for s in subscriptions}
    billable = [a for a in accounts if subs_of.get(a[0])]
    for i in range(1, 201):
        a = rng.choice(billable)
        issued = date(2025 + rng.randrange(2), 1, 1)
        issued = issued.replace(month=rng.randrange(1, 13 if issued.year == 2025 else 10))
        total = 0.0
        items = [LINE_ITEMS[0]] + rng.sample(LINE_ITEMS[1:], rng.randrange(0, 4))
        for n, (text, share) in enumerate(items):
            sub = rng.choice(subs_of[a[0]]) if text != 'Late payment fee' else None
            fee = plan_fee[sub_plan[sub]] if sub else 100.0
            qty = 1 if n == 0 else rng.randrange(1, 4)
            amount = round(fee * share * qty, 2)
            lines.append((len(lines) + 1, i, sub, text, qty, amount))
            total += amount
        total = round(total, 2)
        status = rng.choices(['paid', 'unpaid', 'overdue'], [0.7, 0.18, 0.12])[0]
        invoices.append((i, a[0], issued, issued + timedelta(days=21), total, status))
        if status == 'paid' or rng.random() < 0.12:
            parts = [total] if status == 'paid' and rng.random() < 0.85 else [round(total * 0.5, 2)] * (
                2 if status == 'paid' else 1)
            for amount in parts:
                method = rng.choices(['card', 'bank_transfer', 'wallet', 'cash'], [0.45, 0.2, 0.2, 0.15])[0]
                payments.append((len(payments) + 1, i, a[0],
                                 rng.choice(branches)[0] if method == 'cash' else None,
                                 issued + timedelta(days=rng.randrange(1, 30)), amount, method))
    data['invoices'] = (['invoice_id', 'account_id', 'invoice_date', 'due_date', 'total_amount', 'status'],
                        invoices)
    data['invoice_lines'] = (['invoice_line_id', 'invoice_id', 'subscription_id', 'description', 'quantity',
                              'amount'], lines)
    data['payments'] = (['payment_id', 'invoice_id', 'account_id', 'branch_id', 'payment_date', 'amount',
                         'method'], payments)

    tickets = []
    accounts_of = {}
    for a in accounts:
        accounts_of.setdefault(a[1], []).append(a[0])
    for i in range(1, 91):
        c = rng.choice(customers)
        own = [s for acc in accounts_of.get(c[0], []) for s in subs_of.get(acc, [])]
        sub = rng.choice(own) if own and rng.random() > 0.3 else None
        opened = datetime(2025, 1, 1) + timedelta(minutes=rng.randrange(0, 60 * 24 * 630))
        status = rng.choices(['closed', 'open', 'in_progress'], [0.7, 0.18, 0.12])[0]
        closed = opened + timedelta(hours=rng.randrange(1, 240)) if status == 'closed' else None
        tickets.append((i, c[0], sub, rng.choice(branches)[0] if rng.random() > 0.4 else None, opened, closed,
                        rng.choice(['billing', 'network', 'device', 'plan_change', 'complaint']),
                        rng.choices(['low', 'medium', 'high'], [0.4, 0.42, 0.18])[0], status))
    data['tickets'] = (['ticket_id', 'customer_id', 'subscription_id', 'branch_id', 'opened_at', 'closed_at',
                        'category', 'priority', 'status'], tickets)
    return data


def _text(value):
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    return value


def build_demo(demo_dir=None, force=False):
    """Create the demo files if they are missing (or always, with force). Returns the folder."""
    demo_dir = Path(demo_dir or config.DEMO_DIR)
    db_path, sql_path, csv_dir = demo_dir / 'telecom.db', demo_dir / 'telecom.sql', demo_dir / 'csv'
    complete = db_path.is_file() and sql_path.is_file() and all((csv_dir / f'{t}.csv').is_file() for t in TABLES)
    if complete and not force:
        return demo_dir
    csv_dir.mkdir(parents=True, exist_ok=True)
    data = _generate(random.Random(20261012))

    sql_path.write_text(SQL_HEADER + '-- ===== Part 1: tables =====\n\n' + DDL
                        + '\n-- ===== Part 2: queries =====\n\n' + QUERIES, encoding='utf-8', newline='\n')

    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.execute('PRAGMA foreign_keys = ON')
        con.executescript(DDL)
        for table in TABLES:
            columns, rows = data[table]
            marks = ', '.join('?' for _ in columns)
            con.executemany(f'INSERT INTO {table} ({", ".join(columns)}) VALUES ({marks})',
                            [[_text(v) for v in row] for row in rows])
        con.commit()
        con.execute('VACUUM')
    finally:
        con.close()

    for table in TABLES:
        columns, rows = data[table]
        with open(csv_dir / f'{table}.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            writer.writerows([['' if v is None else _text(v) for v in row] for row in rows])
    return demo_dir


if __name__ == '__main__':
    folder = build_demo(force='--force' in sys.argv)
    print(f'Demo data is in {folder}')
