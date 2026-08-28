-- Migration applied 2026-08-28 directly against the live alpaca_hackathon
-- schema (same direct-Postgres pattern as
-- alpaca_hackathon_schema_evolution.sql). Adds the iron condor strategy as
-- a second structure alongside the existing directional credit vertical
-- spread.
--
-- short_strike/long_strike/short_symbol/long_symbol (pre-existing columns)
-- keep meaning "the only side" for a vertical spread, and become
-- specifically "the PUT side" for an iron condor -- the new call_* columns
-- below hold the call side. A vertical spread's row has strategy='vertical'
-- and all call_* columns NULL; an iron condor's row has
-- strategy='iron_condor' and all four legs populated across the two pairs
-- of columns.

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'vertical';

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS call_short_strike NUMERIC;

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS call_long_strike NUMERIC;

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS call_short_symbol TEXT;

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS call_long_symbol TEXT;
