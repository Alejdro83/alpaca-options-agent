-- Incremental backtest ablation lab (2026-08-29) -- see backtest_ablation.py.
-- One row per (run, config) pair; a run is a full pass of L1/L2/L3/L4/random
-- over the current basket + current production defaults. Never overwritten
-- in place -- a new run just inserts a new run_at batch, so the dashboard
-- can show history if this is ever re-run.
create table if not exists alpaca_hackathon.backtest_ablation (
    id bigserial primary key,
    run_at timestamptz not null default now(),
    config text not null,
    n_trades integer not null,
    total_pnl numeric not null,
    avg_pnl numeric,
    win_rate numeric,
    max_drawdown numeric
);

create table if not exists alpaca_hackathon.backtest_ablation_trades (
    id bigserial primary key,
    run_at timestamptz not null default now(),
    config text not null,
    symbol text,
    direction text,
    entry_date date,
    exit_date date,
    credit numeric,
    pnl numeric,
    exit_reason text
);
