#!/usr/bin/env python3
"""find_candidates_preview.py — Paco's candidate-search consult (2026-09-04).

Runs the SAME screening pipeline the judged bot's bot.py::find_candidates
uses every cycle -- get_universe (S&P 500 + Nasdaq-100) -> filter_universe
(liquidity/price/market-cap) -> generate_swing_signals -> the trend +
adaptive-volatility filter -- and stops there, before any spread gets
built. Never reimplements any of it; imports the real functions directly,
same reasoning as every other tool here.

This replaces reasoning over the fixed, rotating 19-name Watchlist with a
live scan of the real, much larger universe -- you decide which of the
survivors (if any) are worth a closer look with preview_spread_build /
assess_spread_risk / the regime-classification skill. This script does
NOT classify regime or build a plan for you (those are separate
consults) -- it only tells you who cleared the liquidity/trend/vol bar
this cycle and how strong each signal is, so you spend your reasoning on
which ones deserve a real evaluation, not on re-scanning a list yourself.

Consultative only: read-only market/reference data, no account state, no
orders, no Supabase writes.

Usage: find_candidates_preview.py [MAX_RESULTS]
    MAX_RESULTS: optional, default 15 -- survivors are sorted by signal
    strength descending first, so the default is "the best ones", not an
    arbitrary cutoff.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_REPO_ROOT = "/home/lab-master/alpaca-options-agent"


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def main() -> int:
    max_results = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip() else 15

    try:
        for k, v in _load_env(_ALPACA_ENV).items():
            os.environ[k] = v
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"could not load Alpaca credentials: {exc}"}))
        return 2

    sys.path.insert(0, _REPO_ROOT)
    from alpaca_client import AlpacaClient
    from screening.universe import get_universe
    from screening.filters import filter_universe
    from signals.swing import generate_swing_signals
    from bot import _apply_trend_and_volatility_filters

    client = AlpacaClient()
    universe = get_universe()
    filtered = filter_universe(universe, client)
    tickers = [c.symbol for c in filtered]
    if not tickers:
        print(json.dumps({"error": "filter_universe returned zero candidates -- unusual, check logs"}))
        return 1
    signals = generate_swing_signals(tickers, client)
    survivors = _apply_trend_and_volatility_filters(client, signals)

    results = [
        {
            "ticker": sig.ticker,
            "direction": sig.direction,
            "strength": round(sig.strength, 3),
            "regime": regime_result.regime.value,
            "adx": round(regime_result.adx, 2),
            "vol_ratio": round(regime_result.vol_ratio, 3),
            "realized_vol": round(realized_vol, 4),
        }
        for sig, realized_vol, regime_result in survivors
    ]
    results.sort(key=lambda r: r["strength"], reverse=True)

    print(json.dumps({
        "universe_size": len(universe),
        "passed_liquidity_filter": len(tickers),
        "survivors_total": len(results),
        "showing": min(max_results, len(results)),
        "candidates": results[:max_results],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
