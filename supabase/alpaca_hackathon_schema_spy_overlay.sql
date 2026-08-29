-- SPY benchmark overlay (2026-08-29) -- see bot.py's record_account_snapshot
-- call and dashboard/src/components/EquitySparkline.tsx for the consumer.
-- A synthetic, non-capital-consuming shadow benchmark: lets the dashboard
-- show "skill vs market" instead of a bare equity curve a rising tape alone
-- could explain.
alter table alpaca_hackathon.account_snapshots
    add column if not exists spy_price numeric;
