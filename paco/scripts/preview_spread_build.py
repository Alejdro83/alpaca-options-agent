#!/usr/bin/env python3
"""preview_spread_build.py — Paco's spread_builder consult (2026-09-04).

Building a concrete plan (which expiration, which strike pair, real
credit estimate) needs the judged bot's own nearest-to-target-delta
strike selection over a real option chain -- real Black-Scholes delta,
live liquidity/OI filtering, the minimum credit-to-width floor. Exactly
the kind of numeric optimization-over-a-chain-of-data an LLM tends to
approximate rather than get right, same reasoning the
regime-classification skill already applies to ADX/vol_ratio. This
script imports spread_builder.py's real build_spread/build_iron_condor/
build_debit_spread directly -- never reimplements their logic -- so a
preview here can never structurally drift from what the real build
would produce. It fetches its own spot price and realized-vol input
(same black_scholes.realized_vol_from_bars over 400 days of daily bars
the regime-classification skill already uses) so you never need to
supply raw bars/quotes yourself.

Consultative only: prints a plan or an explicit "no plan, because X".
Never places an order, never touches Supabase, never opens a real MCP
connection under any account but your own (ALPACA_API_KEY/SECRET from
scripts/.env, loaded below -- never the judged bot's).

Usage:
    preview_spread_build.py TICKER STRATEGY [DIRECTION] [WIDTH_OVERRIDE]
    STRATEGY: vertical | iron_condor | debit
    DIRECTION: long | short -- required for vertical/debit, ignored
        (omit or pass "-") for iron_condor. Same 'long'/'short' your own
        signal already uses (matches the vendored Signal.direction field).
    WIDTH_OVERRIDE: optional, dollars -- vertical/debit only, iron_condor
        always computes its own width from spot_price.

Example:
    preview_spread_build.py AAPL vertical long
    preview_spread_build.py NVDA iron_condor
    preview_spread_build.py TSLA debit short
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


def _plan_to_dict(plan) -> dict:
    d = {
        "underlying": plan.underlying,
        "direction": plan.direction,
        "expiration": str(plan.expiration),
        "short_strike": plan.short_strike,
        "long_strike": plan.long_strike,
        "short_symbol": plan.short_symbol,
        "long_symbol": plan.long_symbol,
        "credit_estimate": plan.credit_estimate,
        "max_loss": plan.max_loss,
    }
    # IronCondorPlan carries the call side too -- only present on that type.
    for extra in ("call_short_strike", "call_long_strike", "call_short_symbol", "call_long_symbol"):
        if hasattr(plan, extra):
            d[extra] = getattr(plan, extra)
    return d


async def _amain(ticker: str, strategy: str, direction: str | None, width_override: float | None) -> int:
    sys.path.insert(0, _REPO_ROOT)
    from alpaca_client import AlpacaClient
    from alpaca.data.timeframe import TimeFrame
    from datetime import datetime, timedelta, timezone
    import black_scholes
    import pandas as pd
    from mcp_client import AlpacaMCP
    from spread_builder import build_spread, build_debit_spread, build_iron_condor

    client = AlpacaClient()
    quote = client.get_latest_quote(ticker)
    spot_price = (quote["ask_price"] + quote["bid_price"]) / 2
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    bars = client.get_bars(ticker, timeframe=TimeFrame.Day, start=start, limit=500)
    if len(bars) < 30:
        print(json.dumps({"error": f"only {len(bars)} daily bars for {ticker}, can't estimate realized vol"}))
        return 1
    bars_df = pd.DataFrame(bars)
    realized_vol = black_scholes.realized_vol_from_bars(bars_df)

    async with AlpacaMCP() as mcp:
        if strategy == "iron_condor":
            plan = await build_iron_condor(mcp, ticker, spot_price=spot_price, realized_vol=realized_vol)
        elif strategy == "debit":
            plan = await build_debit_spread(mcp, ticker, direction, spot_price=spot_price, realized_vol=realized_vol)
        elif strategy == "vertical":
            plan = await build_spread(
                mcp, ticker, direction, spot_price=spot_price, realized_vol=realized_vol,
                width_override=width_override,
            )
        else:
            print(json.dumps({"error": f"unknown strategy {strategy!r}, use vertical|iron_condor|debit"}))
            return 1

    if plan is None:
        print(json.dumps({
            "ticker": ticker, "strategy": strategy, "plan": None,
            "note": "no plan -- see stderr/log for the specific reason "
                    "(no liquid chain in the DTE window, non-positive credit, "
                    "or below the min-credit-to-width floor)",
        }))
        return 0

    print(json.dumps({
        "ticker": ticker, "strategy": strategy, "spot_price": round(spot_price, 2),
        "realized_vol": round(realized_vol, 4), "plan": _plan_to_dict(plan),
    }))
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: preview_spread_build.py TICKER STRATEGY [DIRECTION] [WIDTH_OVERRIDE]"}))
        return 1
    ticker = sys.argv[1].strip().upper()
    strategy = sys.argv[2].strip().lower()
    direction = sys.argv[3].strip().lower() if len(sys.argv) > 3 and sys.argv[3].strip() not in ("", "-") else None
    width_override = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].strip() else None

    if strategy in ("vertical", "debit") and direction not in ("long", "short"):
        print(json.dumps({"error": f"strategy={strategy!r} needs DIRECTION long|short"}))
        return 1

    try:
        for k, v in _load_env(_ALPACA_ENV).items():
            os.environ[k] = v
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"could not load Alpaca credentials: {exc}"}))
        return 2
    # mcp_client.AlpacaMCP spawns the real MCP server via `uvx` on PATH --
    # only present here (not on the invoking shell's own PATH) inside the
    # judged bot's venv, since only its run_options_cron.sh sources it.
    # Prepend it rather than requiring every caller of this script to
    # remember to `source .venv/bin/activate` first.
    venv_bin = str(Path(_REPO_ROOT) / ".venv" / "bin")
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")

    import asyncio
    return asyncio.run(_amain(ticker, strategy, direction, width_override))


if __name__ == "__main__":
    sys.exit(main())
