-- Migration applied 2026-08-29 directly against the live alpaca_hackathon
-- schema (same direct-Postgres pattern as
-- alpaca_hackathon_schema_evolution.sql). Adds the shadow book's virtual
-- position tracking table — two counterfactual policies (shadow rule and
-- random) evaluated on the same gate-approved candidate menu the real
-- cycle saw, without ever touching Alpaca for orders.
--
-- Mirrors the `spreads` table closely enough to reuse the same mental
-- model, plus a `policy` column ('shadow' | 'random'), a `same_as_llm`
-- boolean (true if this ticker was ALSO what the LLM picked), and an
-- `unrealized_mark` for per-cycle mark-to-market. Both vertical fields
-- (short_strike, long_strike, short_symbol, long_symbol) and iron-condor
-- call-side fields (call_short_strike, call_long_strike, call_short_symbol,
-- call_long_symbol, all nullable) are present — branch on `strategy` to
-- know which set to use.

CREATE TABLE IF NOT EXISTS alpaca_hackathon.shadow_positions (
    id serial primary key,
    -- 'shadow' (mechanical rule) or 'random' (seeded RNG, matched trade count)
    policy text not null,
    -- 'vertical' | 'iron_condor' — same values as spreads.strategy
    strategy text not null default 'vertical',
    underlying text not null,
    -- 'bull_put' | 'bear_call' for vertical; NULL for iron condor (no
    -- directional signal by construction — same as find_candidates() which
    -- sets direction=None for iron_condor candidates)
    direction text,
    expiration date not null,
    cycle_id integer,
    -- Vertical leg fields (PUT side for iron condors)
    short_strike numeric not null,
    long_strike numeric not null,
    short_symbol text not null,
    long_symbol text not null,
    -- Iron condor call-side fields (NULL for verticals)
    call_short_strike numeric,
    call_long_strike numeric,
    call_short_symbol text,
    call_long_symbol text,
    contracts integer not null default 1,
    credit_received numeric not null,
    max_loss numeric not null,
    -- 'open' | 'closed_profit' | 'closed_stop' | 'closed_expiry'
    status text not null default 'open',
    realized_pnl numeric,
    unrealized_mark numeric,
    -- true if this ticker was also what the LLM picked this cycle
    same_as_llm boolean not null default false,
    opened_at timestamptz not null default now(),
    closed_at timestamptz
);

-- Index for the manage_open() query that fetches all open positions
CREATE INDEX IF NOT EXISTS idx_shadow_positions_open
    ON alpaca_hackathon.shadow_positions (status)
    WHERE status = 'open';

-- Index for the dashboard's per-policy aggregate queries
CREATE INDEX IF NOT EXISTS idx_shadow_positions_policy_status
    ON alpaca_hackathon.shadow_positions (policy, status);
