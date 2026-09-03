#!/usr/bin/env python3
"""classify_regime_batch.py — batch regime classification for Paco (2026-09-03).

Real problem found live today: the regime-classification skill (see
../../shared/skills/paco_trading/regime-classification/SKILL.md, pre-fix
version) had Paco call `get_stock_bars` per candidate over MCP -- landing
~400 days of raw bars in the LLM's own context -- then retype that same
blob into a shell heredoc to pipe into classify_regime.py. With 3
candidates a cycle (next_watchlist_batch.py), that is three full
fetch-then-retype round trips inside one turn. Traced against real cycles
from today (2026-09-03): 6 of ~66 wrapper-logged cycles hit run_cycle_paco.py's
300s subprocess timeout and got SIGKILLed, another 21 found the lock still
held by the previous (still-running) cycle -- 27 of 66 ticks (41%) lost to
this, not to a real "no good trade" judgment, and zero real trades placed
all session as of the fix.

This script does the same fetch (AlpacaClient.get_bars, same IEX-feed
fix as the judged bot's own path) and the same classification
(signals.regime.RegimeDetector, same module the judged bot's bot.py
calls -- see classify_regime.py's docstring for why that specific reuse
matters) for MULTIPLE symbols in ONE process, entirely outside the LLM's
context -- Paco never sees a single raw bar, only the compact JSON result
below, once, for the whole batch.

Credentials: Paco's OWN Alpaca account, via mcp_risk_proxy/.env -- NEVER
alpaca-options-agent/.env (that is the judged bot's separate account; see
reconcile_paco.py's docstring for why the two must never cross). Same
_load_env + os.environ override pattern reconcile_paco.py already uses
successfully to import alpaca_client.AlpacaClient under Paco's own keys.

Usage: comma-separated symbols, exactly what next_watchlist_batch.py
prints:
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/classify_regime_batch.py \\
        AAPL,MSFT,NVDA

Prints ONE JSON object per line, one per symbol (order preserved):
    {"symbol": "AAPL", "regime": "ranging", "adx": 16.95, "vol_20d": 0.0298,
     "vol_60d_avg": 0.0258, "vol_ratio": 1.155, "is_high_vol": false,
     "is_strong_trend": false}
or on a per-symbol failure (never aborts the rest of the batch):
    {"symbol": "XYZ", "error": "..."}
Exit code is always 0 if the batch ran at all (check each line's own
"error" key) -- nonzero only if credentials/imports failed before any
symbol could be attempted.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_MIN_ROWS = 60 + 14  # 60-day vol baseline + 14-day ADX warmup, same floor as classify_regime.py


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
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print(json.dumps({"error": "usage: classify_regime_batch.py SYM1,SYM2,SYM3"}))
        return 1
    symbols = [s.strip().upper() for s in sys.argv[1].split(",") if s.strip()]

    try:
        for k, v in _load_env(_ALPACA_ENV).items():
            os.environ[k] = v
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"could not load Alpaca credentials: {exc}"}))
        return 2

    sys.path.insert(0, "/home/lab-master/alpaca-options-agent")
    try:
        import pandas as pd
        from datetime import datetime, timedelta, timezone
        from alpaca.data.timeframe import TimeFrame
        from alpaca_client import AlpacaClient
        from signals.regime import RegimeDetector
    except Exception as exc:
        print(json.dumps({"error": f"import failed: {exc}"}))
        return 2

    try:
        client = AlpacaClient()
    except Exception as exc:
        print(json.dumps({"error": f"AlpacaClient init failed: {exc}"}))
        return 2

    detector = RegimeDetector()
    # `start` controls the actual lookback window -- `limit` alone does NOT
    # (real pitfall the old per-ticker skill flagged: limit=260 with no
    # start returned only 1-3 bars, because start defaults to "just now").
    # 400 calendar days comfortably covers the 74 trading-day floor below
    # including weekends/holidays.
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    for symbol in symbols:
        try:
            bars = client.get_bars(symbol, timeframe=TimeFrame.Day, start=start, limit=500)
            if len(bars) < _MIN_ROWS:
                print(json.dumps({
                    "symbol": symbol,
                    "error": f"only {len(bars)} bars, need at least {_MIN_ROWS} "
                             "(60-day vol baseline + 14-day ADX warmup)",
                }))
                continue
            df = pd.DataFrame(bars)
            result = detector.detect(df)
            print(json.dumps({
                "symbol": symbol,
                "regime": result.regime.value,
                "adx": float(result.adx),
                "vol_20d": float(result.vol_20d),
                "vol_60d_avg": float(result.vol_60d_avg),
                "vol_ratio": float(result.vol_ratio),
                # numpy bool_, not a native Python bool -- json.dumps rejects it
                "is_high_vol": bool(result.is_high_vol),
                "is_strong_trend": bool(result.is_strong_trend),
            }))
        except Exception as exc:
            # Never let one bad symbol abort the rest of the batch -- the
            # whole point is Paco gets a usable result for every candidate
            # it can, not an all-or-nothing failure.
            print(json.dumps({"symbol": symbol, "error": str(exc)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
