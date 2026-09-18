-- Split one transaction across several budget lines. A split takes part of the amount to
-- another line (same sign as the transaction); the rest stays on the transaction's own line.
CREATE TABLE transaction_splits (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    line_item_id   INTEGER NOT NULL REFERENCES line_items(id) ON DELETE CASCADE,
    amount_cents   INTEGER NOT NULL,
    note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX transaction_splits_transaction ON transaction_splits(transaction_id);

-- A rule can also carve a fixed amount of every matching charge into another line
-- (e.g. part of each phone bill is a cloud-storage plan).
ALTER TABLE category_rules ADD COLUMN split_line_item_id INTEGER
    REFERENCES line_items(id) ON DELETE SET NULL;
ALTER TABLE category_rules ADD COLUMN split_amount_cents INTEGER;
ALTER TABLE category_rules ADD COLUMN split_note TEXT NOT NULL DEFAULT '';

-- What each budget line really received: every transaction's remainder plus its splits.
-- Money totals per account still come from `transactions`; per-line totals from here.
CREATE VIEW allocations AS
SELECT t.id AS transaction_id, t.posted_on, t.account_id, t.description, t.excluded,
       t.line_item_id,
       t.amount_cents - COALESCE((SELECT SUM(s.amount_cents) FROM transaction_splits s
                                  WHERE s.transaction_id = t.id), 0) AS amount_cents,
       0 AS is_split
FROM transactions t
UNION ALL
SELECT t.id, t.posted_on, t.account_id,
       CASE WHEN s.note != '' THEN s.note ELSE t.description END,
       t.excluded, s.line_item_id, s.amount_cents, 1
FROM transaction_splits s JOIN transactions t ON t.id = s.transaction_id;
