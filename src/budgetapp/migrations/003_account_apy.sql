-- Interest rate for savings / cash accounts: annual percentage yield as a fraction
-- ('0.03' = 3.00% APY). NULL means none recorded.
ALTER TABLE accounts ADD COLUMN apy TEXT;
