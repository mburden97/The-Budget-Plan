-- Conventions:
--   *_cents  INTEGER  money in whole cents (never floats)
--   rates / shares / prices  TEXT  decimal strings, parsed with Decimal
--   dates    TEXT  ISO-8601 'YYYY-MM-DD'

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- budget plan
-- A category groups line items; its kind drives the math (income adds, others spend).
CREATE TABLE categories (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind       TEXT NOT NULL CHECK (kind IN ('income', 'expense', 'savings', 'debt')),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE accounts (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    type                 TEXT NOT NULL CHECK (type IN (
                             'cash', 'brokerage', 'retirement', 'property', 'other_asset',
                             'loan', 'credit_card', 'other_liability')),
    is_liability         INTEGER GENERATED ALWAYS AS
                             (type IN ('loan', 'credit_card', 'other_liability')) VIRTUAL,
    institution          TEXT NOT NULL DEFAULT '',
    include_in_net_worth INTEGER NOT NULL DEFAULT 1 CHECK (include_in_net_worth IN (0, 1)),
    archived             INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    notes                TEXT NOT NULL DEFAULT ''
);

CREATE TABLE line_items (
    id           INTEGER PRIMARY KEY,
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    frequency    TEXT NOT NULL DEFAULT 'monthly' CHECK (frequency IN (
                     'weekly', 'biweekly', 'semimonthly', 'monthly', 'quarterly', 'annual')),
    -- Optional link, e.g. a "Car loan" payment line -> the loan account.
    account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    notes        TEXT NOT NULL DEFAULT '',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX line_items_category ON line_items(category_id);

-- ---------------------------------------------------------------- net worth
-- Balances are always positive; accounts.is_liability decides the sign.
CREATE TABLE balance_snapshots (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of         TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    UNIQUE (account_id, as_of)
);

-- ---------------------------------------------------------------- loans
CREATE TABLE loans (
    account_id               INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    original_principal_cents INTEGER NOT NULL CHECK (original_principal_cents >= 0),
    annual_rate              TEXT NOT NULL,           -- fraction, e.g. '0.0525'
    term_months              INTEGER CHECK (term_months > 0),
    start_date               TEXT,
    min_payment_cents        INTEGER NOT NULL CHECK (min_payment_cents >= 0),
    extra_payment_cents      INTEGER NOT NULL DEFAULT 0 CHECK (extra_payment_cents >= 0),
    payment_day              INTEGER CHECK (payment_day BETWEEN 1 AND 31)
);

CREATE TABLE loan_payments (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES loans(account_id) ON DELETE CASCADE,
    paid_on         TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    principal_cents INTEGER,
    interest_cents  INTEGER,
    notes           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX loan_payments_account ON loan_payments(account_id, paid_on);

-- ---------------------------------------------------------------- brokerage
CREATE TABLE holdings (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol           TEXT NOT NULL COLLATE NOCASE,
    name             TEXT NOT NULL DEFAULT '',
    asset_class      TEXT NOT NULL DEFAULT 'other' CHECK (asset_class IN (
                         'us_stock', 'intl_stock', 'bond', 'cash', 'real_estate', 'crypto', 'other')),
    shares           TEXT NOT NULL,                    -- decimal string
    cost_basis_cents INTEGER,
    UNIQUE (account_id, symbol)
);

-- Manually entered prices (no network access by default).
CREATE TABLE prices (
    symbol TEXT NOT NULL COLLATE NOCASE,
    as_of  TEXT NOT NULL,
    price  TEXT NOT NULL,                              -- decimal string, per share
    PRIMARY KEY (symbol, as_of)
);

-- ---------------------------------------------------------------- seed data
INSERT INTO categories (name, kind, sort_order) VALUES
    ('Income',              'income',  10),
    ('Fixed Expenses',      'expense', 20),
    ('Flexible Expenses',   'expense', 30),
    ('Savings & Investing', 'savings', 40),
    ('Debt Payments',       'debt',    50);
