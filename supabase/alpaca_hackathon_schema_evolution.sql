-- Migration applied 2026-08-28 directly against the live alpaca_hackathon
-- schema (via db._connection(), not tracked here before now — see db.py's
-- module docstring for why this project uses direct Postgres rather than
-- PostgREST). Adds what the evolution audit trail / auto-revert (Layer 1 +
-- Layer 2, see pendientes.md) needs: which parameter generation was active
-- for a given cycle/spread, and a persistent, append-only history of every
-- promotion/hold/revert decision overnight_evolution.py has made.

ALTER TABLE alpaca_hackathon.cycles
    ADD COLUMN IF NOT EXISTS generation integer NOT NULL DEFAULT 0;

ALTER TABLE alpaca_hackathon.spreads
    ADD COLUMN IF NOT EXISTS generation integer NOT NULL DEFAULT 0;

-- generation 0 == "no evolved params yet, running on config.py defaults".

CREATE TABLE IF NOT EXISTS alpaca_hackathon.evolution_history (
    id serial primary key,
    generation integer not null,
    ran_at timestamptz not null default now(),
    -- 'promoted' | 'held' | 'auto_reverted' | 'manual_revert'
    decision text not null,
    params_before jsonb,
    params_after jsonb,
    reason text not null,
    simulated_metrics jsonb,
    real_metrics jsonb,
    reverted_at timestamptz,
    reverted_reason text
);
