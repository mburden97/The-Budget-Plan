-- Where a holding's dividend yield came from: 'manual' (typed here) or a provider name.
-- A refresh leaves manual figures alone unless the user asks for them to be replaced.
ALTER TABLE holdings ADD COLUMN dividend_source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE holdings ADD COLUMN dividend_as_of TEXT;
