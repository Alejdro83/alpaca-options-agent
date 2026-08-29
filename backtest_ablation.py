"""Incremental backtest ablation lab -- how much does each filter layer
actually contribute? (2026-08-29, prompted by reviewing a teammate's
equivalent build for this same hackathon.)

Same honest-scope caveats as backtest_optimize.py (read its docstring --
Black-Scholes-simulated spread economics on real historical bars/signals/
filters, not real historical option-chain prices; a consistency check
between our own choices, not a market-realistic backtest). This script
reuses ALL of backtest_optimize.py's real logic unmodified (signal scoring,
TrendFilter, volatility filter, Black-Scholes trade simulation) -- it only
restructures the event-collection loop to record entries at FOUR
progressively-filtered stages instead of collecting only the final,
fully-filtered set:

  L1 raw signals       -- any non-neutral swing signal, no trend/vol filter
  L2 + trend filter     -- L1 AND TrendFilter.check(...).allowed
  L3 + vol filter       -- L2 AND the realized-vol-percentile filter
  L4 final rule         -- L3, simulated with CURRENT production defaults
                           (target_delta/dte_range/profit_target_pct) --
                           this is the actual live pipeline, backtested
  L4 random(N seeds)    -- a random subset of L1's raw signal-days, matched
                           trade count to L4's real n_trades, averaged over
                           N seeds -- "did the filter stack + rule beat
                           blind random entry timing on this same basket?"

Each stage keeps its OWN 25-trading-day cooldown-per-symbol tracking (not
shared across stages) -- a stage that skips fewer signals naturally finds
more entry opportunities, and forcing L1's cooldown timing onto L3's sparser
signal stream (or vice versa) would understate one side's true opportunity
set.

Answers the question a bare win-rate number can't: which filter is actually
earning its keep, and does the final rule beat doing nothing more
sophisticated than trading on schedule at random?
"""
from __future__ import annotations

import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/lab-master/trading_bot")
sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

from backtest.data import HistoricalDataLoader  # trading_bot's, unmodified

import black_scholes
from backtest_optimize import (
    BASKET,
    CURRENT_DEFAULTS,
    WARMUP_TRADING_DAYS,
    _passes_volatility_filter,
    _signal_from_df,
    simulate_trade,
)
from signals.adaptive import AdaptiveIndicators
from signals.trend_filter import TrendFilter

COOLDOWN_TRADING_DAYS = 25
RANDOM_SEEDS = 20


def _collect_events(available: dict, adaptive: AdaptiveIndicators, trend_filter: TrendFilter):
    """One pass over the whole basket, classifying every non-neutral signal
    day into which of L1/L2/L3 it reaches -- L2/L3 are strict supersets of
    each earlier stage's *filter pass*, but each stage tracks its own
    cooldown independently (see module docstring), so their raw event
    COUNTS are not simple subsets of one another.
    """
    l1_events: list[tuple] = []
    l2_events: list[tuple] = []
    l3_events: list[tuple] = []

    for symbol, df in available.items():
        cooldown_l1 = cooldown_l2 = cooldown_l3 = -1
        for i in range(WARMUP_TRADING_DAYS, len(df) - 3):
            window = df.iloc[max(0, i - 89): i + 1]
            sig = _signal_from_df(symbol, window, adaptive)
            if sig is None or sig.direction == "neutral":
                continue

            spot = float(df["close"].iloc[i])
            trend_window = df.iloc[: i + 1]
            # Matches backtest_optimize.py's original loop exactly: realized
            # vol is computed on the full up-to-i history (trend_window), not
            # the 90-day signal window -- a real discrepancy caught while
            # writing this ablation, fixed before it could silently produce
            # different vol-filter outcomes than the script this reuses.
            realized_vol = black_scholes.realized_vol_from_bars(trend_window)
            if realized_vol <= 0:
                continue

            if i > cooldown_l1:
                l1_events.append((symbol, i, sig.direction, spot, realized_vol, df))
                cooldown_l1 = i + COOLDOWN_TRADING_DAYS

            try:
                trend_ok = trend_filter.check(trend_window, sig.direction).allowed
            except Exception:
                trend_ok = False
            if trend_ok and i > cooldown_l2:
                l2_events.append((symbol, i, sig.direction, spot, realized_vol, df))
                cooldown_l2 = i + COOLDOWN_TRADING_DAYS

            if trend_ok and _passes_volatility_filter(trend_window) and i > cooldown_l3:
                l3_events.append((symbol, i, sig.direction, spot, realized_vol, df))
                cooldown_l3 = i + COOLDOWN_TRADING_DAYS

    return l1_events, l2_events, l3_events


def _simulate(events, target_delta, dte_range, profit_target_pct):
    """Real gap caught while writing this: simulate_trade() never receives
    `symbol` and always returns SimTrade(symbol="", ...) -- backtest_optimize.py
    has the same gap (it never surfaces per-trade symbols either, only
    aggregates). Fixed here via dataclasses.replace, since this script's
    trade-level detail table would otherwise be useless for spotting which
    names actually drove the P&L.
    """
    import dataclasses

    trades = []
    for symbol, i, direction, spot, rvol, df in events:
        t = simulate_trade(df, i, direction, spot, rvol, target_delta, dte_range, profit_target_pct)
        if t is not None:
            trades.append(dataclasses.replace(t, symbol=symbol))
    return trades


def _summarize(label: str, trades: list) -> dict:
    if not trades:
        return {"config": label, "n_trades": 0, "total_pnl": 0.0, "avg_pnl": None, "win_rate": None, "max_drawdown": 0.0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        running += p
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
    return {
        "config": label,
        "n_trades": len(trades),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(statistics.mean(pnls), 2),
        "win_rate": round(len(wins) / len(pnls), 3),
        "max_drawdown": round(max_dd, 2),
    }


def _record_run(summary: list[dict], sample_trades: list, run_at: datetime) -> None:
    """Persist one full run to Supabase for the dashboard's /api/lab route.
    Best-effort -- a DB hiccup must never stop the console report above,
    same non-fatal convention as bot.py's own db writes."""
    try:
        import psycopg2

        conn = psycopg2.connect(
            host=os.environ["SUPABASE_DB_HOST"], port=os.environ.get("SUPABASE_DB_PORT", 5432),
            dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"), user=os.environ["SUPABASE_DB_USER"],
            password=os.environ["SUPABASE_DB_PASSWORD"], sslmode="require",
        )
        schema = os.environ.get("SUPABASE_SCHEMA", "alpaca_hackathon")
        with conn, conn.cursor() as cur:
            for r in summary:
                cur.execute(
                    f"""insert into {schema}.backtest_ablation
                        (run_at, config, n_trades, total_pnl, avg_pnl, win_rate, max_drawdown)
                        values (%s, %s, %s, %s, %s, %s, %s)""",
                    (run_at, r["config"], r["n_trades"], r["total_pnl"], r["avg_pnl"], r["win_rate"], r["max_drawdown"]),
                )
            for t in sample_trades:
                cur.execute(
                    f"""insert into {schema}.backtest_ablation_trades
                        (run_at, config, symbol, direction, entry_date, exit_date, credit, pnl, exit_reason)
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (run_at, t["config"], t["symbol"], t["direction"], t["entry_date"], t["exit_date"],
                     t["credit"], t["pnl"], t["exit_reason"]),
                )
        conn.close()
        print(f"\nPersisted {len(summary)} config rows + {len(sample_trades)} trade rows to Supabase.")
    except Exception as exc:
        print(f"\nWARNING: failed to persist ablation results to Supabase (non-fatal): {exc}")


def main() -> None:
    print("Loading historical data (Alpaca IEX, via trading_bot's HistoricalDataLoader)...")
    loader = HistoricalDataLoader()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=760)
    data = loader.fetch_all(BASKET, start, end, lookback_days=0)
    available = {s: df.sort_index() for s, df in data.items() if len(df) > WARMUP_TRADING_DAYS + 30}
    print(f"Data loaded for {len(available)}/{len(BASKET)} symbols: {sorted(available)}\n")

    adaptive = AdaptiveIndicators()
    trend_filter = TrendFilter()
    l1, l2, l3 = _collect_events(available, adaptive, trend_filter)
    print(f"L1 raw signal-days: {len(l1)} | L2 + trend filter: {len(l2)} | L3 + vol filter: {len(l3)}\n")

    target_delta, dte_range, profit_target_pct = CURRENT_DEFAULTS

    summary = []
    trades_l1 = _simulate(l1, target_delta, dte_range, profit_target_pct)
    summary.append(_summarize("L1 raw signals", trades_l1))
    trades_l2 = _simulate(l2, target_delta, dte_range, profit_target_pct)
    summary.append(_summarize("L2 + trend filter", trades_l2))
    trades_l3 = _simulate(l3, target_delta, dte_range, profit_target_pct)
    summary.append(_summarize("L3 + vol filter", trades_l3))
    trades_l4 = trades_l3  # L4 IS L3 simulated with the real production defaults -- already done above
    summary.append(_summarize("L4 rule (production defaults)", trades_l4))

    # Random baseline: N seeds of a random subset of L1's raw signal-days,
    # matched trade rate to L4's real n_trades, averaged -- isolates
    # "does the filter stack + rule beat blind random entry timing on this
    # same basket, at this same trade frequency?"
    n_target = len(trades_l4)
    random_totals = []
    random_trades_last: list = []
    for seed in range(RANDOM_SEEDS):
        rng = random.Random(seed)
        sample = rng.sample(l1, min(n_target, len(l1))) if n_target and l1 else []
        trades = _simulate(sample, target_delta, dte_range, profit_target_pct)
        random_totals.append(sum(t.pnl for t in trades))
        random_trades_last = trades  # keep one seed's trades for the detail table
    random_summary = {
        "config": f"L4 random({RANDOM_SEEDS} seeds)",
        "n_trades": n_target,
        "total_pnl": round(statistics.mean(random_totals), 2) if random_totals else 0.0,
        "avg_pnl": None,
        "win_rate": None,
        "max_drawdown": None,
    }
    summary.append(random_summary)

    print(f"{'config':<32} {'n':>5} {'total_pnl':>12} {'avg_pnl':>10} {'win%':>7} {'max_dd':>10}")
    for r in summary:
        win_pct = f"{r['win_rate']*100:.1f}%" if r["win_rate"] is not None else "—"
        avg = f"{r['avg_pnl']:.2f}" if r["avg_pnl"] is not None else "—"
        dd = f"{r['max_drawdown']:.2f}" if r["max_drawdown"] is not None else "—"
        print(f"{r['config']:<32} {r['n_trades']:>5} {r['total_pnl']:>12} {avg:>10} {win_pct:>7} {dd:>10}")

    print(
        f"\nFilter contribution: L1→L2 {summary[1]['total_pnl'] - summary[0]['total_pnl']:+.2f} "
        f"(trend filter), L2→L3 {summary[2]['total_pnl'] - summary[1]['total_pnl']:+.2f} (vol filter), "
        f"L3→L4 {summary[3]['total_pnl'] - summary[2]['total_pnl']:+.2f} (same set, production params -- "
        f"should be $0.00 since L4 IS L3 here; a nonzero value would mean a bug)."
    )
    edge = summary[3]["total_pnl"] - random_summary["total_pnl"]
    print(f"Rule vs random, same trade count ({n_target}): {edge:+.2f} -- the mechanical rule's real edge over blind entry timing.")

    sample_trades = []
    for label, trades in [
        (summary[0]["config"], trades_l1),
        (summary[1]["config"], trades_l2),
        (summary[2]["config"], trades_l3),
        (summary[3]["config"], trades_l4),
        (random_summary["config"], random_trades_last),
    ]:
        for t in trades:
            sample_trades.append({
                "config": label, "symbol": t.symbol, "direction": t.direction,
                "entry_date": t.entry_date, "exit_date": t.exit_date,
                "credit": round(t.credit, 2), "pnl": round(t.pnl, 2), "exit_reason": t.exit_reason,
            })

    _record_run(summary, sample_trades, datetime.now(timezone.utc))

    return summary, random_trades_last


if __name__ == "__main__":
    main()
