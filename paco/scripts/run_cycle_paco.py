#!/usr/bin/env python3
"""run_cycle_paco.py — guaranteed cycle logging wrapper for Paco (2026-08-31).

Paco's trading cron fires as a --prompt job that invokes the LLM directly.
Logging a row to zeroclaw_trading.cycles is step 13 of AGENTS.md's own
checklist -- something the LLM is INSTRUCTED to do, not something guaranteed
by code. Verified live: only 2 of ~9 real ticks since market open actually
wrote a cycles row. This wrapper makes it unconditional: every cron tick
gets a guaranteed row regardless of what Paco itself does inside its turn.

Replaces the --prompt cron job with a plain shell-command job that calls
this script. Two rows per real cycle is fine and expected: one guaranteed
wrapper row (proves "did it run"), one optional richer row from Paco itself
(if it got there) with real regime/strategy/candidate detail. Do not try
to merge or dedupe them.

Lock file (2026-08-31 review, added after the first draft): the agent
subprocess timeout (600s) is longer than the cron interval (10 min) by
design margin only, not guaranteed -- a slow turn could still be running
when the next tick fires. Two concurrent `zeroclaw agent -a trading`
processes acting on the same real account at once is a real risk (duplicate
orders, conflicting position management), same class of problem
emergency_flatten.py's state/bot.lock already exists to prevent for the
judged bot. A tick that finds the lock held logs decision='skipped_locked'
and exits clean rather than starting a second concurrent agent process.

Usage:
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/run_cycle_paco.py

Exit 0 = cycle ran, skipped (lock held), or logged a clean failure. Exit 1 =
the wrapper itself failed before logging anything (DB unreachable) -- the
agent turn may or may not have run; check stderr for details.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import psycopg2

_ENV_PATH = Path(__file__).resolve().parent / ".env"
_SCHEMA = "zeroclaw_trading"
# 2026-08-31 review: was 600 (exactly the cron interval). Real incident
# found live: cycle #40 ran its full 600s, placed a real QQQ iron condor
# order 8 seconds before this timeout killed it -- it never got to poll
# get_order_by_id or log the spread via write_cycle.py. Harmless this time
# (the order later expired unfilled), but if it HAD filled, Paco's own
# bookkeeping would never have known until reconcile_paco.py's next pass
# flagged it as a phantom position and tripped the estop -- a much harsher
# discovery path than Paco just managing a known position next cycle.
# Halved to 300s so a normal cycle has real headroom before the cron's own
# next tick, not a photo finish with SIGKILL.
#
# 2026-09-04: raised back to 480s after finding the REAL reason every
# single cycle was timing out at 300s wasn't cycle length -- it was
# agents.trading.model_provider (anthropic.xiaomi, the /anthropic-shaped
# endpoint) failing to decode responses and burning ~350s on 3 retries
# before erroring out. Switched to openai.xiaomi (the same backend's
# proven-reliable /v1 chat-completions endpoint, already used by the
# judged bot's llm_reasoner.py) -- see GitHub issue "Paco: trading cycles
# 100% timing out". 480s (vs the 600s cron interval) keeps real headroom
# for a legitimately multi-step cycle (iron condor + iron condor +
# vertical, each its own tool-call round trip) without reverting to the
# 8-seconds-from-SIGKILL situation the original 600s caused.
_AGENT_TIMEOUT = 480
_MAX_RESULT_CHARS = 4000
_AGENT_BIN = Path.home() / ".cargo" / "bin" / "zeroclaw"
_LOCK_PATH = Path(__file__).resolve().parent / "run_cycle_paco.lock"
# 2026-09-04 tightened: the old generic phrasing left every fresh cycle
# (no memory of the last one) re-deriving "what do I do now" from
# AGENTS.md's prose before its first real tool call. Pointing straight at
# the cycle-checklist skill (paco_trading bundle, `always: true`) skips
# that -- same steps, same order, just handed over instead of rediscovered.
# A pure prompt/reasoning-overhead optimization: it was NOT what fixed the
# 100% timeout rate (that was the model tier, see KNOWN_ISSUES.md Bug #12)
# and shouldn't be expected to on its own.
_AGENT_PROMPT = (
    "Run one trading cycle. Follow the cycle-checklist skill step by "
    "step, in the order it gives -- don't re-derive the plan from "
    "AGENTS.md's prose first, it's already been turned into that "
    "checklist for you. Report what you did."
)


def _load_env() -> dict[str, str]:
    if not _ENV_PATH.exists():
        print(f"ERROR: {_ENV_PATH} not found -- credentials must live there, never in a prompt file.", file=sys.stderr)
        sys.exit(1)
    env: dict[str, str] = {}
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def _connect():
    env = _load_env()
    try:
        return psycopg2.connect(
            host=env["SUPABASE_DB_HOST"],
            port=env.get("SUPABASE_DB_PORT", "5432"),
            dbname=env.get("SUPABASE_DB_NAME", "postgres"),
            user=env["SUPABASE_DB_USER"],
            password=env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
    except KeyError as exc:
        print(f"ERROR: {_ENV_PATH} is missing required key {exc}.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: could not connect to Supabase: {exc}", file=sys.stderr)
        sys.exit(1)


def _insert_row(decision: str) -> int:
    # decision/result are JSONB columns (confirmed against the real schema
    # -- write_cycle.py's own cmd_cycle wraps both in json.dumps for exactly
    # this reason). A bare Python str passed straight through psycopg2 is
    # NOT valid JSON input (only true/false/null are valid bare literals),
    # so every insert/update here MUST go through json.dumps -- the first
    # draft of this script skipped that and would have failed the DB write
    # on every single tick, never even reaching the agent subprocess.
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"""insert into {_SCHEMA}.cycles
                    (regime, strategy, candidates, decision, result, pnl_day, equity)
                    values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                ("pending", None, None, json.dumps(decision), None, None, None),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _update_row(cycle_id: int, decision: str, result: object) -> None:
    # Fresh connection rather than reusing the one from _insert_row: the
    # agent subprocess in between can run for up to _AGENT_TIMEOUT (10 min)
    # -- an idle connection held open that long risks the pooler recycling
    # or dropping it underneath us. Cheap to just reconnect.
    conn = _connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                f"""update {_SCHEMA}.cycles
                    set decision = %s, result = %s
                    where id = %s""",
                (json.dumps(decision), json.dumps(result), cycle_id),
            )
    finally:
        conn.close()


def main() -> int:
    # Lock BEFORE touching the DB at all -- if a previous invocation is
    # still running its agent subprocess, don't even log a new "started"
    # row for this tick (it never actually starts anything).
    lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("run_cycle_paco: previous cycle still running (lock held), skipping this tick.")
        try:
            cycle_id = _insert_row("skipped_locked")
            _update_row(cycle_id, "skipped_locked", {"reason": "previous cycle's agent subprocess still running"})
        except Exception:
            pass  # best-effort log of the skip; the skip itself is the important part
        return 0

    try:
        try:
            cycle_id = _insert_row("started")
        except Exception as exc:
            print(f"ERROR: could not insert started row: {exc}", file=sys.stderr)
            return 1

        try:
            result = subprocess.run(
                [str(_AGENT_BIN), "agent", "-a", "trading", "-m", _AGENT_PROMPT],
                capture_output=True,
                text=True,
                timeout=_AGENT_TIMEOUT,
            )
            if result.returncode == 0:
                decision = "completed"
                row_result = {"stdout": result.stdout[:_MAX_RESULT_CHARS]}
            else:
                decision = "error"
                row_result = {"error": result.stderr[:_MAX_RESULT_CHARS], "exit_code": result.returncode}
        except subprocess.TimeoutExpired:
            # subprocess.run kills the process (SIGKILL, after its own grace
            # period) and re-raises this -- the process is already dead by
            # the time we get here, no explicit kill() needed.
            decision = "timeout"
            row_result = {"error": f"agent turn exceeded {_AGENT_TIMEOUT}s, killed"}
        except Exception as exc:
            decision = "error"
            row_result = {"error": str(exc)[:_MAX_RESULT_CHARS], "exit_code": -1}

        try:
            _update_row(cycle_id, decision, row_result)
        except Exception as exc:
            print(f"ERROR: could not update cycle row {cycle_id}: {exc}", file=sys.stderr)
            return 1

        print(f"cycle_id={cycle_id} decision={decision}")
        return 0
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: unexpected exception in main: {exc}", file=sys.stderr)
        sys.exit(1)
