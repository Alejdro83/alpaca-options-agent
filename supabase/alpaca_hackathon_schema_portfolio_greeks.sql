-- Real broker-computed portfolio Greeks (2026-08-29) -- see
-- portfolio_greeks.py. Monitoring only: net delta/gamma/theta/vega/rho
-- across all open spreads, using REAL greeks Alpaca returns for held
-- positions (confirmed live -- null for anything not currently held,
-- which is why candidate selection still uses black_scholes.py's proxy).
create table if not exists alpaca_hackathon.portfolio_greeks_snapshots (
    id bigserial primary key,
    snapshot_at timestamptz not null default now(),
    net_delta numeric,
    net_gamma numeric,
    net_theta numeric,
    net_vega numeric,
    net_rho numeric,
    -- Per-spread breakdown: [{spread_id, underlying, strategy, delta, gamma, theta, vega, rho}, ...]
    per_spread jsonb
);
