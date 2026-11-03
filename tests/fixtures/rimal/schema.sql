-- =====================================================================
-- RIMAL TELECOM  --  FICTIONAL DEMO DATABASE
-- ---------------------------------------------------------------------
-- "Rimal Telecom" does not exist. Every customer, employee, dealer,
-- number, identifier and amount in this database is randomly generated.
-- Identifiers that carry a checksum (national ID / Iqama, IBAN, card
-- number, IMEI, ICCID) deliberately FAIL that checksum.
--
-- Dialect: SQLite 3. Table names carry an area prefix because SQLite has
-- no schemas:  CRM_ PRD_ SUB_ BIL_ USG_ NET_ SAL_ SUP_ HR_  (+ LKP_, SYS_)
--
-- This file is executed by generate.py: the TABLES section before the
-- load, the INDEXES and VIEWS sections after it.
-- Each table's comment sits inside its CREATE statement so that SQLite
-- keeps it in sqlite_master.
-- =====================================================================

-- ==== TABLES ====

-- ---------------------------------------------------------------------
-- SYS_ / LKP_  : database label and shared lookups
-- ---------------------------------------------------------------------

CREATE TABLE SYS_DB_INFO (  -- Database label: says this is FICTIONAL demo data; also holds scale, seed and the data window.
    INFO_KEY    VARCHAR(40)  NOT NULL PRIMARY KEY,
    INFO_VALUE  VARCHAR(400) NOT NULL
);

CREATE TABLE LKP_CURRENCY (  -- Lookup: currency codes. Accounts only ever use SAR; rates are illustrative.
    CURRENCY_CODE  CHAR(3)       NOT NULL PRIMARY KEY,
    CURRENCY_NAME  VARCHAR(40)   NOT NULL,
    MINOR_UNITS    INTEGER       NOT NULL DEFAULT 2 CHECK (MINOR_UNITS BETWEEN 0 AND 3),
    RATE_TO_SAR    NUMERIC(12,6) NOT NULL CHECK (RATE_TO_SAR > 0)
);

CREATE TABLE LKP_CITY (  -- Lookup: the 12 cities served, each mapped to a commercial region.
    CITY_CODE     CHAR(3)      NOT NULL PRIMARY KEY,
    CITY_NAME     VARCHAR(40)  NOT NULL UNIQUE,
    CITY_NAME_AR  VARCHAR(40)  NOT NULL,
    PROVINCE      VARCHAR(40)  NOT NULL,
    REGION_ID     INTEGER      NOT NULL REFERENCES NET_REGION (REGION_ID),
    POPULATION_K  INTEGER      CHECK (POPULATION_K > 0)
);

CREATE TABLE LKP_STATUS_CODE (  -- Lookup: status codes per domain (customer, subscription, invoice, ticket ...). Not enforced by foreign keys.
    STATUS_DOMAIN  VARCHAR(20)  NOT NULL,
    STATUS_CODE    VARCHAR(12)  NOT NULL,
    DESCRIPTION    VARCHAR(100) NOT NULL,
    IS_OPEN_STATE  CHAR(1)      NOT NULL DEFAULT 'N' CHECK (IS_OPEN_STATE IN ('Y','N')),
    SORT_ORDER     INTEGER      NOT NULL,
    PRIMARY KEY (STATUS_DOMAIN, STATUS_CODE)
);

-- ---------------------------------------------------------------------
-- CRM_  : customers and accounts
-- ---------------------------------------------------------------------

CREATE TABLE CRM_SEGMENT (  -- Customer value segments used by marketing and the call centre.
    SEGMENT_CODE       VARCHAR(10)  NOT NULL PRIMARY KEY,
    SEGMENT_NAME       VARCHAR(40)  NOT NULL,
    DESCRIPTION        VARCHAR(200),
    MIN_MONTHLY_SPEND  NUMERIC(10,2) CHECK (MIN_MONTHLY_SPEND >= 0)
);

CREATE TABLE CRM_CUSTOMER (  -- Customer master: one row per individual, business or visitor customer. PERSONAL DATA.
    CUST_ID         INTEGER      NOT NULL PRIMARY KEY,
    CUST_TYPE       CHAR(3)      NOT NULL CHECK (CUST_TYPE IN ('IND','BUS','VIS')),
    FIRST_NAME      VARCHAR(40)  NOT NULL,
    FATHER_NAME     VARCHAR(40),
    LAST_NAME       VARCHAR(40)  NOT NULL,
    FULL_NAME_AR    VARCHAR(120),              -- Arabic-script name
    ID_TYPE         CHAR(3)      NOT NULL CHECK (ID_TYPE IN ('NID','IQA','CR','BRD')),
    ID_NUMBER       VARCHAR(15)  NOT NULL,     -- national ID / Iqama / CR / border number
    BIRTH_DATE      DATE,
    GENDER          CHAR(1)      CHECK (GENDER IN ('M','F')),
    NATIONALITY     CHAR(2),
    EMAIL           VARCHAR(80),
    CONTACT_MSISDN  VARCHAR(15),
    ALT_CONTACT_NO  VARCHAR(20),               -- second contact number, rarely captured
    FAX_NO          VARCHAR(20),               -- legacy, no longer captured
    CITY_CODE       CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    SEGMENT_CODE    VARCHAR(10)  NOT NULL REFERENCES CRM_SEGMENT (SEGMENT_CODE),
    PREFERRED_LANG  CHAR(2)      NOT NULL DEFAULT 'AR' CHECK (PREFERRED_LANG IN ('AR','EN')),
    STATUS          VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','INACTIVE','BLOCKED')),
    CREATED_DATE    DATE         NOT NULL,
    LAST_UPDATED    TIMESTAMP
);

CREATE TABLE CRM_ACCOUNT (  -- Billing account. A customer has one or more; subscriptions hang off the account.
    ACCOUNT_ID     INTEGER      NOT NULL PRIMARY KEY,
    ACCOUNT_NO     VARCHAR(12)  NOT NULL UNIQUE,
    CUST_ID        INTEGER      NOT NULL REFERENCES CRM_CUSTOMER (CUST_ID),
    ACCOUNT_TYPE   VARCHAR(10)  NOT NULL CHECK (ACCOUNT_TYPE IN ('PREPAID','POSTPAID','BUSINESS')),
    CYCLE_CODE     CHAR(3)      REFERENCES BIL_BILL_CYCLE (CYCLE_CODE),   -- NULL for prepaid
    CREDIT_LIMIT   NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (CREDIT_LIMIT >= 0),
    CREDIT_CLASS   CHAR(1)      CHECK (CREDIT_CLASS IN ('A','B','C')),
    CURRENCY_CODE  CHAR(3)      NOT NULL DEFAULT 'SAR' REFERENCES LKP_CURRENCY (CURRENCY_CODE),
    EBILL_FLAG     CHAR(1)      NOT NULL DEFAULT 'Y' CHECK (EBILL_FLAG IN ('Y','N')),
    STATUS         VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','CLOSED','SUSPENDED')),
    OPEN_DATE      DATE         NOT NULL,
    CLOSE_DATE     DATE,
    CHECK (CLOSE_DATE IS NULL OR CLOSE_DATE >= OPEN_DATE)
);

CREATE TABLE CRM_ADDRESS (  -- Customer addresses in national-address format (building, street, district, postal code). PERSONAL DATA.
    ADDRESS_ID     INTEGER      NOT NULL PRIMARY KEY,
    CUST_ID        INTEGER      NOT NULL REFERENCES CRM_CUSTOMER (CUST_ID),
    ADDRESS_TYPE   VARCHAR(10)  NOT NULL CHECK (ADDRESS_TYPE IN ('HOME','BILLING','INSTALL','OFFICE')),
    BUILDING_NO    CHAR(4)      NOT NULL,
    STREET_NAME    VARCHAR(60)  NOT NULL,
    DISTRICT       VARCHAR(40)  NOT NULL,
    CITY_CODE      CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    POSTAL_CODE    CHAR(5)      NOT NULL,
    ADDITIONAL_NO  CHAR(4),
    UNIT_NO        VARCHAR(6),
    LATITUDE       NUMERIC(9,6),
    LONGITUDE      NUMERIC(9,6),
    IS_PRIMARY     CHAR(1)      NOT NULL DEFAULT 'Y' CHECK (IS_PRIMARY IN ('Y','N'))
);

CREATE TABLE CRM_BUSINESS_PROFILE (  -- One-to-one extension of CRM_CUSTOMER for business customers (company registration and account manager).
    CUST_ID             INTEGER      NOT NULL PRIMARY KEY REFERENCES CRM_CUSTOMER (CUST_ID),
    COMPANY_NAME        VARCHAR(100) NOT NULL,
    CR_NUMBER           CHAR(10)     NOT NULL,
    VAT_NUMBER          CHAR(15),
    INDUSTRY            VARCHAR(40),
    EMPLOYEE_BAND       VARCHAR(10)  CHECK (EMPLOYEE_BAND IN ('1-9','10-49','50-249','250+')),
    ACCOUNT_MANAGER_ID  INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    CONTRACT_START      DATE
);

CREATE TABLE CRM_BANK_MANDATE (  -- Direct-debit and card-on-file mandates for postpaid accounts. Holds IBANs and card numbers. PERSONAL DATA.
    MANDATE_ID    INTEGER      NOT NULL PRIMARY KEY,
    ACCOUNT_ID    INTEGER      NOT NULL REFERENCES CRM_ACCOUNT (ACCOUNT_ID),
    MANDATE_TYPE  CHAR(4)      NOT NULL CHECK (MANDATE_TYPE IN ('IBAN','CARD')),
    IBAN          CHAR(24),
    BANK_NAME     VARCHAR(40),
    CARD_NUMBER   VARCHAR(19),
    CARD_EXPIRY   CHAR(5),
    HOLDER_NAME   VARCHAR(80)  NOT NULL,
    SIGNED_DATE   DATE         NOT NULL,
    STATUS        VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','CANCELLED','EXPIRED')),
    CHECK ((MANDATE_TYPE = 'IBAN' AND IBAN IS NOT NULL) OR (MANDATE_TYPE = 'CARD' AND CARD_NUMBER IS NOT NULL))
);

CREATE TABLE CRM_MARKETING_CONSENT (  -- Marketing opt-in log loaded from the campaign tool. Bolt-on table: CUSTOMER_ID has no declared foreign key.
    CONSENT_ID   INTEGER      NOT NULL PRIMARY KEY,
    CUSTOMER_ID  INTEGER      NOT NULL,
    CHANNEL      VARCHAR(10)  NOT NULL CHECK (CHANNEL IN ('SMS','EMAIL','CALL','PUSH')),
    OPT_IN_FLAG  CHAR(1)      NOT NULL CHECK (OPT_IN_FLAG IN ('Y','N')),
    CAPTURED_DT  TIMESTAMP    NOT NULL,
    SOURCE       VARCHAR(20)
);

-- ---------------------------------------------------------------------
-- PRD_  : products and tariffs
-- ---------------------------------------------------------------------

CREATE TABLE PRD_PRODUCT_FAMILY (  -- Top level of the product catalogue (prepaid mobile, postpaid mobile, fibre, business ...).
    FAMILY_CODE  VARCHAR(8)   NOT NULL PRIMARY KEY,
    FAMILY_NAME  VARCHAR(40)  NOT NULL,
    DESCRIPTION  VARCHAR(200)
);

CREATE TABLE PRD_PRODUCT (  -- Product lines sold by Rimal Telecom; each has one or more tariff plans.
    PRODUCT_ID      INTEGER      NOT NULL PRIMARY KEY,
    PRODUCT_CODE    VARCHAR(12)  NOT NULL UNIQUE,
    PRODUCT_NAME    VARCHAR(60)  NOT NULL,
    FAMILY_CODE     VARCHAR(8)   NOT NULL REFERENCES PRD_PRODUCT_FAMILY (FAMILY_CODE),
    LAUNCH_DATE     DATE         NOT NULL,
    WITHDRAWN_DATE  DATE,
    STATUS          VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','WITHDRAWN'))
);

CREATE TABLE PRD_TARIFF_PLAN (  -- Tariff plans: allowances, speed and contract length. Prices live in PRD_PLAN_PRICE.
    PLAN_ID          INTEGER      NOT NULL PRIMARY KEY,
    PLAN_CODE        VARCHAR(12)  NOT NULL UNIQUE,
    PLAN_NAME        VARCHAR(60)  NOT NULL,
    PRODUCT_ID       INTEGER      NOT NULL REFERENCES PRD_PRODUCT (PRODUCT_ID),
    PAY_TYPE         CHAR(4)      NOT NULL CHECK (PAY_TYPE IN ('PRE','POST')),
    DATA_GB          NUMERIC(8,1),              -- NULL = unlimited
    VOICE_MIN        INTEGER,                   -- NULL = unlimited
    SMS_COUNT        INTEGER,
    SPEED_MBPS       INTEGER,                   -- fibre only
    CONTRACT_MONTHS  INTEGER      NOT NULL DEFAULT 0 CHECK (CONTRACT_MONTHS IN (0,12,24)),
    STATUS           VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','WITHDRAWN'))
);

CREATE TABLE PRD_PLAN_PRICE (  -- Price history per plan (composite key). The 2025-10-01 price change shows up as a second row per plan.
    PLAN_ID         INTEGER      NOT NULL REFERENCES PRD_TARIFF_PLAN (PLAN_ID),
    EFFECTIVE_FROM  DATE         NOT NULL,
    EFFECTIVE_TO    DATE,
    MONTHLY_FEE     NUMERIC(10,2) NOT NULL CHECK (MONTHLY_FEE >= 0),   -- SAR, excluding VAT
    ACTIVATION_FEE  NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (ACTIVATION_FEE >= 0),
    PRIMARY KEY (PLAN_ID, EFFECTIVE_FROM),
    CHECK (EFFECTIVE_TO IS NULL OR EFFECTIVE_TO >= EFFECTIVE_FROM)
);

CREATE TABLE PRD_ADDON (  -- Optional add-ons: data boosters, international minutes, roaming passes, content packs.
    ADDON_ID       INTEGER      NOT NULL PRIMARY KEY,
    ADDON_CODE     VARCHAR(12)  NOT NULL UNIQUE,
    ADDON_NAME     VARCHAR(60)  NOT NULL,
    ADDON_TYPE     VARCHAR(10)  NOT NULL CHECK (ADDON_TYPE IN ('DATA','VOICE','ROAMING','CONTENT','INTL')),
    PRICE          NUMERIC(10,2) NOT NULL CHECK (PRICE >= 0),
    VALIDITY_DAYS  INTEGER      NOT NULL CHECK (VALIDITY_DAYS > 0),
    DATA_MB        INTEGER,
    STATUS         VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','WITHDRAWN'))
);

CREATE TABLE PRD_ROAMING_ZONE (  -- Roaming and rating zones (HOME, GCC, MENA, EUROPE, ASIA, WORLD).
    ZONE_CODE         VARCHAR(8)   NOT NULL PRIMARY KEY,
    ZONE_NAME         VARCHAR(40)  NOT NULL,
    DESCRIPTION       VARCHAR(200),
    DAILY_PASS_PRICE  NUMERIC(8,2) CHECK (DAILY_PASS_PRICE >= 0)
);

CREATE TABLE PRD_RATE_CARD (  -- Out-of-bundle rates per event type and zone, with effective dates (rates changed on 2025-10-01).
    RATE_ID         INTEGER      NOT NULL PRIMARY KEY,
    EVENT_TYPE      CHAR(3)      NOT NULL REFERENCES USG_EVENT_TYPE (EVENT_TYPE),
    ZONE_CODE       VARCHAR(8)   NOT NULL REFERENCES PRD_ROAMING_ZONE (ZONE_CODE),
    UNIT            VARCHAR(6)   NOT NULL CHECK (UNIT IN ('MIN','SMS','MB')),
    RATE_PER_UNIT   NUMERIC(10,4) NOT NULL CHECK (RATE_PER_UNIT >= 0),
    EFFECTIVE_FROM  DATE         NOT NULL,
    EFFECTIVE_TO    DATE,
    UNIQUE (EVENT_TYPE, ZONE_CODE, EFFECTIVE_FROM)
);

CREATE TABLE PRD_DEVICE_MODEL (  -- Handset, tablet and router models sold or seen on the network. Brands are invented.
    MODEL_ID      INTEGER      NOT NULL PRIMARY KEY,
    BRAND         VARCHAR(30)  NOT NULL,
    MODEL_NAME    VARCHAR(40)  NOT NULL,
    TAC           CHAR(8)      NOT NULL UNIQUE,   -- first 8 digits of the IMEI
    DEVICE_TYPE   VARCHAR(10)  NOT NULL CHECK (DEVICE_TYPE IN ('PHONE','TABLET','ROUTER','WEARABLE')),
    SUPPORTS_5G   CHAR(1)      NOT NULL CHECK (SUPPORTS_5G IN ('Y','N')),
    LIST_PRICE    NUMERIC(10,2) CHECK (LIST_PRICE >= 0),
    LAUNCH_YEAR   INTEGER      CHECK (LAUNCH_YEAR BETWEEN 2012 AND 2030)
);

-- ---------------------------------------------------------------------
-- SUB_  : subscriptions, SIMs, numbers and devices
-- ---------------------------------------------------------------------

CREATE TABLE SUB_SUBSCRIPTION (  -- One row per mobile line, data SIM or fibre service, current and historic. MSISDN is PERSONAL DATA.
    SUBSCRIPTION_ID    INTEGER      NOT NULL PRIMARY KEY,
    ACCOUNT_ID         INTEGER      NOT NULL REFERENCES CRM_ACCOUNT (ACCOUNT_ID),
    PLAN_ID            INTEGER      NOT NULL REFERENCES PRD_TARIFF_PLAN (PLAN_ID),
    MSISDN             VARCHAR(15),               -- 9665XXXXXXXX; NULL for fibre
    SERVICE_TYPE       VARCHAR(8)   NOT NULL CHECK (SERVICE_TYPE IN ('MOBILE','DATA','FIBRE')),
    PAY_TYPE           CHAR(4)      NOT NULL CHECK (PAY_TYPE IN ('PRE','POST')),
    STATUS             VARCHAR(12)  NOT NULL CHECK (STATUS IN ('ACTIVE','SUSPENDED','DEACTIVATED')),
    ACTIVATION_DATE    DATE         NOT NULL,
    DEACTIVATION_DATE  DATE,
    CHURN_REASON       VARCHAR(20),
    CITY_CODE          CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    SALES_CHANNEL      VARCHAR(10),
    DEALER_CODE        VARCHAR(10),               -- selling dealer; no declared foreign key
    LAST_UPDATED       TIMESTAMP
);

CREATE TABLE SUB_SIM_CARD (  -- SIM and eSIM inventory: ICCID, IMSI and PUK. SUBSCRIPTION_ID is NULL while a SIM is in stock. PERSONAL DATA.
    SIM_ID           INTEGER      NOT NULL PRIMARY KEY,
    ICCID            VARCHAR(20)  NOT NULL,       -- should be unique; no constraint was ever added
    IMSI             CHAR(15)     NOT NULL,
    SIM_TYPE         VARCHAR(8)   NOT NULL CHECK (SIM_TYPE IN ('PHYSICAL','ESIM')),
    SUBSCRIPTION_ID  INTEGER      REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    STATUS           VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','INACTIVE','IN_STOCK','REPLACED')),
    BATCH_NO         VARCHAR(12),
    ISSUED_DATE      DATE,
    PUK_CODE         CHAR(8)
);

CREATE TABLE SUB_NUMBER_RANGE (  -- MSISDN ranges allocated to Rimal Telecom, by number class.
    RANGE_ID        INTEGER      NOT NULL PRIMARY KEY,
    RANGE_START     VARCHAR(15)  NOT NULL UNIQUE,
    RANGE_END       VARCHAR(15)  NOT NULL,
    NUMBER_CLASS    VARCHAR(10)  NOT NULL CHECK (NUMBER_CLASS IN ('STANDARD','SILVER','GOLD','M2M')),
    ALLOCATED_DATE  DATE         NOT NULL,
    STATUS          VARCHAR(10)  NOT NULL CHECK (STATUS IN ('OPEN','EXHAUSTED','RESERVED')),
    CHECK (RANGE_END >= RANGE_START)
);

CREATE TABLE SUB_DEVICE (  -- Devices seen on a subscription (IMEI). PERSONAL DATA.
    DEVICE_ID        INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    IMEI             CHAR(15)     NOT NULL,
    MODEL_ID         INTEGER      REFERENCES PRD_DEVICE_MODEL (MODEL_ID),
    FIRST_SEEN_DATE  DATE         NOT NULL,
    LAST_SEEN_DATE   DATE,
    FINANCED_FLAG    CHAR(1)      NOT NULL DEFAULT 'N' CHECK (FINANCED_FLAG IN ('Y','N'))
);

CREATE TABLE SUB_SUBSCRIPTION_ADDON (  -- Add-ons bought on a subscription (composite key: subscription, add-on, start date).
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    ADDON_ID         INTEGER      NOT NULL REFERENCES PRD_ADDON (ADDON_ID),
    START_DATE       DATE         NOT NULL,
    END_DATE         DATE,
    PRICE_PAID       NUMERIC(10,2) NOT NULL CHECK (PRICE_PAID >= 0),
    AUTO_RENEW       CHAR(1)      NOT NULL DEFAULT 'N' CHECK (AUTO_RENEW IN ('Y','N')),
    PRIMARY KEY (SUBSCRIPTION_ID, ADDON_ID, START_DATE)
);

CREATE TABLE SUB_STATUS_HISTORY (  -- Audit trail of subscription status changes (activation, suspension, deactivation). Kept since January 2024.
    HIST_ID          INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    OLD_STATUS       VARCHAR(12),
    NEW_STATUS       VARCHAR(12)  NOT NULL,
    CHANGE_DT        TIMESTAMP    NOT NULL,
    REASON_CODE      VARCHAR(20),
    CHANGED_BY       VARCHAR(20)  NOT NULL
);

CREATE TABLE SUB_PLAN_CHANGE (  -- Plan upgrades, downgrades and forced migrations off withdrawn plans.
    CHANGE_ID        INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    OLD_PLAN_ID      INTEGER      NOT NULL REFERENCES PRD_TARIFF_PLAN (PLAN_ID),
    NEW_PLAN_ID      INTEGER      NOT NULL REFERENCES PRD_TARIFF_PLAN (PLAN_ID),
    CHANGE_DATE      DATE         NOT NULL,
    CHANGE_TYPE      VARCHAR(10)  NOT NULL CHECK (CHANGE_TYPE IN ('UPGRADE','DOWNGRADE','MIGRATION')),
    CHANNEL          VARCHAR(10)
);

CREATE TABLE SUB_FIBRE_SERVICE (  -- One-to-one extension of SUB_SUBSCRIPTION for fibre lines: install address, cabinet, ONT and router.
    SUBSCRIPTION_ID  INTEGER      NOT NULL PRIMARY KEY REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    SERVICE_NO       VARCHAR(12)  NOT NULL UNIQUE,
    ADDRESS_ID       INTEGER      REFERENCES CRM_ADDRESS (ADDRESS_ID),
    CABINET_ID       INTEGER      REFERENCES NET_FIBRE_CABINET (CABINET_ID),
    ONT_SERIAL       VARCHAR(16)  NOT NULL,
    ROUTER_MAC       CHAR(17),
    INSTALL_DATE     DATE         NOT NULL,
    DOWNLOAD_MBPS    INTEGER      NOT NULL CHECK (DOWNLOAD_MBPS > 0),
    UPLOAD_MBPS      INTEGER      NOT NULL CHECK (UPLOAD_MBPS > 0)
);

-- ---------------------------------------------------------------------
-- BIL_  : billing - invoices, payments and adjustments
-- ---------------------------------------------------------------------

CREATE TABLE BIL_BILL_CYCLE (  -- Bill cycles: the day of the month on which a postpaid account is billed.
    CYCLE_CODE   CHAR(3)      NOT NULL PRIMARY KEY,
    CYCLE_DAY    INTEGER      NOT NULL CHECK (CYCLE_DAY BETWEEN 1 AND 28),
    DESCRIPTION  VARCHAR(60)
);

CREATE TABLE BIL_PAYMENT_METHOD (  -- Payment methods accepted for invoices.
    METHOD_CODE    VARCHAR(8)   NOT NULL PRIMARY KEY,
    METHOD_NAME    VARCHAR(40)  NOT NULL,
    IS_ELECTRONIC  CHAR(1)      NOT NULL CHECK (IS_ELECTRONIC IN ('Y','N'))
);

CREATE TABLE BIL_INVOICE (  -- Monthly invoice header per postpaid or business account. Amounts in SAR; VAT is 15%.
    INVOICE_ID       INTEGER      NOT NULL PRIMARY KEY,
    INVOICE_NO       VARCHAR(16)  NOT NULL UNIQUE,
    ACCOUNT_ID       INTEGER      NOT NULL REFERENCES CRM_ACCOUNT (ACCOUNT_ID),
    CYCLE_CODE       CHAR(3)      NOT NULL REFERENCES BIL_BILL_CYCLE (CYCLE_CODE),
    PERIOD_START     DATE         NOT NULL,
    PERIOD_END       DATE         NOT NULL,
    ISSUE_DATE       DATE         NOT NULL,
    DUE_DATE         DATE         NOT NULL,
    AMOUNT_EXCL_VAT  NUMERIC(12,2) NOT NULL,
    VAT_AMOUNT       NUMERIC(12,2) NOT NULL,
    TOTAL_AMOUNT     NUMERIC(12,2) NOT NULL,
    STATUS           VARCHAR(12)  NOT NULL CHECK (STATUS IN ('ISSUED','PAID','OVERDUE','WRITTEN_OFF')),
    PAID_DATE        DATE,
    CHECK (PERIOD_END >= PERIOD_START),
    CHECK (DUE_DATE >= ISSUE_DATE)
);

CREATE TABLE BIL_INVOICE_LINE (  -- Invoice lines (composite key: invoice, line number): recurring fee, usage, roaming, add-on, device instalment.
    INVOICE_ID       INTEGER      NOT NULL REFERENCES BIL_INVOICE (INVOICE_ID),
    LINE_NO          INTEGER      NOT NULL CHECK (LINE_NO > 0),
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    CHARGE_TYPE      VARCHAR(10)  NOT NULL CHECK (CHARGE_TYPE IN ('RECURRING','USAGE','ROAMING','ADDON','DEVICE')),
    DESCRIPTION      VARCHAR(80),
    QUANTITY         NUMERIC(10,2) NOT NULL DEFAULT 1,
    AMOUNT           NUMERIC(12,2) NOT NULL CHECK (AMOUNT >= 0),      -- excluding VAT
    VAT_AMOUNT       NUMERIC(12,2) NOT NULL CHECK (VAT_AMOUNT >= 0),
    PRIMARY KEY (INVOICE_ID, LINE_NO)
);

CREATE TABLE BIL_PAYMENT (  -- Payments received against postpaid accounts. REF_NO is free text keyed by the cashier or the bank file.
    PAYMENT_ID    INTEGER      NOT NULL PRIMARY KEY,
    ACCOUNT_ID    INTEGER      NOT NULL REFERENCES CRM_ACCOUNT (ACCOUNT_ID),
    PAYMENT_DATE  DATE         NOT NULL,
    AMOUNT        NUMERIC(12,2) NOT NULL CHECK (AMOUNT > 0),
    METHOD_CODE   VARCHAR(8)   NOT NULL REFERENCES BIL_PAYMENT_METHOD (METHOD_CODE),
    REF_NO        VARCHAR(20),
    RECEIPT_NO    VARCHAR(16)  NOT NULL UNIQUE,
    STATUS        VARCHAR(10)  NOT NULL CHECK (STATUS IN ('POSTED','REVERSED')),
    POSTED_BY     VARCHAR(20)  NOT NULL
);

CREATE TABLE BIL_ADJUSTMENT (  -- Credit and debit adjustments. This is the only billing table where negative amounts are legitimate.
    ADJUSTMENT_ID       INTEGER      NOT NULL PRIMARY KEY,
    ACCOUNT_ID          INTEGER      NOT NULL REFERENCES CRM_ACCOUNT (ACCOUNT_ID),
    INVOICE_ID          INTEGER      REFERENCES BIL_INVOICE (INVOICE_ID),
    ADJ_TYPE            VARCHAR(10)  NOT NULL CHECK (ADJ_TYPE IN ('CREDIT','DEBIT','GOODWILL','WRITE_OFF')),
    AMOUNT              NUMERIC(12,2) NOT NULL CHECK (AMOUNT <> 0),   -- negative = credit to the customer
    REASON_CODE         VARCHAR(20)  NOT NULL,
    ADJ_DATE            DATE         NOT NULL,
    CREATED_BY_EMP_ID   INTEGER      NOT NULL REFERENCES HR_EMPLOYEE (EMP_ID),
    APPROVED_BY_EMP_ID  INTEGER,                   -- approver; no declared foreign key
    NOTES               VARCHAR(200)
);

CREATE TABLE BIL_DUNNING_EVENT (  -- Collection steps taken on late invoices: reminder, soft bar, hard bar, hand-over to collections.
    DUNNING_ID   INTEGER      NOT NULL PRIMARY KEY,
    INVOICE_ID   INTEGER      NOT NULL REFERENCES BIL_INVOICE (INVOICE_ID),
    STEP_NO      INTEGER      NOT NULL CHECK (STEP_NO BETWEEN 1 AND 4),
    ACTION_CODE  VARCHAR(12)  NOT NULL CHECK (ACTION_CODE IN ('SMS_REMINDER','SOFT_BAR','HARD_BAR','COLLECTIONS')),
    EVENT_DATE   DATE         NOT NULL,
    UNIQUE (INVOICE_ID, STEP_NO)
);

CREATE TABLE BIL_DISPUTE (  -- Disputed invoice lines (composite foreign key to BIL_INVOICE_LINE). TICKET_NO points at the support ticket, undeclared.
    DISPUTE_ID       INTEGER      NOT NULL PRIMARY KEY,
    INVOICE_ID       INTEGER      NOT NULL,
    LINE_NO          INTEGER      NOT NULL,
    TICKET_NO        VARCHAR(14),
    DISPUTED_AMOUNT  NUMERIC(12,2) NOT NULL CHECK (DISPUTED_AMOUNT > 0),
    STATUS           VARCHAR(10)  NOT NULL CHECK (STATUS IN ('OPEN','UPHELD','REJECTED')),
    OPENED_DATE      DATE         NOT NULL,
    RESOLVED_DATE    DATE,
    FOREIGN KEY (INVOICE_ID, LINE_NO) REFERENCES BIL_INVOICE_LINE (INVOICE_ID, LINE_NO)
);

-- ---------------------------------------------------------------------
-- USG_  : usage - calls, SMS, data sessions and recharges
-- ---------------------------------------------------------------------

CREATE TABLE USG_EVENT_TYPE (  -- Usage event types and their rating unit.
    EVENT_TYPE  CHAR(3)      NOT NULL PRIMARY KEY,
    EVENT_NAME  VARCHAR(40)  NOT NULL,
    UNIT        VARCHAR(6)   NOT NULL
);

CREATE TABLE USG_ROAMING_PARTNER (  -- Partner networks abroad, by roaming zone. Partner names and TADIG codes are invented.
    PARTNER_ID       INTEGER      NOT NULL PRIMARY KEY,
    TADIG_CODE       CHAR(5)      NOT NULL UNIQUE,
    PARTNER_NAME     VARCHAR(60)  NOT NULL,
    ZONE_CODE        VARCHAR(8)   NOT NULL REFERENCES PRD_ROAMING_ZONE (ZONE_CODE),
    AGREEMENT_START  DATE
);

CREATE TABLE USG_CDR (  -- Rated usage records (calls, SMS, data sessions): the biggest table. A sample of traffic, not every event. OTHER_PARTY is PERSONAL DATA.
    CDR_ID           INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    EVENT_TYPE       CHAR(3)      NOT NULL REFERENCES USG_EVENT_TYPE (EVENT_TYPE),
    EVENT_START      TIMESTAMP    NOT NULL,
    DURATION_SEC     INTEGER      CHECK (DURATION_SEC >= 0),     -- voice and data
    VOLUME_KB        INTEGER      CHECK (VOLUME_KB >= 0),        -- data only
    DIRECTION        CHAR(2)      CHECK (DIRECTION IN ('MO','MT')),
    OTHER_PARTY      VARCHAR(15),                                -- called / calling number
    CELL_ID          INTEGER,                                    -- serving cell; no declared foreign key; NULL when roaming
    ROAMING_FLAG     CHAR(1)      NOT NULL DEFAULT 'N' CHECK (ROAMING_FLAG IN ('Y','N')),
    ROAM_ZONE_CODE   VARCHAR(8),                                 -- no declared foreign key
    PARTNER_ID       INTEGER      REFERENCES USG_ROAMING_PARTNER (PARTNER_ID),
    CHARGE_AMOUNT    NUMERIC(10,3) NOT NULL DEFAULT 0 CHECK (CHARGE_AMOUNT >= 0),
    SOURCE_FEED      VARCHAR(10)  NOT NULL
);

CREATE TABLE USG_RECHARGE (  -- Prepaid top-ups by channel. AMOUNT includes 15% VAT.
    RECHARGE_ID      INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIPTION_ID  INTEGER      NOT NULL REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    RECHARGE_DT      TIMESTAMP    NOT NULL,
    AMOUNT           NUMERIC(8,2) NOT NULL CHECK (AMOUNT > 0),
    VAT_AMOUNT       NUMERIC(8,2) NOT NULL CHECK (VAT_AMOUNT >= 0),
    CHANNEL          VARCHAR(10)  NOT NULL CHECK (CHANNEL IN ('APP','VOUCHER','DEALER','SHOP','BANK','USSD')),
    VOUCHER_SERIAL   VARCHAR(16),
    DEALER_ID        INTEGER      REFERENCES SAL_DEALER (DEALER_ID),
    BALANCE_AFTER    NUMERIC(10,2) CHECK (BALANCE_AFTER >= 0)
);

CREATE TABLE USG_DAILY_TRAFFIC (  -- Network-wide traffic totals per day and city (composite key), loaded from the mediation summary.
    TRAFFIC_DATE   DATE         NOT NULL,
    CITY_CODE      CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    VOICE_MINUTES  INTEGER      NOT NULL CHECK (VOICE_MINUTES >= 0),
    SMS_COUNT      INTEGER      NOT NULL CHECK (SMS_COUNT >= 0),
    DATA_GB        NUMERIC(12,1) NOT NULL CHECK (DATA_GB >= 0),
    ACTIVE_SUBS    INTEGER      NOT NULL CHECK (ACTIVE_SUBS >= 0),
    PEAK_HOUR      INTEGER      NOT NULL CHECK (PEAK_HOUR BETWEEN 0 AND 23),
    PRIMARY KEY (TRAFFIC_DATE, CITY_CODE)
);

-- ---------------------------------------------------------------------
-- NET_  : network - regions, sites and cells
-- ---------------------------------------------------------------------

CREATE TABLE NET_REGION (  -- Region hierarchy (self-reference): country, five regions, then one area per city.
    REGION_ID         INTEGER      NOT NULL PRIMARY KEY,
    REGION_CODE       VARCHAR(8)   NOT NULL UNIQUE,
    REGION_NAME       VARCHAR(40)  NOT NULL,
    REGION_LEVEL      INTEGER      NOT NULL CHECK (REGION_LEVEL IN (0,1,2)),
    PARENT_REGION_ID  INTEGER      REFERENCES NET_REGION (REGION_ID)
);

CREATE TABLE NET_SITE (  -- Radio sites: location, type and status. Each site carries one or more cells.
    SITE_ID         INTEGER      NOT NULL PRIMARY KEY,
    SITE_CODE       VARCHAR(10)  NOT NULL UNIQUE,
    SITE_NAME       VARCHAR(60)  NOT NULL,
    REGION_ID       INTEGER      NOT NULL REFERENCES NET_REGION (REGION_ID),
    CITY_CODE       CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    SITE_TYPE       VARCHAR(10)  NOT NULL CHECK (SITE_TYPE IN ('MACRO','ROOFTOP','INDOOR','SMALL_CELL')),
    LATITUDE        NUMERIC(9,6) NOT NULL CHECK (LATITUDE BETWEEN 15 AND 33),
    LONGITUDE       NUMERIC(9,6) NOT NULL CHECK (LONGITUDE BETWEEN 34 AND 56),
    TOWER_HEIGHT_M  INTEGER      CHECK (TOWER_HEIGHT_M BETWEEN 3 AND 90),
    ON_AIR_DATE     DATE,
    STATUS          VARCHAR(16)  NOT NULL CHECK (STATUS IN ('ON_AIR','PLANNED','DECOMMISSIONED'))
);

CREATE TABLE NET_CELL (  -- Cells (sectors) on each site, by technology and band.
    CELL_ID      INTEGER      NOT NULL PRIMARY KEY,
    SITE_ID      INTEGER      NOT NULL REFERENCES NET_SITE (SITE_ID),
    CELL_NAME    VARCHAR(16)  NOT NULL UNIQUE,
    TECHNOLOGY   CHAR(2)      NOT NULL CHECK (TECHNOLOGY IN ('3G','4G','5G')),
    BAND_MHZ     INTEGER      NOT NULL,
    SECTOR_NO    INTEGER      NOT NULL CHECK (SECTOR_NO BETWEEN 1 AND 4),
    AZIMUTH_DEG  INTEGER      NOT NULL CHECK (AZIMUTH_DEG BETWEEN 0 AND 359),
    STATUS       VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','LOCKED'))
);

CREATE TABLE NET_FIBRE_CABINET (  -- Street cabinets that fibre home services connect to.
    CABINET_ID         INTEGER      NOT NULL PRIMARY KEY,
    CABINET_CODE       VARCHAR(10)  NOT NULL UNIQUE,
    REGION_ID          INTEGER      NOT NULL REFERENCES NET_REGION (REGION_ID),
    CITY_CODE          CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    DISTRICT           VARCHAR(40),
    CAPACITY_PORTS     INTEGER      NOT NULL CHECK (CAPACITY_PORTS > 0),
    USED_PORTS         INTEGER      NOT NULL CHECK (USED_PORTS >= 0),
    COMMISSIONED_DATE  DATE,
    CHECK (USED_PORTS <= CAPACITY_PORTS)
);

CREATE TABLE NET_OUTAGE (  -- Outage log from the NOC tool. ELEMENT_REF is whatever the engineer typed: usually a site code, sometimes a cabinet code.
    OUTAGE_ID     INTEGER      NOT NULL PRIMARY KEY,
    ELEMENT_REF   VARCHAR(12)  NOT NULL,
    OUTAGE_START  TIMESTAMP    NOT NULL,
    OUTAGE_END    TIMESTAMP,
    SEVERITY      VARCHAR(10)  NOT NULL CHECK (SEVERITY IN ('MINOR','MAJOR','CRITICAL')),
    CAUSE_CODE    VARCHAR(16)  NOT NULL,
    REPORTED_BY   VARCHAR(20),
    CHECK (OUTAGE_END IS NULL OR OUTAGE_END >= OUTAGE_START)
);

CREATE TABLE NET_SITE_KPI_MONTHLY (  -- Monthly KPIs per site (composite key: site, month): availability, traffic, dropped calls.
    SITE_ID           INTEGER      NOT NULL REFERENCES NET_SITE (SITE_ID),
    KPI_MONTH         CHAR(7)      NOT NULL,     -- YYYY-MM
    AVAILABILITY_PCT  NUMERIC(5,2) NOT NULL CHECK (AVAILABILITY_PCT BETWEEN 0 AND 100),
    TRAFFIC_GB        NUMERIC(12,1) NOT NULL CHECK (TRAFFIC_GB >= 0),
    DROP_CALL_RATE    NUMERIC(5,2) NOT NULL CHECK (DROP_CALL_RATE BETWEEN 0 AND 100),
    AVG_USERS         INTEGER      NOT NULL CHECK (AVG_USERS >= 0),
    PRIMARY KEY (SITE_ID, KPI_MONTH)
);

-- ---------------------------------------------------------------------
-- SAL_  : sales - shops, dealers, orders and commissions
-- ---------------------------------------------------------------------

CREATE TABLE SAL_CHANNEL (  -- Sales channels (own shops, dealers, online, telesales).
    CHANNEL_CODE  VARCHAR(10)  NOT NULL PRIMARY KEY,
    CHANNEL_NAME  VARCHAR(40)  NOT NULL,
    IS_DIRECT     CHAR(1)      NOT NULL CHECK (IS_DIRECT IN ('Y','N'))
);

CREATE TABLE SAL_SHOP (  -- Rimal Telecom's own retail shops.
    SHOP_ID         INTEGER      NOT NULL PRIMARY KEY,
    SHOP_CODE       VARCHAR(8)   NOT NULL UNIQUE,
    SHOP_NAME       VARCHAR(60)  NOT NULL,
    CITY_CODE       CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    DISTRICT        VARCHAR(40),
    SHOP_TYPE       VARCHAR(10)  NOT NULL CHECK (SHOP_TYPE IN ('FLAGSHIP','STANDARD','KIOSK')),
    MANAGER_EMP_ID  INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    OPEN_DATE       DATE         NOT NULL,
    CLOSE_DATE      DATE,
    STATUS          VARCHAR(10)  NOT NULL CHECK (STATUS IN ('OPEN','CLOSED'))
);

CREATE TABLE SAL_DEALER (  -- Independent dealers who sell SIMs and top-ups for commission. Dealer names are invented. Contact and IBAN are PERSONAL DATA.
    DEALER_ID       INTEGER      NOT NULL PRIMARY KEY,
    DEALER_CODE     VARCHAR(10)  NOT NULL UNIQUE,
    DEALER_NAME     VARCHAR(80)  NOT NULL,
    CITY_CODE       CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    TIER            CHAR(1)      NOT NULL CHECK (TIER IN ('A','B','C')),
    CONTACT_NAME    VARCHAR(80),
    CONTACT_MSISDN  VARCHAR(15),
    IBAN            CHAR(24),
    ONBOARD_DATE    DATE         NOT NULL,
    STATUS          VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','TERMINATED'))
);

CREATE TABLE SAL_ORDER (  -- Sales orders. SHOP_ID is a declared foreign key but is empty for online, telesales and dealer orders.
    ORDER_ID         INTEGER      NOT NULL PRIMARY KEY,
    ORDER_NO         VARCHAR(14)  NOT NULL UNIQUE,
    CUSTOMER_ID      INTEGER      NOT NULL REFERENCES CRM_CUSTOMER (CUST_ID),
    CHANNEL_CODE     VARCHAR(10)  NOT NULL REFERENCES SAL_CHANNEL (CHANNEL_CODE),
    SHOP_ID          INTEGER      REFERENCES SAL_SHOP (SHOP_ID),
    DEALER_ID        INTEGER      REFERENCES SAL_DEALER (DEALER_ID),
    SALES_EMP_ID     INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    ORDER_DATE       DATE         NOT NULL,
    ORDER_TYPE       VARCHAR(14)  NOT NULL CHECK (ORDER_TYPE IN ('NEW_LINE','SIM_SWAP','DEVICE','PLAN_CHANGE','FIBRE_INSTALL')),
    PLAN_CODE        VARCHAR(12),               -- plan sold; no declared foreign key
    SUBSCRIPTION_ID  INTEGER      REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    TOTAL_AMOUNT     NUMERIC(10,2) NOT NULL CHECK (TOTAL_AMOUNT >= 0),
    STATUS           VARCHAR(10)  NOT NULL CHECK (STATUS IN ('COMPLETED','CANCELLED','PENDING'))
);

CREATE TABLE SAL_ORDER_ITEM (  -- Order lines (composite key: order, item number): SIM, plan, device, add-on or fee.
    ORDER_ID    INTEGER      NOT NULL REFERENCES SAL_ORDER (ORDER_ID),
    ITEM_NO     INTEGER      NOT NULL CHECK (ITEM_NO > 0),
    ITEM_TYPE   VARCHAR(8)   NOT NULL CHECK (ITEM_TYPE IN ('SIM','PLAN','DEVICE','ADDON','FEE')),
    PRODUCT_ID  INTEGER      REFERENCES PRD_PRODUCT (PRODUCT_ID),
    MODEL_ID    INTEGER      REFERENCES PRD_DEVICE_MODEL (MODEL_ID),
    QUANTITY    INTEGER      NOT NULL DEFAULT 1 CHECK (QUANTITY > 0),
    UNIT_PRICE  NUMERIC(10,2) NOT NULL CHECK (UNIT_PRICE >= 0),
    PRIMARY KEY (ORDER_ID, ITEM_NO)
);

CREATE TABLE SAL_COMMISSION (  -- Dealer commissions. SOURCE_DOC is the document the commission was earned on (an order number, or a period code for residuals).
    COMMISSION_ID    INTEGER      NOT NULL PRIMARY KEY,
    DEALER_ID        INTEGER      NOT NULL REFERENCES SAL_DEALER (DEALER_ID),
    PERIOD_MONTH     CHAR(7)      NOT NULL,     -- YYYY-MM
    COMMISSION_TYPE  VARCHAR(12)  NOT NULL CHECK (COMMISSION_TYPE IN ('ACTIVATION','RESIDUAL')),
    SOURCE_DOC       VARCHAR(20),
    AMOUNT           NUMERIC(10,2) NOT NULL CHECK (AMOUNT >= 0),
    STATUS           VARCHAR(12)  NOT NULL CHECK (STATUS IN ('ACCRUED','PAID','CLAWED_BACK')),
    PAID_DATE        DATE
);

CREATE TABLE SAL_SHOP_TARGET (  -- Monthly activation and revenue targets per shop (composite key: shop, month).
    SHOP_ID             INTEGER      NOT NULL REFERENCES SAL_SHOP (SHOP_ID),
    TARGET_MONTH        CHAR(7)      NOT NULL,   -- YYYY-MM
    TARGET_ACTIVATIONS  INTEGER      NOT NULL CHECK (TARGET_ACTIVATIONS >= 0),
    ACTUAL_ACTIVATIONS  INTEGER      NOT NULL CHECK (ACTUAL_ACTIVATIONS >= 0),
    TARGET_REVENUE      NUMERIC(12,2) NOT NULL CHECK (TARGET_REVENUE >= 0),
    PRIMARY KEY (SHOP_ID, TARGET_MONTH)
);

CREATE TABLE SAL_STG_DEALER_ACTIVATION (  -- STAGING: raw dealer activation files as received. No primary key, text columns, unvalidated phone formats, reloaded batches. PERSONAL DATA.
    LOAD_BATCH        VARCHAR(12),
    DEALER_CODE       VARCHAR(10),
    CUSTOMER_NAME     VARCHAR(100),
    ID_NUMBER         VARCHAR(20),
    CONTACT_PHONE     VARCHAR(20),
    MSISDN_ACTIVATED  VARCHAR(20),
    PLAN_CODE         VARCHAR(12),
    ACTIVATION_DT     VARCHAR(20),
    LOAD_TS           VARCHAR(20)
);

-- ---------------------------------------------------------------------
-- SUP_  : support - tickets, complaints and field visits
-- ---------------------------------------------------------------------

CREATE TABLE SUP_TICKET_CATEGORY (  -- Ticket categories with their default priority and SLA.
    CATEGORY_CODE     VARCHAR(12)  NOT NULL PRIMARY KEY,
    CATEGORY_NAME     VARCHAR(60)  NOT NULL,
    DEFAULT_PRIORITY  CHAR(2)      NOT NULL CHECK (DEFAULT_PRIORITY IN ('P1','P2','P3','P4')),
    SLA_HOURS         INTEGER      NOT NULL CHECK (SLA_HOURS > 0)
);

CREATE TABLE SUP_TICKET (  -- Customer service tickets. Includes tickets carried over in the 2019 CRM migration (SOURCE_SYSTEM = 'LEGACY_CRM').
    TICKET_ID        INTEGER      NOT NULL PRIMARY KEY,
    TICKET_NO        VARCHAR(14)  NOT NULL UNIQUE,
    CUSTOMER_ID      INTEGER      REFERENCES CRM_CUSTOMER (CUST_ID),
    SUBSCRIPTION_ID  INTEGER      REFERENCES SUB_SUBSCRIPTION (SUBSCRIPTION_ID),
    CATEGORY_CODE    VARCHAR(12)  NOT NULL REFERENCES SUP_TICKET_CATEGORY (CATEGORY_CODE),
    CHANNEL          VARCHAR(8)   NOT NULL CHECK (CHANNEL IN ('CALL','APP','SHOP','EMAIL','SOCIAL')),
    PRIORITY         CHAR(2)      NOT NULL CHECK (PRIORITY IN ('P1','P2','P3','P4')),
    STATUS           VARCHAR(12)  NOT NULL CHECK (STATUS IN ('OPEN','IN_PROGRESS','RESOLVED','CLOSED')),
    OPENED_DT        TIMESTAMP    NOT NULL,
    CLOSED_DT        TIMESTAMP,
    ASSIGNED_EMP_ID  INTEGER,                   -- handling agent; no declared foreign key
    SUBJECT          VARCHAR(120),
    SOURCE_SYSTEM    VARCHAR(16)  NOT NULL,
    CHECK (CLOSED_DT IS NULL OR CLOSED_DT >= OPENED_DT)
);

CREATE TABLE SUP_TICKET_NOTE (  -- Agent notes on tickets (free text).
    NOTE_ID        INTEGER      NOT NULL PRIMARY KEY,
    TICKET_ID      INTEGER      NOT NULL REFERENCES SUP_TICKET (TICKET_ID),
    NOTE_DT        TIMESTAMP    NOT NULL,
    AUTHOR_EMP_ID  INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    NOTE_TEXT      VARCHAR(500) NOT NULL,
    IS_INTERNAL    CHAR(1)      NOT NULL DEFAULT 'Y' CHECK (IS_INTERNAL IN ('Y','N'))
);

CREATE TABLE SUP_COMPLAINT (  -- Formal complaints escalated from tickets. SERVICE_NO is the number the customer complained about, as typed on the form.
    COMPLAINT_ID      INTEGER      NOT NULL PRIMARY KEY,
    TICKET_ID         INTEGER      NOT NULL REFERENCES SUP_TICKET (TICKET_ID),
    SERVICE_NO        VARCHAR(15),
    COMPLAINT_TYPE    VARCHAR(20)  NOT NULL,
    REGULATOR_REF     VARCHAR(16),
    ESCALATED_DT      TIMESTAMP    NOT NULL,
    RESOLUTION        VARCHAR(20),
    COMPENSATION_AMT  NUMERIC(10,2) CHECK (COMPENSATION_AMT >= 0)
);

CREATE TABLE SUP_FIELD_VISIT (  -- Technician visits for fibre installs and faults.
    VISIT_ID           INTEGER      NOT NULL PRIMARY KEY,
    TICKET_ID          INTEGER      NOT NULL REFERENCES SUP_TICKET (TICKET_ID),
    TECHNICIAN_EMP_ID  INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    ADDRESS_ID         INTEGER      REFERENCES CRM_ADDRESS (ADDRESS_ID),
    SCHEDULED_DT       TIMESTAMP    NOT NULL,
    COMPLETED_DT       TIMESTAMP,
    VISIT_STATUS       VARCHAR(10)  NOT NULL CHECK (VISIT_STATUS IN ('SCHEDULED','COMPLETED','NO_ACCESS','CANCELLED')),
    OUTCOME_CODE       VARCHAR(16),
    TRAVEL_KM          NUMERIC(6,1) CHECK (TRAVEL_KM >= 0)
);

CREATE TABLE SUP_SURVEY (  -- Satisfaction survey answers after a ticket is closed.
    SURVEY_ID     INTEGER      NOT NULL PRIMARY KEY,
    TICKET_ID     INTEGER      NOT NULL UNIQUE REFERENCES SUP_TICKET (TICKET_ID),
    RESPONDED_DT  TIMESTAMP    NOT NULL,
    CSAT_SCORE    INTEGER      NOT NULL CHECK (CSAT_SCORE BETWEEN 1 AND 5),
    NPS_SCORE     INTEGER      CHECK (NPS_SCORE BETWEEN 0 AND 10),
    COMMENT_TEXT  VARCHAR(300)
);

CREATE TABLE SUP_CALL_LOG (  -- Call-centre call log from the legacy telephony platform. SUBSCRIBER_REF is its name for the subscription id; no declared foreign key. CALLER_NO is PERSONAL DATA.
    CALL_ID         INTEGER      NOT NULL PRIMARY KEY,
    SUBSCRIBER_REF  INTEGER,
    CALLER_NO       VARCHAR(15),
    AGENT_EMP_ID    INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),   -- NULL when the call ended in the IVR
    QUEUE_NAME      VARCHAR(20)  NOT NULL,
    CALL_START      TIMESTAMP    NOT NULL,
    WAIT_SEC        INTEGER      NOT NULL CHECK (WAIT_SEC >= 0),
    TALK_SEC        INTEGER      NOT NULL CHECK (TALK_SEC >= 0),
    DISPOSITION     VARCHAR(16)  NOT NULL
);

-- ---------------------------------------------------------------------
-- HR_  : employees, departments and payroll grades
-- ---------------------------------------------------------------------

CREATE TABLE HR_DEPARTMENT (  -- Department tree (self-reference) with cost centres.
    DEPT_ID         INTEGER      NOT NULL PRIMARY KEY,
    DEPT_NAME       VARCHAR(60)  NOT NULL,
    PARENT_DEPT_ID  INTEGER      REFERENCES HR_DEPARTMENT (DEPT_ID),
    COST_CENTRE     CHAR(6)      NOT NULL UNIQUE
);

CREATE TABLE HR_PAY_GRADE (  -- Payroll grades with their monthly basic-salary band in SAR.
    GRADE_CODE  CHAR(3)      NOT NULL PRIMARY KEY,
    GRADE_NAME  VARCHAR(40)  NOT NULL,
    MIN_SALARY  NUMERIC(10,2) NOT NULL CHECK (MIN_SALARY > 0),
    MAX_SALARY  NUMERIC(10,2) NOT NULL,
    CHECK (MAX_SALARY >= MIN_SALARY)
);

CREATE TABLE HR_EMPLOYEE (  -- Employees, with a self-reference to their manager. PERSONAL DATA.
    EMP_ID            INTEGER      NOT NULL PRIMARY KEY,
    EMP_NO            VARCHAR(8)   NOT NULL UNIQUE,
    FIRST_NAME        VARCHAR(40)  NOT NULL,
    LAST_NAME         VARCHAR(40)  NOT NULL,
    NATIONAL_ID       VARCHAR(10)  NOT NULL,
    BIRTH_DATE        DATE         NOT NULL,
    GENDER            CHAR(1)      NOT NULL CHECK (GENDER IN ('M','F')),
    NATIONALITY       CHAR(2)      NOT NULL,
    EMAIL             VARCHAR(80)  NOT NULL UNIQUE,
    MOBILE_NO         VARCHAR(15),
    DEPT_ID           INTEGER      NOT NULL REFERENCES HR_DEPARTMENT (DEPT_ID),
    GRADE_CODE        CHAR(3)      NOT NULL REFERENCES HR_PAY_GRADE (GRADE_CODE),
    JOB_TITLE         VARCHAR(60)  NOT NULL,
    MANAGER_ID        INTEGER      REFERENCES HR_EMPLOYEE (EMP_ID),
    CITY_CODE         CHAR(3)      NOT NULL REFERENCES LKP_CITY (CITY_CODE),
    HIRE_DATE         DATE         NOT NULL,
    TERMINATION_DATE  DATE,
    STATUS            VARCHAR(10)  NOT NULL CHECK (STATUS IN ('ACTIVE','TERMINATED')),
    CHECK (TERMINATION_DATE IS NULL OR TERMINATION_DATE >= HIRE_DATE)
);

CREATE TABLE HR_SALARY_HISTORY (  -- Salary history per employee (composite key: employee, effective date). PERSONAL DATA.
    EMP_ID               INTEGER      NOT NULL REFERENCES HR_EMPLOYEE (EMP_ID),
    EFFECTIVE_DATE       DATE         NOT NULL,
    BASIC_SALARY         NUMERIC(10,2) NOT NULL CHECK (BASIC_SALARY > 0),
    HOUSING_ALLOWANCE    NUMERIC(10,2) NOT NULL CHECK (HOUSING_ALLOWANCE >= 0),
    TRANSPORT_ALLOWANCE  NUMERIC(10,2) NOT NULL CHECK (TRANSPORT_ALLOWANCE >= 0),
    CHANGE_REASON        VARCHAR(20)  NOT NULL,
    PRIMARY KEY (EMP_ID, EFFECTIVE_DATE)
);

CREATE TABLE HR_BANK_ACCOUNT (  -- One-to-one extension of HR_EMPLOYEE: the salary bank account. Bank names are invented. PERSONAL DATA.
    EMP_ID          INTEGER      NOT NULL PRIMARY KEY REFERENCES HR_EMPLOYEE (EMP_ID),
    IBAN            CHAR(24)     NOT NULL,
    BANK_NAME       VARCHAR(40)  NOT NULL,
    ACCOUNT_HOLDER  VARCHAR(80)  NOT NULL,
    VERIFIED_FLAG   CHAR(1)      NOT NULL DEFAULT 'Y' CHECK (VERIFIED_FLAG IN ('Y','N'))
);

CREATE TABLE HR_PAYROLL_RUN (  -- Monthly payroll result per employee (composite key: employee, month). PERSONAL DATA.
    EMP_ID                INTEGER      NOT NULL REFERENCES HR_EMPLOYEE (EMP_ID),
    PAY_MONTH             CHAR(7)      NOT NULL,   -- YYYY-MM
    GROSS_PAY             NUMERIC(10,2) NOT NULL CHECK (GROSS_PAY >= 0),
    SOCIAL_INS_DEDUCTION  NUMERIC(10,2) NOT NULL CHECK (SOCIAL_INS_DEDUCTION >= 0),
    OTHER_DEDUCTIONS      NUMERIC(10,2) NOT NULL CHECK (OTHER_DEDUCTIONS >= 0),
    NET_PAY               NUMERIC(10,2) NOT NULL CHECK (NET_PAY >= 0),
    PAID_DATE             DATE         NOT NULL,
    PRIMARY KEY (EMP_ID, PAY_MONTH)
);

-- ==== INDEXES ====
-- Most foreign keys are indexed. Some are not, on purpose (DATA_DICTIONARY.md lists them):
-- that is how a database looks after ten years of changes.

CREATE INDEX IX_CRM_CUSTOMER_CITY      ON CRM_CUSTOMER (CITY_CODE);
CREATE INDEX IX_CRM_CUSTOMER_SEGMENT   ON CRM_CUSTOMER (SEGMENT_CODE);
CREATE INDEX IX_CRM_CUSTOMER_IDNO      ON CRM_CUSTOMER (ID_NUMBER);
CREATE INDEX IX_CRM_ACCOUNT_CUST       ON CRM_ACCOUNT (CUST_ID);
CREATE INDEX IX_CRM_ADDRESS_CUST       ON CRM_ADDRESS (CUST_ID);
CREATE INDEX IX_CRM_MANDATE_ACCOUNT    ON CRM_BANK_MANDATE (ACCOUNT_ID);
CREATE INDEX IX_CRM_CONSENT_CUSTOMER   ON CRM_MARKETING_CONSENT (CUSTOMER_ID);
CREATE INDEX IX_PRD_PRODUCT_FAMILY     ON PRD_PRODUCT (FAMILY_CODE);
CREATE INDEX IX_PRD_PLAN_PRODUCT       ON PRD_TARIFF_PLAN (PRODUCT_ID);
CREATE INDEX IX_SUB_SUBS_ACCOUNT       ON SUB_SUBSCRIPTION (ACCOUNT_ID);
CREATE INDEX IX_SUB_SUBS_PLAN          ON SUB_SUBSCRIPTION (PLAN_ID);
CREATE UNIQUE INDEX UX_SUB_SUBS_MSISDN ON SUB_SUBSCRIPTION (MSISDN);
CREATE INDEX IX_SUB_SUBS_STATUS        ON SUB_SUBSCRIPTION (STATUS, PAY_TYPE);
CREATE INDEX IX_SUB_SIM_SUBS           ON SUB_SIM_CARD (SUBSCRIPTION_ID);
CREATE INDEX IX_SUB_SIM_ICCID          ON SUB_SIM_CARD (ICCID);          -- not unique: see the note on the column
CREATE INDEX IX_SUB_DEVICE_SUBS        ON SUB_DEVICE (SUBSCRIPTION_ID);
CREATE INDEX IX_SUB_DEVICE_IMEI        ON SUB_DEVICE (IMEI);
CREATE INDEX IX_SUB_HIST_SUBS          ON SUB_STATUS_HISTORY (SUBSCRIPTION_ID);
CREATE INDEX IX_SUB_PLANCHG_SUBS       ON SUB_PLAN_CHANGE (SUBSCRIPTION_ID);
CREATE INDEX IX_BIL_INVOICE_ACCOUNT    ON BIL_INVOICE (ACCOUNT_ID);
CREATE INDEX IX_BIL_INVOICE_ISSUE      ON BIL_INVOICE (ISSUE_DATE);
CREATE INDEX IX_BIL_LINE_SUBS          ON BIL_INVOICE_LINE (SUBSCRIPTION_ID);
CREATE INDEX IX_BIL_PAYMENT_ACCOUNT    ON BIL_PAYMENT (ACCOUNT_ID);
CREATE INDEX IX_BIL_PAYMENT_DATE       ON BIL_PAYMENT (PAYMENT_DATE);
CREATE INDEX IX_BIL_ADJ_ACCOUNT        ON BIL_ADJUSTMENT (ACCOUNT_ID);
CREATE INDEX IX_BIL_DUNNING_INVOICE    ON BIL_DUNNING_EVENT (INVOICE_ID);
CREATE INDEX IX_USG_CDR_SUBS           ON USG_CDR (SUBSCRIPTION_ID);
CREATE INDEX IX_USG_CDR_START          ON USG_CDR (EVENT_START);
CREATE INDEX IX_USG_RECHARGE_SUBS      ON USG_RECHARGE (SUBSCRIPTION_ID);
CREATE INDEX IX_USG_RECHARGE_DT        ON USG_RECHARGE (RECHARGE_DT);
CREATE INDEX IX_NET_SITE_REGION        ON NET_SITE (REGION_ID);
CREATE INDEX IX_NET_CELL_SITE          ON NET_CELL (SITE_ID);
CREATE INDEX IX_SAL_ORDER_CUSTOMER     ON SAL_ORDER (CUSTOMER_ID);
CREATE INDEX IX_SAL_ORDER_SHOP         ON SAL_ORDER (SHOP_ID);
CREATE INDEX IX_SAL_ORDER_DEALER       ON SAL_ORDER (DEALER_ID);
CREATE INDEX IX_SAL_ORDER_DATE         ON SAL_ORDER (ORDER_DATE);
CREATE INDEX IX_SAL_COMMISSION_DEALER  ON SAL_COMMISSION (DEALER_ID);
CREATE INDEX IX_SUP_TICKET_CUSTOMER    ON SUP_TICKET (CUSTOMER_ID);
CREATE INDEX IX_SUP_TICKET_SUBS        ON SUP_TICKET (SUBSCRIPTION_ID);
CREATE INDEX IX_SUP_TICKET_OPENED      ON SUP_TICKET (OPENED_DT);
CREATE INDEX IX_SUP_NOTE_TICKET        ON SUP_TICKET_NOTE (TICKET_ID);
CREATE INDEX IX_SUP_COMPLAINT_TICKET   ON SUP_COMPLAINT (TICKET_ID);
CREATE INDEX IX_SUP_VISIT_TICKET       ON SUP_FIELD_VISIT (TICKET_ID);
CREATE INDEX IX_SUP_CALL_START         ON SUP_CALL_LOG (CALL_START);
CREATE INDEX IX_HR_EMPLOYEE_DEPT       ON HR_EMPLOYEE (DEPT_ID);
CREATE INDEX IX_HR_EMPLOYEE_MANAGER    ON HR_EMPLOYEE (MANAGER_ID);
CREATE INDEX IX_HR_EMPLOYEE_GRADE      ON HR_EMPLOYEE (GRADE_CODE);
CREATE INDEX IX_LKP_CITY_REGION        ON LKP_CITY (REGION_ID);
CREATE INDEX IX_CRM_ADDRESS_CITY       ON CRM_ADDRESS (CITY_CODE);
CREATE INDEX IX_CRM_BIZ_MANAGER        ON CRM_BUSINESS_PROFILE (ACCOUNT_MANAGER_ID);
CREATE INDEX IX_PRD_RATE_ZONE          ON PRD_RATE_CARD (ZONE_CODE);
CREATE INDEX IX_SUB_SUBS_CITY          ON SUB_SUBSCRIPTION (CITY_CODE);
CREATE INDEX IX_SUB_DEVICE_MODEL       ON SUB_DEVICE (MODEL_ID);
CREATE INDEX IX_SUB_ADDON_ADDON        ON SUB_SUBSCRIPTION_ADDON (ADDON_ID);
CREATE INDEX IX_SUB_FIBRE_ADDRESS      ON SUB_FIBRE_SERVICE (ADDRESS_ID);
CREATE INDEX IX_SUB_FIBRE_CABINET      ON SUB_FIBRE_SERVICE (CABINET_ID);
CREATE INDEX IX_BIL_ADJ_INVOICE        ON BIL_ADJUSTMENT (INVOICE_ID);
CREATE INDEX IX_BIL_DISPUTE_LINE       ON BIL_DISPUTE (INVOICE_ID, LINE_NO);
CREATE INDEX IX_USG_RECHARGE_DEALER    ON USG_RECHARGE (DEALER_ID);
CREATE INDEX IX_USG_PARTNER_ZONE       ON USG_ROAMING_PARTNER (ZONE_CODE);
CREATE INDEX IX_NET_CABINET_REGION     ON NET_FIBRE_CABINET (REGION_ID);
CREATE INDEX IX_NET_CABINET_CITY       ON NET_FIBRE_CABINET (CITY_CODE);
CREATE INDEX IX_SAL_SHOP_CITY          ON SAL_SHOP (CITY_CODE);
CREATE INDEX IX_SAL_SHOP_MANAGER       ON SAL_SHOP (MANAGER_EMP_ID);
CREATE INDEX IX_SAL_DEALER_CITY        ON SAL_DEALER (CITY_CODE);
CREATE INDEX IX_SAL_ORDER_SUBS         ON SAL_ORDER (SUBSCRIPTION_ID);
CREATE INDEX IX_SAL_ITEM_PRODUCT       ON SAL_ORDER_ITEM (PRODUCT_ID);
CREATE INDEX IX_SUP_TICKET_CATEGORY    ON SUP_TICKET (CATEGORY_CODE);
CREATE INDEX IX_SUP_VISIT_TECHNICIAN   ON SUP_FIELD_VISIT (TECHNICIAN_EMP_ID);
CREATE INDEX IX_SUP_VISIT_ADDRESS      ON SUP_FIELD_VISIT (ADDRESS_ID);
CREATE INDEX IX_SUP_CALL_AGENT         ON SUP_CALL_LOG (AGENT_EMP_ID);

-- ==== VIEWS ====

CREATE VIEW V_ACTIVE_SUBSCRIBERS AS  -- Active subscriptions with customer, plan and city.
SELECT s.SUBSCRIPTION_ID, s.MSISDN, s.SERVICE_TYPE, s.PAY_TYPE, s.ACTIVATION_DATE,
       p.PLAN_CODE, p.PLAN_NAME,
       c.CUST_ID, c.CUST_TYPE, c.FIRST_NAME || ' ' || c.LAST_NAME AS CUSTOMER_NAME,
       ci.CITY_NAME, r.REGION_NAME
FROM SUB_SUBSCRIPTION s
JOIN CRM_ACCOUNT a      ON a.ACCOUNT_ID = s.ACCOUNT_ID
JOIN CRM_CUSTOMER c     ON c.CUST_ID = a.CUST_ID
JOIN PRD_TARIFF_PLAN p  ON p.PLAN_ID = s.PLAN_ID
JOIN LKP_CITY ci        ON ci.CITY_CODE = s.CITY_CODE
JOIN NET_REGION r       ON r.REGION_ID = ci.REGION_ID
WHERE s.STATUS = 'ACTIVE';

CREATE VIEW V_MONTHLY_REVENUE_BY_REGION AS  -- Invoiced postpaid revenue per issue month and region (SAR).
SELECT substr(i.ISSUE_DATE, 1, 7) AS REVENUE_MONTH,
       r.REGION_NAME,
       COUNT(*)                         AS INVOICE_COUNT,
       ROUND(SUM(i.AMOUNT_EXCL_VAT), 2) AS REVENUE_EXCL_VAT,
       ROUND(SUM(i.VAT_AMOUNT), 2)      AS VAT_AMOUNT,
       ROUND(SUM(i.TOTAL_AMOUNT), 2)    AS REVENUE_INCL_VAT
FROM BIL_INVOICE i
JOIN CRM_ACCOUNT a   ON a.ACCOUNT_ID = i.ACCOUNT_ID
JOIN CRM_CUSTOMER c  ON c.CUST_ID = a.CUST_ID
JOIN LKP_CITY ci     ON ci.CITY_CODE = c.CITY_CODE
JOIN NET_REGION r    ON r.REGION_ID = ci.REGION_ID
GROUP BY substr(i.ISSUE_DATE, 1, 7), r.REGION_NAME;

CREATE VIEW V_OVERDUE_INVOICES AS  -- Unpaid invoices past their due date, with days overdue as of the data end date.
SELECT i.INVOICE_ID, i.INVOICE_NO, i.ACCOUNT_ID, a.CUST_ID, a.CREDIT_CLASS,
       i.ISSUE_DATE, i.DUE_DATE, i.TOTAL_AMOUNT,
       CAST(julianday(d.INFO_VALUE) - julianday(i.DUE_DATE) AS INTEGER) AS DAYS_OVERDUE
FROM BIL_INVOICE i
JOIN CRM_ACCOUNT a  ON a.ACCOUNT_ID = i.ACCOUNT_ID
JOIN SYS_DB_INFO d  ON d.INFO_KEY = 'DATA_END_DATE'
WHERE i.STATUS = 'OVERDUE';

CREATE VIEW V_DAILY_USAGE_SUMMARY AS  -- Usage records per day and event type: events, minutes, megabytes and charges.
SELECT substr(u.EVENT_START, 1, 10) AS USAGE_DATE,
       u.EVENT_TYPE,
       COUNT(*)                                AS EVENT_COUNT,
       ROUND(SUM(u.DURATION_SEC) / 60.0, 1)    AS TOTAL_MINUTES,
       ROUND(SUM(u.VOLUME_KB) / 1024.0, 1)     AS TOTAL_MB,
       ROUND(SUM(u.CHARGE_AMOUNT), 2)          AS TOTAL_CHARGE
FROM USG_CDR u
GROUP BY substr(u.EVENT_START, 1, 10), u.EVENT_TYPE;
