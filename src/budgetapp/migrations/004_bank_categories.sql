-- The bank's own category for a transaction (e.g. Chase "Groceries"), kept from CSV
-- imports, plus a user-chosen mapping from bank category -> budget line.
ALTER TABLE transactions ADD COLUMN bank_category TEXT;

CREATE TABLE bank_category_map (
    bank_category TEXT PRIMARY KEY COLLATE NOCASE,
    line_item_id  INTEGER REFERENCES line_items(id) ON DELETE CASCADE,
    excluded      INTEGER NOT NULL DEFAULT 0 CHECK (excluded IN (0, 1)),
    CHECK (line_item_id IS NOT NULL OR excluded = 1)
);
