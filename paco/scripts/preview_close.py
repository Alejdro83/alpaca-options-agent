#!/usr/bin/env python3
"""preview_close.py — Paco's risk_gate.should_close consult (2026-09-04).

Whether a position has hit its profit target or stop is a real-number
comparison (cost-to-close vs. credit_received * a threshold, or the
mirror-image proceeds formula for a debit spread) -- exactly the kind of
arithmetic AGENTS.md step 12 currently has you do in your head, with no
tool backing it the way opening a position already has (assess_spread_risk).
This script imports risk_gate.should_close / is_near_stop /
should_force_close directly -- the judged bot's own real close logic,
never reimplemented -- and fetches the live mark itself via
executor_mcp.get_spread_mark / get_iron_condor_mark, so the number you
see is the same real quote-based mark the judged bot would act on too.

Consultative only: tells you what the real trigger says. Never places an
order, never touches Supabase. You still decide whether to act on it --
same as assess_spread_risk on the entry side.

Usage:
    preview_close.py STRATEGY STRUCTURE CREDIT_RECEIVED EXPIRATION \\
        SHORT_SYMBOL LONG_SYMBOL [CALL_SHORT_SYMBOL CALL_LONG_SYMBOL]

    STRATEGY: vertical | iron_condor
    STRUCTURE: credit | debit (iron_condor is always credit)
    CREDIT_RECEIVED: the real number from when you opened it (per-contract,
        dollars -- NEGATIVE for a debit spread, matching AGENTS.md step 13's
        own storage convention)
    EXPIRATION: YYYY-MM-DD
    SHORT_SYMBOL / LONG_SYMBOL: the put-side (or only) legs
    CALL_SHORT_SYMBOL / CALL_LONG_SYMBOL: iron_condor only, the call side

Example:
    preview_close.py vertical credit 82.00 2026-09-11 \\
        AAPL260911P00320000 AAPL260911P00315000
    preview_close.py iron_condor credit 145.00 2026-09-18 \\
        SPY260918P00560000 SPY260918P00555000 SPY260918C00600000 SPY260918C00605000
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
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


async def _amain(strategy: str, structure: str, credit_received: float, expiration: str,
                  short_symbol: str, long_symbol: str,
                  call_short_symbol: str | None, call_long_symbol: str | None) -> int:
    sys.path.insert(0, _REPO_ROOT)
    import risk_gate
    from executor_mcp import get_spread_mark, get_iron_condor_mark
    from mcp_client import AlpacaMCP

    exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
    is_credit = structure != "debit"

    async with AlpacaMCP() as mcp:
        if strategy == "iron_condor":
            if not (call_short_symbol and call_long_symbol):
                print(json.dumps({"error": "iron_condor needs CALL_SHORT_SYMBOL and CALL_LONG_SYMBOL"}))
                return 1
            mark = await get_iron_condor_mark(
                mcp, short_put_symbol=short_symbol, long_put_symbol=long_symbol,
                short_call_symbol=call_short_symbol, long_call_symbol=call_long_symbol,
            )
            width = None
        else:
            mark = await get_spread_mark(mcp, short_symbol, long_symbol, structure=structure)
            width = None
            if not is_credit:
                # should_close's debit branch needs width in dollars to know
                # the spread's own max possible gain -- derive it from the
                # two strikes embedded in the OCC symbols rather than making
                # you pass it separately (OCC: last 8 digits are strike * 1000).
                try:
                    short_strike = int(short_symbol[-8:]) / 1000
                    long_strike = int(long_symbol[-8:]) / 1000
                    width = abs(short_strike - long_strike) * 100
                except (ValueError, IndexError):
                    print(json.dumps({"error": f"could not derive strike width from {short_symbol}/{long_symbol}"}))
                    return 1

    if mark is None:
        print(json.dumps({"error": "quote fetch failed -- no live mark available right now, try again shortly"}))
        return 1

    should_close, reason = risk_gate.should_close(
        credit_received=credit_received, current_mark=mark,
        is_credit_spread=is_credit, width=width,
    )
    near_stop = risk_gate.is_near_stop(credit_received=credit_received, current_mark=mark) if is_credit else None
    force_close, force_reason = risk_gate.should_force_close(expiration=exp_date)

    print(json.dumps({
        "current_mark": mark,
        "should_close": bool(force_close or should_close),
        "reason": force_reason if force_close else (reason if should_close else None),
        "near_stop": near_stop,
        "note": "current_mark is cost-to-close for credit, proceeds-from-closing for debit -- same sign "
                "convention get_spread_mark and executor_mcp.close_spread already use.",
    }))
    return 0


def main() -> int:
    if len(sys.argv) < 7:
        print(json.dumps({"error": (
            "usage: preview_close.py STRATEGY STRUCTURE CREDIT_RECEIVED EXPIRATION "
            "SHORT_SYMBOL LONG_SYMBOL [CALL_SHORT_SYMBOL CALL_LONG_SYMBOL]"
        )}))
        return 1
    strategy = sys.argv[1].strip().lower()
    structure = sys.argv[2].strip().lower()
    credit_received = float(sys.argv[3])
    expiration = sys.argv[4].strip()
    short_symbol = sys.argv[5].strip().upper()
    long_symbol = sys.argv[6].strip().upper()
    call_short_symbol = sys.argv[7].strip().upper() if len(sys.argv) > 7 else None
    call_long_symbol = sys.argv[8].strip().upper() if len(sys.argv) > 8 else None

    if strategy not in ("vertical", "iron_condor"):
        print(json.dumps({"error": f"unknown strategy {strategy!r}, use vertical|iron_condor"}))
        return 1
    if structure not in ("credit", "debit"):
        print(json.dumps({"error": f"unknown structure {structure!r}, use credit|debit"}))
        return 1

    try:
        for k, v in _load_env(_ALPACA_ENV).items():
            os.environ[k] = v
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"could not load Alpaca credentials: {exc}"}))
        return 2
    venv_bin = str(Path(_REPO_ROOT) / ".venv" / "bin")
    os.environ["PATH"] = venv_bin + os.pathsep + os.environ.get("PATH", "")

    import asyncio
    return asyncio.run(_amain(
        strategy, structure, credit_received, expiration,
        short_symbol, long_symbol, call_short_symbol, call_long_symbol,
    ))


if __name__ == "__main__":
    sys.exit(main())
