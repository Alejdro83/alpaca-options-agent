-- Beta-weighted delta (2026-08-29) -- see portfolio_greeks.py. "This
-- portfolio moves like N shares of SPY", computed from real trailing daily
-- returns per underlying, never a hardcoded beta table.
alter table alpaca_hackathon.portfolio_greeks_snapshots
    add column if not exists beta_weighted_delta numeric;
