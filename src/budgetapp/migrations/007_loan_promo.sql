-- End of a promotional rate (e.g. a 0% store-card promo). If it's a deferred-interest promo,
-- any balance left on that date can be charged interest back to the purchase date, so the
-- app checks whether the planned payment clears the balance in time.
ALTER TABLE loans ADD COLUMN promo_ends_on TEXT;
