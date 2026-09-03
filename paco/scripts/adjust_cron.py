#!/usr/bin/env python3
"""adjust_cron.py — Paco's own adaptive heartbeat (2026-08-30).

The "smart cron" from this project's original concept doc (frequency
that speeds up near a stop, slows down when idle) was built first for
the OTHER, deterministic bot (alpaca-options-agent/bot.py rewriting its
own Hermes job) rather than for Paco -- an accident of timing: Paco was
shelved when that feature was built, then revived later without it.
This is the same idea, ported to Paco's own zeroclaw cron job.

Fully self-contained: takes no arguments, needs no state passed in by
Paco. Run this at the end of every cycle:

    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/adjust_cron.py

Reads Alpaca creds from mcp_risk_proxy/.env (the real trading_bot account
Paco trades on) and Supabase creds from the neighboring .env (same file
write_cycle.py uses) -- never touches the judged hackathon account or its
alpaca_hackathon schema. Any failure prints a warning and exits 0: an
adaptive-frequency problem must never look like a failed trading cycle.

Frequency tiers (minutes), same logic as bot.py's _adaptive_cron_minutes:
  2  -- any open spread is within 80% of its stop-loss threshold
  5  -- normal: spreads open, none near stop (the original fixed cadence)
  30 -- idle (nothing open) AND today's circuit breaker is tripped
  10 -- idle and calm otherwise
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# alpaca_client.py lives in alpaca-options-agent's own repo, not on this
# venv's default path -- this script runs under that venv (for psycopg2
# and alpaca-py, both already installed and tested there) but needs the
# project's own directory added explicitly to import from it.
sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

_WORKSPACE = Path(__file__).resolve().parent.parent
_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_ZEROCLAW_BIN = Path.home() / ".cargo" / "bin" / "zeroclaw"
# 2026-08-30 fix: this used to be a hardcoded id -- if the job is ever
# removed and recreated (its id changes every time), a hardcoded constant
# would silently stop updating the real cron with no error anyone would
# notice, since a zeroclaw failure here is deliberately non-fatal (see
# module docstring). Kept as a last-resort fallback below in case
# `zeroclaw cron list`'s text format ever changes underneath the parser
# in _resolve_paco_cron_job_id -- a fallback to a possibly-stale id still
# just fails cleanly with a WARNING, it can't silently do the wrong thing.
#
# 2026-08-31: Paco's own trading-cycle job stopped being the one `prompt:`
# job in this instance -- it's now `run_cycle_paco.py`'s guaranteed-logging
# wrapper (a plain `cmd:` job, see that script's own docstring for why),
# so it now looks exactly like reconcile_paco.py/portfolio_greeks_paco.py/
# cancel_stale_orders_paco.py's jobs on the surface. Disambiguate by the
# actual script name in the `cmd:` line instead of by job type.
_PACO_CRON_JOB_ID_FALLBACK = "1db4f69b-c615-41be-93a9-b3ab39d2c8b9"
_PACO_CRON_SCRIPT_MARKER = "run_cycle_paco.py"
_SCHEMA = "zeroclaw_trading"


def _resolve_paco_cron_job_id() -> str:
    """Finds Paco's own trading-cycle job by content, not a stored id --
    the one whose `cmd:` line invokes run_cycle_paco.py specifically,
    distinguishing it from the other independent cmd-jobs
    (reconcile_paco.py/portfolio_greeks_paco.py/cancel_stale_orders_paco.py)
    that must never have their schedule touched by this script. Falls back
    to the last-known id if the list can't be parsed for any reason.
    """
    if not _ZEROCLAW_BIN.exists():
        return _PACO_CRON_JOB_ID_FALLBACK
    try:
        result = subprocess.run(
            [str(_ZEROCLAW_BIN), "cron", "list"], capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return _PACO_CRON_JOB_ID_FALLBACK
        current_id = None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            m = re.match(r"^-\s+([0-9a-f-]{36})\s+\|", stripped)
            if m:
                current_id = m.group(1)
                continue
            if stripped.startswith("cmd:") and _PACO_CRON_SCRIPT_MARKER in stripped and current_id:
                return current_id
    except Exception:
        pass
    print("WARNING: could not resolve Paco's cron job id dynamically -- using last-known id", file=sys.stderr)
    return _PACO_CRON_JOB_ID_FALLBACK

# Same defaults as alpaca-options-agent's config.py -- kept as explicit
# literals here rather than importing that project's config, since this
# script deliberately stays isolated from the judged bot's own codebase
# (see mcp_risk_proxy's own docstring for why this experiment is kept
# separate on purpose).
STOP_LOSS_MULTIPLE = 2.0
NEAR_STOP_PCT = 0.80
MAX_DAILY_LOSS_PCT = 0.03


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        print(f"WARNING: {path} not found -- skipping adaptive cron this cycle", file=sys.stderr)
        return {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def _fetch_open_spreads(supabase_env: dict[str, str]) -> list[dict]:
    conn = psycopg2.connect(
        host=supabase_env["SUPABASE_DB_HOST"],
        port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
        dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
        user=supabase_env["SUPABASE_DB_USER"],
        password=supabase_env["SUPABASE_DB_PASSWORD"],
        sslmode="require",
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"select id, symbol, short_leg, long_leg, credit, max_loss "
                f"from {_SCHEMA}.spreads where status = 'open'"
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def _is_near_stop(credit_received: float, current_mark: float) -> bool:
    if not credit_received:
        return False
    stop_threshold = credit_received * STOP_LOSS_MULTIPLE
    return current_mark >= stop_threshold * NEAR_STOP_PCT


def _decide_minutes(alpaca_env: dict[str, str], open_spreads: list[dict]) -> int:
    # Must set these BEFORE importing alpaca_client -- config.py builds its
    # module-level singleton from os.environ at import time, once, so the
    # keys have to already be there.
    import os
    for k, v in alpaca_env.items():
        os.environ[k] = v
    from alpaca_client import AlpacaClient  # alpaca-options-agent's venv provides this

    client = AlpacaClient()

    account = client.get_account()
    equity = float(account.get("equity") or 0)
    last_equity = float(account.get("last_equity") or equity)
    daily_pl_pct = (equity - last_equity) / last_equity if last_equity else 0.0
    circuit_breaker_active = daily_pl_pct <= -MAX_DAILY_LOSS_PCT

    if not open_spreads:
        return 30 if circuit_breaker_active else 10

    positions = {p["symbol"]: p for p in client.get_positions()}
    near_stop = False
    for s in open_spreads:
        short_pos = positions.get(s["short_leg"])
        long_pos = positions.get(s["long_leg"])
        if short_pos is None or long_pos is None:
            continue  # can't price this one live; don't let it block the others
        contracts = abs(float(short_pos["qty"])) or 1.0
        current_mark = (float(short_pos["current_price"]) - float(long_pos["current_price"])) * 100
        credit_per_contract = float(s["credit"] or 0) / contracts if float(s["credit"] or 0) else float(s["credit"] or 0)
        if _is_near_stop(credit_per_contract, current_mark):
            near_stop = True
            break

    if near_stop:
        return 2
    return 5


def main() -> None:
    supabase_env = _load_env(_SUPABASE_ENV)
    alpaca_env = _load_env(_ALPACA_ENV)
    if not supabase_env or not alpaca_env:
        return

    try:
        open_spreads = _fetch_open_spreads(supabase_env)
    except Exception as exc:
        print(f"WARNING: adaptive cron couldn't read open spreads: {exc}", file=sys.stderr)
        return

    try:
        minutes = _decide_minutes(alpaca_env, open_spreads)
    except Exception as exc:
        print(f"WARNING: adaptive cron couldn't compute frequency: {exc}", file=sys.stderr)
        return

    expr = f"*/{minutes} 13-20 * * 1-5"
    if not _ZEROCLAW_BIN.exists():
        print(f"WARNING: {_ZEROCLAW_BIN} not found -- skipping cron update", file=sys.stderr)
        return

    job_id = _resolve_paco_cron_job_id()
    result = subprocess.run(
        [str(_ZEROCLAW_BIN), "cron", "update", job_id,
         "--agent", "trading", "--expression", expr],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: zeroclaw cron update failed: {result.stderr.strip()}", file=sys.stderr)
        return

    print(f"Adaptive cron: next check frequency every {minutes} min ({expr})")


if __name__ == "__main__":
    main()
