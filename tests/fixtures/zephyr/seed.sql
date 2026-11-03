-- Zephyr Outdoor Supply Co. (fictional) -- sample data
-- Load after schema.sql. Insert order follows the foreign keys.

PRAGMA foreign_keys = ON;

-- Managers are filled in after the employees exist.
INSERT INTO department (department_id, name, budget) VALUES
    (1, 'Executive',        500000.00),
    (2, 'Sales',           1200000.00),
    (3, 'Human Resources',  300000.00),
    (4, 'Logistics',        850000.00),
    (5, 'IT',               600000.00),
    (6, 'Marketing',        450000.00);

INSERT INTO store (store_id, name, street, city, postal_code, phone, opened_on) VALUES
    (1, 'Zephyr Flagship',       '12 Harbor Street',  'Portland', '97201', '555-0101', '2015-03-14'),
    (2, 'Zephyr Mountain View',  '480 Summit Road',   'Denver',   '80202', '555-0102', '2017-06-01'),
    (3, 'Zephyr Lakeside',       '77 Shoreline Ave',  'Chicago',  '60611', '555-0103', '2019-09-20'),
    (4, 'Zephyr Desert Outpost', '9 Canyon Way',      'Phoenix',  '85004', '555-0104', '2023-11-05');

INSERT INTO employee
    (employee_id, first_name, last_name, email, birth_date, hire_date, job_title, salary, department_id, store_id, supervisor_id)
VALUES
    (1,  'Marisol', 'Okafor',    'marisol.okafor@zephyr-outdoor.example',   '1975-04-12', '2015-01-05', 'Chief Executive Officer', 185000.00, 1, NULL, NULL),
    (2,  'Dmitri',  'Vance',     'dmitri.vance@zephyr-outdoor.example',     '1980-09-30', '2015-02-10', 'Sales Director',          112000.00, 2, NULL, 1),
    (3,  'Priya',   'Raman',     'priya.raman@zephyr-outdoor.example',      '1984-01-22', '2016-05-16', 'HR Manager',               88000.00, 3, NULL, 1),
    (4,  'Tobias',  'Lindqvist', 'tobias.lindqvist@zephyr-outdoor.example', '1979-11-03', '2015-08-01', 'Logistics Manager',        95000.00, 4, NULL, 1),
    (5,  'Hana',    'Kobayashi', 'hana.kobayashi@zephyr-outdoor.example',   '1988-07-19', '2018-03-12', 'IT Manager',              105000.00, 5, NULL, 1),
    (6,  'Caleb',   'Whitmore',  'caleb.whitmore@zephyr-outdoor.example',   '1986-02-27', '2015-03-01', 'Store Manager',            68000.00, 2, 1,    2),
    (7,  'Imani',   'Brooks',    'imani.brooks@zephyr-outdoor.example',     '1995-06-08', '2019-07-22', 'Sales Associate',          41000.00, 2, 1,    6),
    (8,  'Rafael',  'Mendes',    'rafael.mendes@zephyr-outdoor.example',    '1998-12-15', '2021-04-05', 'Sales Associate',          39000.00, 2, 1,    6),
    (9,  'Greta',   'Sorensen',  'greta.sorensen@zephyr-outdoor.example',   '1983-10-10', '2017-05-15', 'Store Manager',            66000.00, 2, 2,    2),
    (10, 'Omar',    'Haddad',    'omar.haddad@zephyr-outdoor.example',      '1993-03-25', '2018-09-03', 'Sales Associate',          42000.00, 2, 2,    9),
    (11, 'Lucy',    'Tran',      'lucy.tran@zephyr-outdoor.example',        '1990-08-14', '2019-08-26', 'Store Manager',            64000.00, 2, 3,    2),
    (12, 'Felix',   'Aubert',    'felix.aubert@zephyr-outdoor.example',     '1999-05-02', '2022-01-17', 'Sales Associate',          38500.00, 2, 3,    11),
    (13, 'Nadia',   'Petrov',    'nadia.petrov@zephyr-outdoor.example',     '1992-11-28', '2020-02-03', 'HR Specialist',            52000.00, 3, NULL, 3),
    (14, 'Samuel',  'Oyelaran',  'samuel.oyelaran@zephyr-outdoor.example',  '1985-04-06', '2016-10-10', 'Warehouse Supervisor',     58000.00, 4, NULL, 4),
    (15, 'Wen',     'Li',        'wen.li@zephyr-outdoor.example',           '1994-09-17', '2021-06-14', 'Systems Administrator',    72000.00, 5, NULL, 5),
    (16, 'Bianca',  'Russo',     'bianca.russo@zephyr-outdoor.example',     '1989-01-31', '2023-10-02', 'Store Manager',            63000.00, 2, 4,    2);

UPDATE department SET manager_id = 1, manager_since = '2015-01-05' WHERE department_id = 1;
UPDATE department SET manager_id = 2, manager_since = '2015-02-10' WHERE department_id = 2;
UPDATE department SET manager_id = 3, manager_since = '2016-05-16' WHERE department_id = 3;
UPDATE department SET manager_id = 4, manager_since = '2015-08-01' WHERE department_id = 4;
UPDATE department SET manager_id = 5, manager_since = '2018-03-12' WHERE department_id = 5;

INSERT INTO employee_dependent (employee_id, dependent_name, relationship, birth_date) VALUES
    (1,  'Kelechi', 'Spouse', '1974-08-20'),
    (1,  'Ada',     'Child',  '2008-02-11'),
    (4,  'Astrid',  'Child',  '2012-06-30'),
    (4,  'Nils',    'Child',  '2015-09-04'),
    (6,  'Jordan',  'Spouse', '1987-12-01'),
    (9,  'Jordan',  'Child',  '2014-05-22'),
    (10, 'Layla',   'Child',  '2020-03-18'),
    (14, 'Ruth',    'Parent', '1955-07-07');

INSERT INTO customer (customer_id, first_name, last_name, email, street, city, postal_code, joined_on) VALUES
    (1,  'Elena',    'Marsh',     'elena.marsh@mail.example',      '31 Birch Lane',       'Portland', '97205', '2019-04-02'),
    (2,  'Kofi',     'Asante',    'kofi.asante@mail.example',      '8 Alder Court',       'Portland', '97209', '2020-01-15'),
    (3,  'Sienna',   'Blackwood', 'sienna.blackwood@mail.example', '220 Pine Ridge Dr',   'Denver',   '80205', '2020-06-21'),
    (4,  'Mateo',    'Fuentes',   'mateo.fuentes@mail.example',    '5 Aspen Loop',        'Boulder',  '80301', '2021-03-09'),
    (5,  'Ingrid',   'Holm',      'ingrid.holm@mail.example',      '14 Lakeview Terrace', 'Chicago',  '60614', '2021-08-30'),
    (6,  'Tariq',    'Nasser',    'tariq.nasser@mail.example',     '63 Maple Street',     'Chicago',  '60622', '2022-02-14'),
    (7,  'Chloe',    'Dubois',    'chloe.dubois@mail.example',     '902 Juniper Blvd',    'Seattle',  '98101', '2022-05-05'),
    (8,  'Arjun',    'Mehta',     'arjun.mehta@mail.example',      '17 Saguaro Path',     'Phoenix',  '85006', '2023-01-27'),
    (9,  'Yuki',     'Tanaka',    'yuki.tanaka@mail.example',      '40 Cedar Hollow',     'Austin',   '78701', '2023-07-11'),
    (10, 'Benedikt', 'Vogel',     'benedikt.vogel@mail.example',   '6 Willow Bend',       'Boise',    '83702', '2024-02-19');

INSERT INTO customer_phone (customer_id, phone_number, phone_type) VALUES
    (1, '555-1001', 'mobile'),
    (1, '555-1002', 'home'),
    (2, '555-1003', 'mobile'),
    (3, '555-1004', 'mobile'),
    (3, '555-1005', 'work'),
    (3, '555-1006', 'home'),
    (5, '555-1007', 'mobile'),
    (6, '555-1008', 'mobile'),
    (7, '555-1009', 'work'),
    (8, '555-1010', 'mobile'),
    (9, '555-1011', 'mobile');

INSERT INTO loyalty_card (card_number, customer_id, tier, points, issued_on) VALUES
    ('ZL-000101', 1, 'Gold',   4820, '2019-04-02'),
    ('ZL-000102', 3, 'Silver', 1975, '2020-07-01'),
    ('ZL-000103', 5, 'Bronze',  430, '2021-09-12'),
    ('ZL-000104', 7, 'Gold',   5310, '2022-05-05'),
    ('ZL-000105', 8, 'Bronze',  120, '2023-02-01');

INSERT INTO category (category_id, name, parent_category_id) VALUES
    (1, 'Camping',             NULL),
    (2, 'Tents',               1),
    (3, 'Sleeping Bags',       1),
    (4, 'Hiking',              NULL),
    (5, 'Backpacks',           4),
    (6, 'Footwear',            4),
    (7, 'Climbing',            NULL),
    (8, 'Cooking & Hydration', 1),
    (9, 'Apparel',             NULL);

INSERT INTO product (product_id, sku, name, description, unit_price, discontinued, category_id) VALUES
    (1,  'TNT-2P-01', 'Ridgeline 2-Person Tent',          'Three-season freestanding tent, 1.9 kg',        249.00, 0, 2),
    (2,  'TNT-4P-02', 'Basecamp 4-Person Tent',           'Family tent with two vestibules',               399.00, 0, 2),
    (3,  'SLB-DN-01', 'Featherdown -5C Sleeping Bag',     'Down-filled mummy bag rated to -5 C',           189.50, 0, 3),
    (4,  'SLB-SY-02', 'Trailhead Synthetic Sleeping Bag', 'Synthetic fill, rated to +5 C',                  89.90, 0, 3),
    (5,  'BPK-45-01', 'Summit 45L Backpack',              'Weekend pack with adjustable torso',            159.00, 0, 5),
    (6,  'BPK-65-02', 'Expedition 65L Backpack',          'Multi-day pack with rain cover',                219.00, 0, 5),
    (7,  'BPK-20-03', 'Daybreak 20L Daypack',             'Lightweight daypack with hydration sleeve',      64.50, 0, 5),
    (8,  'FTW-HB-01', 'Granite Hiking Boots',             'Waterproof leather mid-cut boots',              174.00, 0, 6),
    (9,  'FTW-TR-02', 'Swiftfoot Trail Runners',          'Breathable trail running shoes',                119.00, 0, 6),
    (10, 'CLM-HR-01', 'Apex Climbing Harness',            'Adjustable sport climbing harness',              79.00, 0, 7),
    (11, 'CLM-RP-02', 'Dynamic Rope 60m',                 '9.8 mm single rope, dry treated',               199.00, 0, 7),
    (12, 'CLM-CB-03', 'Locking Carabiner (3-pack)',       'Screw-gate aluminium carabiners',                34.00, 0, 7),
    (13, 'CKG-ST-01', 'Pocket Camp Stove',                'Folding canister stove, 85 g',                   54.90, 0, 8),
    (14, 'CKG-WB-02', 'Insulated Water Bottle 1L',        'Double-wall stainless steel bottle',             29.50, 0, 8),
    (15, 'CKG-FL-03', 'Gravity Water Filter',             '4 L gravity-fed filter system',                  99.00, 0, 8),
    (16, 'TNT-1P-03', 'Solo Bivy Shelter',                NULL,                                            139.00, 1, 2);

INSERT INTO supplier (supplier_id, company_name, contact_name, email, phone, country) VALUES
    (1, 'Alpenglow Textiles GmbH', 'Katrin Bauer',  'sales@alpenglow.example',       '555-2001', 'Germany'),
    (2, 'Cascade Gear Works',      'Derek Nolan',   'orders@cascadegear.example',    '555-2002', 'USA'),
    (3, 'Shenzhen Trailcraft Ltd', 'Li Na',         'export@trailcraft.example',     '555-2003', 'China'),
    (4, 'Andes Rope & Rigging',    'Lucia Paredes', 'ventas@andesrope.example',      '555-2004', 'Peru'),
    (5, 'Nordic Hydration AS',     'Erik Dahl',     'post@nordichydration.example',  '555-2005', 'Norway'),
    (6, 'Outback Canvas Pty',      NULL,            'hello@outbackcanvas.example',   NULL,       'Australia');

INSERT INTO product_supplier (product_id, supplier_id, supply_cost, lead_time_days, is_preferred) VALUES
    (1,  1, 140.00, 21, 1),
    (1,  3, 128.00, 35, 0),
    (2,  1, 225.00, 21, 1),
    (3,  1, 105.00, 28, 1),
    (4,  3,  42.00, 35, 1),
    (4,  2,  51.00, 10, 0),
    (5,  2,  88.00, 14, 1),
    (6,  2, 121.00, 14, 1),
    (6,  3, 109.00, 40, 0),
    (7,  3,  27.50, 35, 1),
    (8,  2,  96.00, 12, 1),
    (9,  3,  58.00, 30, 1),
    (10, 4,  38.00, 25, 1),
    (11, 4, 102.00, 25, 1),
    (12, 4,  13.50, 25, 1),
    (12, 3,  11.00, 35, 0),
    (13, 3,  24.00, 30, 1),
    (14, 5,  12.00, 18, 1),
    (15, 5,  48.00, 18, 1),
    (15, 2,  55.00,  9, 0),
    (16, 1,  80.00, 21, 1);

INSERT INTO warehouse (warehouse_id, name, city, capacity_m3) VALUES
    (1, 'West Coast Distribution Center', 'Tacoma',      12000),
    (2, 'Central Hub',                    'Kansas City', 18500),
    (3, 'Southwest Depot',                'Tucson',       6000);

INSERT INTO inventory (warehouse_id, product_id, quantity_on_hand, reorder_level, last_counted_on) VALUES
    (1, 1,   42, 10, '2026-09-12'),
    (1, 2,   18,  5, '2026-09-12'),
    (1, 3,   30,  8, '2026-09-12'),
    (1, 5,   55, 15, '2026-09-12'),
    (1, 6,   22,  8, '2026-09-12'),
    (1, 8,   40, 12, '2026-09-12'),
    (1, 10,  60, 20, '2026-09-12'),
    (1, 11,  25,  8, '2026-09-12'),
    (1, 12, 140, 40, '2026-09-12'),
    (1, 14, 200, 50, '2026-09-12'),
    (2, 1,   65, 15, '2026-09-19'),
    (2, 2,   30,  8, '2026-09-19'),
    (2, 3,   48, 10, '2026-09-19'),
    (2, 4,  120, 30, '2026-09-19'),
    (2, 5,   70, 20, '2026-09-19'),
    (2, 6,   35, 10, '2026-09-19'),
    (2, 7,  150, 40, '2026-09-19'),
    (2, 8,   58, 15, '2026-09-19'),
    (2, 9,   90, 25, '2026-09-19'),
    (2, 12, 300, 80, '2026-09-19'),
    (2, 13, 110, 30, '2026-09-19'),
    (2, 14, 340, 80, '2026-09-19'),
    (2, 15,  45, 12, '2026-09-19'),
    (3, 4,   35, 10, '2026-09-05'),
    (3, 7,   40, 15, '2026-09-05'),
    (3, 9,    6, 10, '2026-09-05'),
    (3, 13,  28, 10, '2026-09-05'),
    (3, 14,  75, 25, '2026-09-05'),
    (3, 16,   4,  0, '2026-09-05');

-- store_id and employee_id are NULL for online orders.
INSERT INTO sales_order (order_id, order_date, status, customer_id, store_id, employee_id) VALUES
    (1,  '2026-01-12', 'Delivered',  1, 1,    7),
    (2,  '2026-01-20', 'Delivered',  3, 2,    10),
    (3,  '2026-02-03', 'Delivered',  2, 1,    8),
    (4,  '2026-02-14', 'Delivered',  7, NULL, NULL),
    (5,  '2026-03-08', 'Delivered',  5, 3,    12),
    (6,  '2026-03-22', 'Delivered',  1, NULL, NULL),
    (7,  '2026-04-10', 'Delivered',  4, 2,    9),
    (8,  '2026-05-02', 'Delivered',  6, 3,    11),
    (9,  '2026-06-18', 'Delivered',  3, NULL, NULL),
    (10, '2026-07-07', 'Delivered',  8, NULL, NULL),
    (11, '2026-08-15', 'Shipped',    7, NULL, NULL),
    (12, '2026-09-01', 'Shipped',    9, NULL, NULL),
    (13, '2026-09-20', 'Processing', 1, 1,    7),
    (14, '2026-09-28', 'Cancelled',  2, NULL, NULL),
    (15, '2026-10-03', 'Pending',    5, 3,    12);

INSERT INTO order_item (order_id, line_no, product_id, quantity, unit_price, discount_pct) VALUES
    (1,  1, 1,  1, 249.00,  0),
    (1,  2, 3,  2, 189.50,  0),
    (1,  3, 14, 2,  29.50,  0),
    (2,  1, 8,  1, 174.00,  0),
    (2,  2, 5,  1, 159.00, 10),
    (3,  1, 7,  1,  64.50,  0),
    (3,  2, 14, 1,  29.50,  0),
    (4,  1, 11, 1, 199.00,  0),
    (4,  2, 10, 2,  79.00,  0),
    (4,  3, 12, 2,  34.00,  0),
    (5,  1, 2,  1, 399.00,  5),
    (5,  2, 4,  4,  89.90,  0),
    (6,  1, 6,  1, 219.00,  0),
    (6,  2, 15, 1,  99.00,  0),
    (7,  1, 9,  2, 119.00,  0),
    (7,  2, 13, 1,  54.90,  0),
    (8,  1, 16, 1, 139.00, 20),
    (8,  2, 14, 3,  29.50,  0),
    (9,  1, 1,  1, 249.00,  0),
    (9,  2, 4,  2,  89.90,  0),
    (10, 1, 5,  1, 159.00,  0),
    (10, 2, 8,  1, 174.00,  0),
    (10, 3, 13, 1,  54.90,  0),
    (11, 1, 2,  1, 399.00,  0),
    (11, 2, 3,  2, 189.50,  0),
    (11, 3, 6,  1, 219.00,  0),
    (12, 1, 9,  1, 119.00,  0),
    (12, 2, 7,  1,  64.50,  0),
    (13, 1, 10, 1,  79.00,  0),
    (13, 2, 11, 1, 199.00,  0),
    (14, 1, 15, 1,  99.00,  0),
    (15, 1, 12, 4,  34.00,  0),
    (15, 2, 5,  1, 159.00,  0);

INSERT INTO payment (payment_id, order_id, paid_on, amount, method) VALUES
    (1,  1,  '2026-01-12', 687.00, 'Credit Card'),
    (2,  2,  '2026-01-20', 317.10, 'Debit Card'),
    (3,  3,  '2026-02-03',  94.00, 'Cash'),
    (4,  4,  '2026-02-14', 425.00, 'Credit Card'),
    (5,  5,  '2026-03-08', 100.00, 'Gift Card'),
    (6,  5,  '2026-03-08', 638.65, 'Credit Card'),
    (7,  6,  '2026-03-22', 318.00, 'Credit Card'),
    (8,  7,  '2026-04-10', 292.90, 'Debit Card'),
    (9,  8,  '2026-05-02', 199.70, 'Cash'),
    (10, 9,  '2026-06-18', 428.80, 'Credit Card'),
    (11, 10, '2026-07-07', 387.90, 'Bank Transfer'),
    (12, 11, '2026-08-15', 500.00, 'Gift Card'),
    (13, 11, '2026-08-15', 497.00, 'Credit Card'),
    (14, 12, '2026-09-01', 183.50, 'Credit Card'),
    (15, 13, '2026-09-20', 278.00, 'Debit Card');

INSERT INTO shipment (shipment_id, order_id, warehouse_id, carrier, tracking_number, shipped_on, delivered_on) VALUES
    (1, 4,  1, 'SwiftParcel',       'SP-88410023', '2026-02-15', '2026-02-18'),
    (2, 6,  2, 'SwiftParcel',       'SP-88417754', '2026-03-23', '2026-03-25'),
    (3, 9,  2, 'NorthStar Freight', 'NS-5520917',  '2026-06-19', '2026-06-23'),
    (4, 10, 2, 'NorthStar Freight', 'NS-5523388',  '2026-07-08', '2026-07-11'),
    (5, 11, 1, 'SwiftParcel',       'SP-88440912', '2026-08-16', '2026-08-19'),
    (6, 11, 2, 'SwiftParcel',       'SP-88441035', '2026-08-18', NULL),
    (7, 12, 3, 'Desert Express',    'DX-104477',   '2026-09-02', NULL);
