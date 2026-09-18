-- Trackers: crypto accounts, emergency fund, essential categories, live prices,
-- transactions (budget vs. actual), categorization rules, CSV import profiles,
-- sinking funds.
--
-- db.migrate() runs this with foreign-key enforcement off (required for the
-- table rebuild below) and runs PRAGMA foreign_key_check before committing.

-- ------------------------------------------------ accounts: rebuild to extend CHECK
CREATE TABLE accounts_new (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    type                 TEXT NOT NULL CHECK (type IN (
                             'cash', 'brokerage', 'retirement', 'crypto', 'property', 'other_asset',
                             'loan', 'credit_card', 'other_liability')),
    is_liability         INTEGER GENERATED ALWAYS AS
                             (type IN ('loan', 'credit_card', 'other_liability')) VIRTUAL,
    institution          TEXT NOT NULL DEFAULT '',
    include_in_net_worth INTEGER NOT NULL DEFAULT 1 CHECK (include_in_net_worth IN (0, 1)),
    emergency_fund       INTEGER NOT NULL DEFAULT 0 CHECK (emergency_fund IN (0, 1)),
    archived             INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    notes                TEXT NOT NULL DEFAULT ''
);
INSERT INTO accounts_new (id, name, type, institution, include_in_net_worth, archived, notes)
    SELECT id, name, type, institution, include_in_net_worth, archived, notes FROM accounts;
DROP TABLE accounts;
ALTER TABLE accounts_new RENAME TO accounts;

-- ------------------------------------------------ categories: essentials drive the emergency fund
ALTER TABLE categories ADD COLUMN essential INTEGER NOT NULL DEFAULT 0 CHECK (essential IN (0, 1));
UPDATE categories SET essential = 1 WHERE name = 'Fixed Expenses' OR kind = 'debt';

-- ------------------------------------------------ holdings: price source + latest price
-- quote_source: 'market' (stocks/ETFs/funds), 'crypto', or 'manual' (cash, private assets)
-- quote_id:     optional provider id override, e.g. CoinGecko 'bitcoin' or Yahoo 'BRK-B'
ALTER TABLE holdings ADD COLUMN quote_source TEXT NOT NULL DEFAULT 'market'
    CHECK (quote_source IN ('market', 'crypto', 'manual'));
ALTER TABLE holdings ADD COLUMN quote_id TEXT NOT NULL DEFAULT '';
ALTER TABLE holdings ADD COLUMN last_price TEXT;
ALTER TABLE holdings ADD COLUMN price_as_of TEXT;

-- ------------------------------------------------ transactions (actual spending / income)
CREATE TABLE transactions (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    posted_on    TEXT NOT NULL,
    description  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,                  -- money in > 0, money out < 0
    line_item_id INTEGER REFERENCES line_items(id) ON DELETE SET NULL,
    excluded     INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),  -- transfers etc.
    notes        TEXT NOT NULL DEFAULT '',
    import_hash  TEXT UNIQUE,                       -- de-duplicates re-imported CSV rows
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX transactions_posted ON transactions(posted_on);
CREATE INDEX transactions_line_item ON transactions(line_item_id);

-- "Description contains <pattern>" -> line item (or exclude as a transfer).
CREATE TABLE category_rules (
    id           INTEGER PRIMARY KEY,
    pattern      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    line_item_id INTEGER REFERENCES line_items(id) ON DELETE CASCADE,
    excluded     INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    CHECK (line_item_id IS NOT NULL OR excluded = 1)
);

-- Remembered CSV column mapping per account (JSON).
CREATE TABLE import_profiles (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    mapping    TEXT NOT NULL
);

-- ------------------------------------------------ sinking funds (save monthly for big bills)
CREATE TABLE sinking_funds (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    due_date     TEXT,
    saved_cents  INTEGER NOT NULL DEFAULT 0 CHECK (saved_cents >= 0),
    line_item_id INTEGER REFERENCES line_items(id) ON DELETE SET NULL,
    notes        TEXT NOT NULL DEFAULT ''
);
