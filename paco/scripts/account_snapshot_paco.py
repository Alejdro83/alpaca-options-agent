#!/usr/bin/env python3
"""account_snapshot_paco.py — independent account-balance snapshot for
Paco (2026-09-01).

Real gap found live: the public dashboard's /compare panel showed Paco
with no balance at all, while the judged bot and rookieriot both showed
theirs. zeroclaw_trading.account_snapshots had ZERO rows, ever --
write_cycle.py already has a `snapshot` subcommand for this, but AGENTS.md
never actually instructs Paco to call it anywhere in the cycle procedure,
so it silently depended on the LLM noticing and using it on its own. Same
class of gap as reconcile_paco.py/portfolio_greeks_paco.py: a monitoring/
dashboard feature must never depend on the LLM remembering to log it.

Runs as its OWN zeroclaw cron job (a plain shell task, not an --agent
task) -- read-only start to finish, never places or modifies an order.

CRITICAL safety pattern, copied verbatim from reconcile_paco.py/
portfolio_greeks_paco.py: loads mcp_risk_proxy/.env (Paco's own
repurposed trading_bot account) and sets those as os.environ BEFORE
importing alpaca_client/config, so config.alpaca never accidentally
resolves to the judged hackathon account's credentials
(alpaca-options-agent/.env). Never import alpaca_client at module level
for this reason -- only inside main(), after the environment is set.

Usage (no arguments):
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/account_snapshot_paco.py

Exit 0 = snapshot recorded. Exit 2 = the check itself failed (DB/broker
unreachable) -- never raises past main(), same fail-safe posture as the
other independent scripts; a failure here only means "the dashboard stays
stale", never a trading action, so there is no estop/kill-switch
escalation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2

_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_SCHEMA = "zeroclaw_trading"

sys.path.insert(0, "/home/lab-master/alpaca-options-agent")


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
        supabase_env = _load_env(_SUPABASE_ENV)
        alpaca_env = _load_env(_ALPACA_ENV)
    except FileNotFoundError as exc:
        print(f"ERROR: could not load required .env: {exc}", file=sys.stderr)
        return 2

    try:
        import os
        for k, v in alpaca_env.items():
            os.environ[k] = v
        from alpaca_client import AlpacaClient

        client = AlpacaClient()
        account = client.get_account()
        positions = client.get_positions()
    except Exception as exc:
        print(f"ERROR: broker unavailable: {exc}", file=sys.stderr)
        return 2

    equity = account["equity"]
    daily_pnl = equity - account["last_equity"]

    # SPY price alongside every snapshot -- same synthetic "skill vs.
    # market" overlay the judged bot's dashboard curve already has
    # (bot.py's own comment: non-capital-affecting, informational only).
    # Added 2026-09-01: Alex noticed Paco's curve had no dashed SPY line --
    # not a missing "calculator", just a column/value this table never had.
    spy_price = None
    try:
        snap = client.get_snapshots(["SPY"])
        spy_price = snap.get("SPY", {}).get("latest_trade_price")
    except Exception:
        print("account_snapshot_paco: could not fetch SPY price (non-fatal)", file=sys.stderr)

    try:
        conn = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"],
            port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"],
            password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        with conn, conn.cursor() as cur:
            cur.execute(
                f"""insert into {_SCHEMA}.account_snapshots
                    (equity, buying_power, daily_pnl, positions, spy_price)
                    values (%s, %s, %s, %s, %s) returning id""",
                (equity, account["buying_power"], daily_pnl, json.dumps(positions), spy_price),
            )
            new_id = cur.fetchone()[0]
        conn.close()
        print(f"account_snapshot_paco: recorded snapshot id={new_id}, "
              f"equity=${equity:,.2f} daily_pnl=${daily_pnl:+,.2f} positions={len(positions)} "
              f"spy_price={spy_price}")
        return 0
    except Exception as exc:
        print(f"ERROR: could not write snapshot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
