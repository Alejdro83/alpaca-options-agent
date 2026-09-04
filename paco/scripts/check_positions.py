#!/usr/bin/env python3
"""check_positions.py — Paco's ground-truth position check (2026-09-04).

A real incident (2026-09-04): mid-cycle, Paco believed it had "7 spreads
at cap" with nothing in `zeroclaw_trading.spreads` to back that number --
a belief with no real data behind it, not caught until much later. This
is the tool for exactly that moment: when you're not sure how many
positions you actually have, or whether your own DB matches what the
broker really holds, ask this instead of trusting your own memory of the
cycle so far.

Deliberately NOT reconcile_paco.py: that script can auto-write DB closes
and, on an unexplained mismatch, engage a real kill-all estop (freezes
you for hours) -- exactly right for its own 15-min background job, very
wrong for something you might call just because you're unsure mid-cycle.
This script only reads and prints. It never writes, never touches the
estop, never engages anything -- pure ground truth, you decide what (if
anything) to do about a mismatch.

Usage: check_positions.py (no arguments)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_REPO_ROOT = "/home/lab-master/alpaca-options-agent"
_SCHEMA = "zeroclaw_trading"


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
        supabase_env = _load_env(_SUPABASE_ENV)
    except FileNotFoundError as exc:
        print(json.dumps({"error": f"could not load credentials: {exc}"}))
        return 2

    sys.path.insert(0, _REPO_ROOT)
    for k, v in alpaca_env.items():
        os.environ[k] = v
    from alpaca_client import AlpacaClient
    import psycopg2

    try:
        client = AlpacaClient()
        positions = client.get_positions()
    except Exception as exc:
        print(json.dumps({"error": f"broker position fetch failed: {exc}"}))
        return 1
    broker_symbols = sorted(p["symbol"] for p in positions if str(p.get("symbol") or ""))

    try:
        con = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"], port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"], password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        cur = con.cursor()
        cur.execute(
            f"SELECT id, symbol, strategy, short_leg, long_leg, call_short_leg, call_long_leg "
            f"FROM {_SCHEMA}.spreads WHERE status = 'open' ORDER BY id"
        )
        db_open = cur.fetchall()
        con.close()
    except Exception as exc:
        print(json.dumps({"error": f"DB read failed: {exc}"}))
        return 1

    db_leg_symbols: set[str] = set()
    db_rows = []
    for row_id, symbol, strategy, short_leg, long_leg, call_short_leg, call_long_leg in db_open:
        legs = [s for s in (short_leg, long_leg, call_short_leg, call_long_leg) if s]
        db_leg_symbols.update(legs)
        db_rows.append({"id": row_id, "symbol": symbol, "strategy": strategy, "legs": legs})

    only_at_broker = sorted(set(broker_symbols) - db_leg_symbols)
    only_in_db = sorted(db_leg_symbols - set(broker_symbols))

    print(json.dumps({
        "broker_open_legs": broker_symbols,
        "db_open_spreads": db_rows,
        "db_open_count": len(db_rows),
        "matches": not only_at_broker and not only_in_db,
        "legs_at_broker_not_in_db": only_at_broker,
        "legs_in_db_not_at_broker": only_in_db,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
