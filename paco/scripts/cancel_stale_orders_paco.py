#!/usr/bin/env python3
"""cancel_stale_orders_paco.py — independent stale-order sweep for Paco
(2026-08-31, real gap found live: a SPY iron condor sat resting unfilled
for 45+ min with no later cycle even mentioning it -- not a position, not
in zeroclaw_trading.spreads, invisible to reconciliation too).

Same principle as reconcile_paco.py/portfolio_greeks_paco.py: runs as its
OWN zeroclaw cron job (plain shell command, no --agent), decoupled from
Paco's own reasoning -- a resting order Paco forgot about must not depend
on Paco remembering to check on it. This repo's own bot already solves
this at ORDER TIME (poll get_order_by_id for up to 45s, cancel+fail if it
never fills) -- Paco's place_option_order call doesn't have an equivalent
loop, so this is the cross-cycle safety net instead: any order still
resting (not filled/canceled/rejected) past STALE_MINUTES gets cancelled.
Cancelling is always safe here -- Paco never logs a spread to its own DB
until AFTER a confirmed fill, so a cancelled stale order was never
counted as an open position anywhere.

Usage (no arguments):
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/cancel_stale_orders_paco.py

Exit 0 = swept clean (or nothing to sweep). Exit 2 = broker unreachable --
non-fatal by design (this is a safety net, not a trading action; a
transient failure here just means the sweep retries next cron tick).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

STALE_MINUTES = 10  # one full Paco cycle (cron runs every 10 min) is enough
# time for a marketable limit to fill under normal liquidity; matches this
# repo's own bot's much shorter 45s specifically because that bot polls
# inline right after placing the order -- this is a cross-cycle sweep
# instead, so it only needs to catch what Paco's own cycle already missed.

# Orders in these Alpaca statuses are still "live" (could still fill) --
# anything else (filled/canceled/rejected/expired) needs no action.
_LIVE_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "held"}


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
    try:
        alpaca_env = _load_env(_ALPACA_ENV)
    except FileNotFoundError as exc:
        print(f"ERROR: could not load {_ALPACA_ENV}: {exc}", file=sys.stderr)
        return 2

    try:
        import os
        for k, v in alpaca_env.items():
            os.environ[k] = v
        from alpaca_client import AlpacaClient

        client = AlpacaClient()
        orders = client.get_orders(status="open")
    except Exception as exc:
        print(f"ERROR: broker orders unavailable: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=STALE_MINUTES)
    swept = 0
    for o in orders:
        if o["status"] not in _LIVE_STATUSES:
            continue
        try:
            submitted = datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            # Unknown age -- fail-closed the same way this project always
            # does for a missing/unparseable timestamp: treat as stale
            # rather than assume it's fine.
            submitted = None
        if submitted is not None and submitted > cutoff:
            continue  # still fresh, give it more time
        age = (now - submitted) if submitted else None
        print(f"cancel_stale_orders_paco: cancelling {o['id']} "
              f"(status={o['status']}, age={age}, legs={[l['symbol'] for l in (o['legs'] or [])]})")
        try:
            client.cancel_order(o["id"])
            swept += 1
        except Exception as exc:
            print(f"WARNING: could not cancel {o['id']}: {exc}", file=sys.stderr)

    if swept == 0:
        print("cancel_stale_orders_paco: nothing to sweep.")
    else:
        print(f"cancel_stale_orders_paco: cancelled {swept} stale order(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
