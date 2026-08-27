"""Sanity checks for backtest_optimize.py's simulation engine -- written
2026-08-27 AFTER the first optimization run had already been acted on
(short_leg_target_delta bumped 0.17 -> 0.20), which was the wrong order:
these checks should have gated that decision, not followed it. Running
them caught a real, previously-undisclosed problem (see check 1 below)
that reversed the 0.20 recommendation. Kept in the repo so this ordering
mistake isn't repeated on the next tuning pass -- run this FIRST.

Exit code 0 and "ALL CHECKS PASSED" means the trade-lifecycle mechanics
(profit target / stop / decay / bear-call mirror) are sound. It does NOT
mean the strike-selection proxy is precise -- check 1 measures exactly how
imprecise it is, and that number should be re-read before trusting any
fine-grained parameter comparison (e.g. 0.17 vs 0.20 delta) out of
backtest_optimize.py's results table.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

from backtest_optimize import bs_price, strike_for_delta, simulate_trade
import black_scholes


def check_strike_delta_roundtrip() -> float:
    """For a strike picked by strike_for_delta(target), does bs_delta on
    that strike actually come back near `target`? Only exact if strikes
    were continuous; real (and our proxy) strikes are rounded to $1, so
    some error is expected -- this measures how much, across the actual
    production DTE midpoints and a realistic spot/vol range, so the
    number is grounded rather than assumed small.
    """
    dte_midpoints = {"(7,14)": 10, "(10,21)": 15, "(14,30)": 22}
    worst = 0.0
    worst_detail = None
    for target in (0.15, 0.17, 0.20, 0.25):
        for opt in ("put", "call"):
            for label, dte in dte_midpoints.items():
                for spot in (30, 40, 60, 100, 150, 250, 400, 600):
                    for vol in (0.20, 0.25, 0.30, 0.35, 0.45):
                        k = strike_for_delta(spot=spot, target_delta=target, dte_days=dte,
                                              volatility=vol, option_type=opt)
                        d = abs(black_scholes.bs_delta(spot=spot, strike=k, dte_days=dte,
                                                        volatility=vol, option_type=opt))
                        rel_err = abs(d - target) / target
                        if rel_err > worst:
                            worst = rel_err
                            worst_detail = (target, opt, label, spot, vol, k, round(d, 3))
    print(f"  worst relative delta error: {worst:.1%}  (target={worst_detail[0]} {worst_detail[1]} "
          f"dte_bucket={worst_detail[2]} spot={worst_detail[3]} vol={worst_detail[4]} "
          f"-> strike={worst_detail[5]} actual_delta={worst_detail[6]})")
    print("  -> this is the $1-strike-rounding proxy's real error budget. Any parameter-grid")
    print("     difference smaller than this (e.g. 0.17 vs 0.20 delta) is NOT distinguishable")
    print("     from noise and should not be acted on without a finer proxy or real chain data.")
    return worst


def _flat_df(n=40, price=100.0):
    dates = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame({"close": [price] * n, "high": [price] * n, "low": [price] * n,
                          "open": [price] * n, "volume": [1_000_000] * n}, index=dates)


def _trend_df(n=40, start=100.0, end=115.0, flat_days=5):
    dates = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    prices = [start] * flat_days + list(np.linspace(start, end, n - flat_days))
    return pd.DataFrame({"close": prices, "high": prices, "low": prices, "open": prices,
                          "volume": [1_000_000] * n}, index=dates)


def check_lifecycle() -> bool:
    ok = True

    t = simulate_trade(_trend_df(end=115.0), 4, "long", 100.0, 0.25, 0.20, (10, 21), 0.50)
    ok &= t is not None and t.pnl > 0 and t.exit_reason == "profit_target"
    print(f"  strong favorable move -> profit_target: {t.exit_reason if t else None}, "
          f"pnl={t.pnl if t else None} {'OK' if t and t.pnl > 0 and t.exit_reason == 'profit_target' else 'FAIL'}")

    t = simulate_trade(_trend_df(end=80.0), 4, "long", 100.0, 0.25, 0.20, (10, 21), 0.50)
    ok &= t is not None and t.pnl < 0 and t.exit_reason == "stop"
    print(f"  strong adverse move -> stop: {t.exit_reason if t else None}, "
          f"pnl={t.pnl if t else None} {'OK' if t and t.pnl < 0 and t.exit_reason == 'stop' else 'FAIL'}")

    t = simulate_trade(_flat_df(), 4, "long", 100.0, 0.25, 0.20, (10, 21), 0.50)
    ok &= t is not None and t.pnl > 0
    print(f"  flat price -> theta-decay profit: pnl={t.pnl if t else None} "
          f"{'OK' if t and t.pnl > 0 else 'FAIL'}")

    t = simulate_trade(_trend_df(end=85.0), 4, "short", 100.0, 0.25, 0.20, (10, 21), 0.50)
    ok &= t is not None and t.pnl > 0
    print(f"  bear call spread profits on a drop: pnl={t.pnl if t else None} "
          f"{'OK' if t and t.pnl > 0 else 'FAIL'}")

    return ok


def check_price_sanity() -> bool:
    p = bs_price(spot=90, strike=110, dte_days=0.5, volatility=0.25, option_type="put")
    ok = abs(p - 20.0) < 0.05
    print(f"  deep-ITM put near expiry ~ intrinsic (20.00): got {p:.2f} {'OK' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    print("=== 1. Strike/delta round-trip error budget ===")
    worst_err = check_strike_delta_roundtrip()
    print("\n=== 2. Black-Scholes price sanity ===")
    price_ok = check_price_sanity()
    print("\n=== 3. Trade lifecycle (profit target / stop / decay / bear-call mirror) ===")
    lifecycle_ok = check_lifecycle()

    print("\n=== Summary ===")
    print(f"  lifecycle mechanics: {'PASS' if lifecycle_ok and price_ok else 'FAIL'}")
    print(f"  strike-rounding error budget: {worst_err:.1%} relative (informational, not pass/fail --")
    print(f"  treat any grid comparison finer than this as unreliable)")
    sys.exit(0 if (lifecycle_ok and price_ok) else 1)
