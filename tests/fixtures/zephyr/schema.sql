-- Zephyr Outdoor Supply Co. (fictional) -- practice schema for ER diagramming
-- Written for SQLite; the types are plain ANSI so it reads the same elsewhere.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- People and organization
-- ---------------------------------------------------------------------------

CREATE TABLE department (
    department_id   INTEGER PRIMARY KEY,
    name            VARCHAR(50)   NOT NULL UNIQUE,
    budget          DECIMAL(12,2) NOT NULL CHECK (budget >= 0),
    manager_id      INT UNIQUE,
    manager_since   DATE,
    FOREIGN KEY (manager_id) REFERENCES employee (employee_id)
);

CREATE TABLE store (
    store_id        INTEGER PRIMARY KEY,
    name            VARCHAR(60)  NOT NULL UNIQUE,
    street          VARCHAR(100) NOT NULL,
    city            VARCHAR(50)  NOT NULL,
    postal_code     VARCHAR(12)  NOT NULL,
    phone           VARCHAR(20),
    opened_on       DATE         NOT NULL
);

CREATE TABLE employee (
    employee_id     INTEGER PRIMARY KEY,
    first_name      VARCHAR(40)   NOT NULL,
    last_name       VARCHAR(40)   NOT NULL,
    email           VARCHAR(100)  NOT NULL UNIQUE,
    birth_date      DATE          NOT NULL,
    hire_date       DATE          NOT NULL,
    job_title       VARCHAR(60)   NOT NULL,
    salary          DECIMAL(10,2) NOT NULL CHECK (salary > 0),
    department_id   INT           NOT NULL,
    store_id        INT,
    supervisor_id   INT,
    FOREIGN KEY (department_id) REFERENCES department (department_id),
    FOREIGN KEY (store_id)      REFERENCES store (store_id),
    FOREIGN KEY (supervisor_id) REFERENCES employee (employee_id),
    CHECK (supervisor_id <> employee_id)
);

CREATE TABLE employee_dependent (
    employee_id     INT         NOT NULL,
    dependent_name  VARCHAR(40) NOT NULL,
    relationship    VARCHAR(10) NOT NULL CHECK (relationship IN ('Spouse', 'Child', 'Parent')),
    birth_date      DATE        NOT NULL,
    PRIMARY KEY (employee_id, dependent_name),
    FOREIGN KEY (employee_id) REFERENCES employee (employee_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Customers
-- ---------------------------------------------------------------------------

CREATE TABLE customer (
    customer_id     INTEGER PRIMARY KEY,
    first_name      VARCHAR(40)  NOT NULL,
    last_name       VARCHAR(40)  NOT NULL,
    email           VARCHAR(100) NOT NULL UNIQUE,
    street          VARCHAR(100) NOT NULL,
    city            VARCHAR(50)  NOT NULL,
    postal_code     VARCHAR(12)  NOT NULL,
    joined_on       DATE         NOT NULL
);

CREATE TABLE customer_phone (
    customer_id     INT         NOT NULL,
    phone_number    VARCHAR(20) NOT NULL,
    phone_type      VARCHAR(10) NOT NULL CHECK (phone_type IN ('mobile', 'home', 'work')),
    PRIMARY KEY (customer_id, phone_number),
    FOREIGN KEY (customer_id) REFERENCES customer (customer_id) ON DELETE CASCADE
);

CREATE TABLE loyalty_card (
    card_number     VARCHAR(12) PRIMARY KEY NOT NULL,
    customer_id     INT         NOT NULL UNIQUE,
    tier            VARCHAR(10) NOT NULL CHECK (tier IN ('Bronze', 'Silver', 'Gold')),
    points          INT         NOT NULL DEFAULT 0 CHECK (points >= 0),
    issued_on       DATE        NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customer (customer_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Catalog and supply
-- ---------------------------------------------------------------------------

CREATE TABLE category (
    category_id         INTEGER PRIMARY KEY,
    name                VARCHAR(50) NOT NULL UNIQUE,
    parent_category_id  INT,
    FOREIGN KEY (parent_category_id) REFERENCES category (category_id),
    CHECK (parent_category_id <> category_id)
);

CREATE TABLE product (
    product_id      INTEGER PRIMARY KEY,
    sku             VARCHAR(12)   NOT NULL UNIQUE,
    name            VARCHAR(80)   NOT NULL,
    description     VARCHAR(255),
    unit_price      DECIMAL(10,2) NOT NULL CHECK (unit_price >= 0),
    discontinued    BOOLEAN       NOT NULL DEFAULT 0,
    category_id     INT           NOT NULL,
    FOREIGN KEY (category_id) REFERENCES category (category_id)
);

CREATE TABLE supplier (
    supplier_id     INTEGER PRIMARY KEY,
    company_name    VARCHAR(80)  NOT NULL,
    contact_name    VARCHAR(80),
    email           VARCHAR(100) NOT NULL UNIQUE,
    phone           VARCHAR(20),
    country         VARCHAR(50)  NOT NULL
);

CREATE TABLE product_supplier (
    product_id      INT           NOT NULL,
    supplier_id     INT           NOT NULL,
    supply_cost     DECIMAL(10,2) NOT NULL CHECK (supply_cost >= 0),
    lead_time_days  INT           NOT NULL CHECK (lead_time_days >= 0),
    is_preferred    BOOLEAN       NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, supplier_id),
    FOREIGN KEY (product_id)  REFERENCES product (product_id),
    FOREIGN KEY (supplier_id) REFERENCES supplier (supplier_id)
);

-- ---------------------------------------------------------------------------
-- Stock
-- ---------------------------------------------------------------------------

CREATE TABLE warehouse (
    warehouse_id    INTEGER PRIMARY KEY,
    name            VARCHAR(60) NOT NULL UNIQUE,
    city            VARCHAR(50) NOT NULL,
    capacity_m3     INT         NOT NULL CHECK (capacity_m3 > 0)
);

CREATE TABLE inventory (
    warehouse_id      INT  NOT NULL,
    product_id        INT  NOT NULL,
    quantity_on_hand  INT  NOT NULL CHECK (quantity_on_hand >= 0),
    reorder_level     INT  NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    last_counted_on   DATE NOT NULL,
    PRIMARY KEY (warehouse_id, product_id),
    FOREIGN KEY (warehouse_id) REFERENCES warehouse (warehouse_id),
    FOREIGN KEY (product_id)   REFERENCES product (product_id)
);

-- ---------------------------------------------------------------------------
-- Sales
-- ---------------------------------------------------------------------------

CREATE TABLE sales_order (
    order_id        INTEGER PRIMARY KEY,
    order_date      DATE        NOT NULL,
    status          VARCHAR(12) NOT NULL
                    CHECK (status IN ('Pending', 'Processing', 'Shipped', 'Delivered', 'Cancelled')),
    customer_id     INT         NOT NULL,
    store_id        INT,
    employee_id     INT,
    FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    FOREIGN KEY (store_id)    REFERENCES store (store_id),
    FOREIGN KEY (employee_id) REFERENCES employee (employee_id)
);

CREATE TABLE order_item (
    order_id        INT           NOT NULL,
    line_no         INT           NOT NULL CHECK (line_no >= 1),
    product_id      INT           NOT NULL,
    quantity        INT           NOT NULL CHECK (quantity > 0),
    unit_price      DECIMAL(10,2) NOT NULL CHECK (unit_price >= 0),
    discount_pct    DECIMAL(5,2)  NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    PRIMARY KEY (order_id, line_no),
    FOREIGN KEY (order_id)   REFERENCES sales_order (order_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES product (product_id)
);

CREATE TABLE payment (
    payment_id      INTEGER PRIMARY KEY,
    order_id        INT           NOT NULL,
    paid_on         DATE          NOT NULL,
    amount          DECIMAL(10,2) NOT NULL CHECK (amount > 0),
    method          VARCHAR(15)   NOT NULL
                    CHECK (method IN ('Cash', 'Credit Card', 'Debit Card', 'Gift Card', 'Bank Transfer')),
    FOREIGN KEY (order_id) REFERENCES sales_order (order_id)
);

CREATE TABLE shipment (
    shipment_id      INTEGER PRIMARY KEY,
    order_id         INT         NOT NULL,
    warehouse_id     INT         NOT NULL,
    carrier          VARCHAR(40) NOT NULL,
    tracking_number  VARCHAR(30) NOT NULL UNIQUE,
    shipped_on       DATE        NOT NULL,
    delivered_on     DATE,
    FOREIGN KEY (order_id)     REFERENCES sales_order (order_id),
    FOREIGN KEY (warehouse_id) REFERENCES warehouse (warehouse_id),
    CHECK (delivered_on IS NULL OR delivered_on >= shipped_on)
);
