-- Migration applied 2026-09-02 directly against the live alpaca_hackathon
-- schema (same direct-Postgres pattern as
-- alpaca_hackathon_schema_evolution.sql / alpaca_hackathon_schema_iron_condor.sql).
-- Adds the directional DEBIT vertical as a second premium convention
-- alongside the existing credit vertical / iron condor -- see
-- spread_builder.build_debit_spread and SpreadPlan's own docstring for the
-- full sign/role convention this column distinguishes.
--
-- For structure='debit': credit_received is NEGATIVE (the debit paid, not
-- received), and short_strike/short_symbol mean the leg this project
-- BOUGHT rather than sold (the opposite of what those columns mean for
-- structure='credit'). max_loss stays uniformly positive either way (the
-- worst-case dollar loss), so nothing downstream that only reads max_loss
-- (risk_gate concentration/cluster caps, position sizing) needed any
-- schema change at all.

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS structure TEXT NOT NULL DEFAULT 'credit';
