-- Dividends per holding: the yearly yield (a fraction, e.g. '0.0325') and whether it is
-- reinvested (a DRIP buys more shares) or paid out as cash. The projection compounds the
-- reinvested part into the investments and adds the rest to cash.
ALTER TABLE holdings ADD COLUMN dividend_yield TEXT;
ALTER TABLE holdings ADD COLUMN reinvest INTEGER NOT NULL DEFAULT 1 CHECK (reinvest IN (0, 1));
