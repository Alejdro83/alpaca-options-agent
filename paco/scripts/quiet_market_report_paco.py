#!/usr/bin/env python3
"""quiet_market_report_paco.py — end-of-day quiet-market diagnostic for
Paco (2026-09-02).

Mirrors the judged bot's quiet_market_report.py (same day, same
reasoning -- see its own module docstring for the full "why"): report-
only, NEVER relaxes any gate, NEVER opens/changes anything. Runs as its
OWN zeroclaw cron (a plain shell task, not an --agent/LLM task) --
deliberately decoupled from Paco's own reasoning, same principle already
used by reconcile_paco.py/account_snapshot_paco.py/portfolio_greeks_paco.py:
a monitoring feature must never depend on the LLM remembering to run it.

Deliberately scoped to the "were we quiet today" signal ONLY, not a real-
P&L summary too -- unlike the judged bot (whose real opens/closes only
ever reach a human via the cycle-log-based Discord delivery), Paco
already calls notify.sh the instant it opens or closes a real position
(AGENTS.md step 11/12) -- duplicating that here would just be a second,
redundant notification for the same event.

Delivered via notify.sh (Telegram, Paco's existing channel) rather than
Discord -- Paco has no Discord channel wired.

Usage (no arguments, exit 0 always -- a notification/DB hiccup here must
never look like anything Paco itself did wrong):
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/quiet_market_report_paco.py
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_SCHEMA = "zeroclaw_trading"
_REPORT_PATH = Path(__file__).resolve().parent.parent / "state" / "quiet_market_report.md"
_NOTIFY_SH = Path(__file__).resolve().parent / "notify.sh"


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


def _connect():
    env = _load_env(_SUPABASE_ENV)
    return psycopg2.connect(
        host=env["SUPABASE_DB_HOST"],
        port=env.get("SUPABASE_DB_PORT", "5432"),
        dbname=env.get("SUPABASE_DB_NAME", "postgres"),
        user=env["SUPABASE_DB_USER"],
        password=env["SUPABASE_DB_PASSWORD"],
        sslmode="require",
    )


def check_quiet_market_day(today: date) -> str | None:
    """Returns a short summary if TODAY produced zero real spreads opened
    for Paco, or None if at least one real spread opened today (stay
    silent -- Paco's own notify.sh already announced it live).
    """
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"select symbol, ts from {_SCHEMA}.spreads where ts::date = %s",
                (today.isoformat(),),
            )
            if cur.fetchall():
                return None  # at least one real open today -- nothing to report

            cur.execute(
                f"select decision, regime, strategy, ts from {_SCHEMA}.cycles "
                f"where ts::date = %s order by ts",
                (today.isoformat(),),
            )
            todays_cycles = list(cur.fetchall())
    finally:
        conn.close()

    total_cycles = len(todays_cycles)
    # run_cycle_paco.py's guaranteed wrapper rows store a plain string
    # decision ('started'/'completed'/'timeout'/'error'); Paco's own
    # richer write_cycle.py rows store a dict ({'action':..., 'reason':...}
    # or similar). Both are real -- counted separately rather than
    # assuming one shape, since the real table mixes both (confirmed live
    # 2026-09-02).
    wrapper_rows = sum(1 for c in todays_cycles if isinstance(c["decision"], str))
    errors = sum(1 for c in todays_cycles if c["decision"] in ("timeout", "error"))

    lines = [
        f"# Quiet-market report (Paco) — {today.isoformat()}",
        "",
        f"0 real spreads opened today, across {total_cycles} real cycle row(s) "
        f"({wrapper_rows} guaranteed-wrapper rows, {total_cycles - wrapper_rows} of Paco's "
        f"own reasoned rows, {errors} timeout/error).",
        "",
    ]
    if todays_cycles:
        lines.append("## Real cycle decisions today")
        for c in todays_cycles:
            lines.append(f"- {c['ts']}: {c['decision']} (regime={c.get('regime')}, strategy={c.get('strategy')})")
    report_text = "\n".join(lines)
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(report_text)

    return (
        f"🌙 Mercado tranquilo (Paco) — {today.isoformat()}: 0 spreads reales abiertos hoy, "
        f"{total_cycles} ciclos reales ({errors} timeout/error). Detalle: {_REPORT_PATH}"
    )


def main() -> int:
    today = datetime.now(timezone.utc).date()
    try:
        summary = check_quiet_market_day(today)
    except Exception as exc:
        print(f"WARNING: quiet-market check failed (non-fatal): {exc}", file=sys.stderr)
        return 0
    if summary is None:
        return 0

    print(summary)
    if _NOTIFY_SH.exists():
        try:
            subprocess.run([str(_NOTIFY_SH), summary], check=False, timeout=30)
        except Exception as exc:
            print(f"WARNING: notify.sh failed (non-fatal): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
