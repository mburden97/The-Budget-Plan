-- Subscriptions the user has cancelled: kept off the active list, and any charge that posts
-- after the cancel date is flagged. `pattern` is the itemized subscription's key (a rule's
-- pattern or the merchant), matched against transaction descriptions.
CREATE TABLE cancelled_subscriptions (
    id           INTEGER PRIMARY KEY,
    pattern      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name         TEXT NOT NULL,
    cancelled_on TEXT NOT NULL
);
