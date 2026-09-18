-- How a loan's required minimum is worked out: a fixed amount (min_payment_cents, as
-- before), a percent of the balance (min_payment_percent, a fraction such as '0.03'), or
-- none at all (e.g. a personal loan). What is actually paid each month is unchanged:
-- min_payment_cents + extra_payment_cents.
ALTER TABLE loans ADD COLUMN min_rule TEXT NOT NULL DEFAULT 'fixed'
    CHECK (min_rule IN ('fixed', 'percent', 'none'));
ALTER TABLE loans ADD COLUMN min_payment_percent TEXT;
