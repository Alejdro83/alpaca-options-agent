#!/usr/bin/env python3
"""reconcile_paco.py — independent broker/DB reconciliation for Paco (2026-08-30).

Runs as its OWN zeroclaw cron job (a plain shell task, not an --agent
task) — deliberately decoupled from Paco's own LLM reasoning loop, same
principle as the judged bot's reconciler.py: this must never depend on
the LLM remembering to check it or reasoning about it correctly. Real
gap this closes: this project already had one incident from exactly this
failure class (2026-08-27, a rejected order that still produced a
phantom "open" row for the judged bot, fixed at the source that day) --
Paco had no equivalent independent check at all.

Compares Alpaca's real option positions (trading_bot's account, via
mcp_risk_proxy/.env -- NEVER the judged hackathon account) against
zeroclaw_trading.spreads (status='open'). A mismatch calls
`zeroclaw estop --level kill-all` for real -- unlike a kill_switch file,
this does not depend on Paco reading anything at the start of its next
cycle; it blocks tool calls immediately, verified live 2026-08-30
(engage/resume round-tripped cleanly against the real daemon).

Self-heal (2026-09-02): a DB-open spread whose legs have ALL vanished
from the broker is, by far, most often just a clean close that Paco's
own cycle placed but never wrote back -- the 300s agent budget gets
SIGKILLed between the fill and the write_cycle.py close-spread call
(run_cycle_paco.py's own comments predicted exactly this). Before
escalating that case to a kill-all estop -- which freezes Paco for hours
until a human resumes -- this script now looks for the matching closing
fill in Alpaca's own order history. If it finds an exact, fully-filled,
opposite-side closing order for those precise legs, it writes the close
itself (status='closed', real pnl from the fills) and does NOT engage
the estop. Everything genuinely unexplained -- orphan broker legs, leg
qty/side disagreements, a partial or missing closing fill, "can't
verify" -- still trips the estop exactly as before.

Usage (no arguments):
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/reconcile_paco.py

Exit 0 = reconciled OK (or nothing to reconcile, or every mismatch was a
benign auto-healed close). Exit 1 = real mismatch found, estop engaged.
Exit 2 = the check itself failed (DB or broker unreachable) -- fails
closed: engages estop too, same as a real mismatch, since "can't verify"
must never be treated as "verified fine".
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_ZEROCLAW_BIN = Path.home() / ".cargo" / "bin" / "zeroclaw"
_SCHEMA = "zeroclaw_trading"
_TRACKING_DB = Path("/home/lab-master/mcp_risk_proxy/state/tracking.db")
_TRACKING_DB_BACKUP = Path("/home/lab-master/mcp_risk_proxy/state/tracking_backup.db")


def _backup_tracking_db() -> None:
    """The proxy's local SQLite tracking state has no backup at all --
    low risk (it's reconstructable from Alpaca's own real positions/orders
    if lost) but a real gap, and this cron already runs every 15 min
    independent of Paco's own reasoning, so it's a natural place for a
    cheap safety copy. Uses sqlite3's own backup API (not a raw file
    copy) specifically because it's safe against a concurrent writer --
    a plain `cp` could grab a half-written page if the proxy is mid-write.
    Best-effort: never blocks or fails the real reconciliation below.
    """
    if not _TRACKING_DB.exists():
        return
    try:
        src = sqlite3.connect(str(_TRACKING_DB))
        try:
            dst = sqlite3.connect(str(_TRACKING_DB_BACKUP))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except Exception as exc:
        print(f"WARNING: tracking.db backup failed (non-fatal): {exc}", file=sys.stderr)


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


def _option_symbols_from_positions(positions: list[dict]) -> set[str]:
    """Same conservative heuristic as the judged bot's reconciler.py:
    OCC option symbols contain digits and run well past 6 chars; equity
    tickers on this account never do."""
    out: set[str] = set()
    for p in positions:
        sym = str(p.get("symbol") or "")
        if sym and (any(ch.isdigit() for ch in sym) or len(sym) > 6):
            out.add(sym)
    return out


def _db_leg_symbols(spreads: list[dict]) -> set[str]:
    legs: set[str] = set()
    for s in spreads:
        for col in ("short_leg", "long_leg", "call_short_leg", "call_long_leg"):
            if s.get(col):
                legs.add(s[col])
    return legs


def _spread_leg_symbols(spread: dict) -> set[str]:
    return {
        spread[col]
        for col in ("short_leg", "long_leg", "call_short_leg", "call_long_leg")
        if spread.get(col)
    }


# 2026-08-30 fix: the symbol-set check above only proves every expected
# symbol EXISTS somewhere at the broker -- it says nothing about quantity
# or direction. zeroclaw_trading.spreads has no `contracts` column at all
# (schema gap, not fixed here to avoid a migration this close to Monday),
# so this can't compare against "what Paco meant to trade" -- but it CAN
# compare legs against EACH OTHER: every leg of one real spread must share
# the same |qty| (a spread's legs are always sized together) and match
# its expected side (short_leg/call_short_leg -> broker side "short",
# long_leg/call_long_leg -> "long"). A short leg quietly filled at 5
# contracts against a long leg at 1 would pass the old symbol-only check
# and still be flagged here.
_LEG_ROLES = {
    "short_leg": "short", "long_leg": "long",
    "call_short_leg": "short", "call_long_leg": "long",
}

# The order side Paco used to OPEN each leg role (a credit/debit spread is
# always short-one-leg / long-the-other). Closing the position is the
# exact opposite side on every leg -- that's what the self-heal matches.
_OPEN_SIDE_BY_ROLE = {"short": "sell", "long": "buy"}


def _leg_consistency_issues(spreads: list[dict], positions: list[dict]) -> list[str]:
    by_symbol = {p["symbol"]: p for p in positions}
    issues: list[str] = []
    for s in spreads:
        legs = [(col, s[col]) for col in _LEG_ROLES if s.get(col)]
        seen_qty: dict[float, list[str]] = {}
        for col, symbol in legs:
            pos = by_symbol.get(symbol)
            if pos is None:
                continue  # already reported as a phantom leg above
            expected_side = _LEG_ROLES[col]
            actual_side = str(pos.get("side") or "")
            if actual_side != expected_side:
                issues.append(
                    f"spread #{s['id']} {symbol} ({col}): expected side "
                    f"'{expected_side}', broker says '{actual_side}'"
                )
            qty = abs(float(pos.get("qty") or 0))
            seen_qty.setdefault(qty, []).append(f"{symbol} ({col})")
        if len(seen_qty) > 1:
            detail = "; ".join(f"{q} contract(s): {', '.join(syms)}" for q, syms in seen_qty.items())
            issues.append(f"spread #{s['id']} legs disagree on quantity -- {detail}")
    return issues


def _side_str(value) -> str:
    return (value.value if hasattr(value, "value") else str(value)).lower()


def _find_closing_fill(client, spread: dict) -> dict | None:
    """Look for the closing order that flattened `spread` at the broker.

    Returns {"close_cash": float, "closed_at": str, "close_order_id": str,
    "contracts": int} for an exact, fully-filled, opposite-side match, or
    None if nothing unambiguous was found (in which case the caller must
    fall back to the estop -- an unexplained phantom is not safe to
    auto-close).

    close_cash is the net broker cash flow of the CLOSE alone, already
    scaled to the whole position (x100 x contracts): negative = Paco paid
    to buy the spread back (the normal case for a credit spread), positive
    = Paco received to close.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    expected = {}  # leg symbol -> closing side ("buy"/"sell")
    for col, role in _LEG_ROLES.items():
        sym = spread.get(col)
        if sym:
            open_side = _OPEN_SIDE_BY_ROLE[role]
            expected[sym] = "buy" if open_side == "sell" else "sell"
    if not expected:
        return None

    opened_at = spread.get("ts")
    if isinstance(opened_at, datetime):
        after = opened_at - timedelta(minutes=1)
    else:
        after = datetime.now(timezone.utc) - timedelta(days=14)

    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, after=after, limit=500, nested=True,
    )
    orders = client._trading.get_orders(filter=req)

    want_contracts = int(spread.get("contracts") or 1)
    matches: list[tuple[datetime, dict]] = []

    for o in orders:
        legs = list(getattr(o, "legs", None) or [])
        if not legs:
            continue
        by_sym = {leg.symbol: leg for leg in legs}
        if set(by_sym) != set(expected):
            continue

        ok = True
        close_cash = 0.0
        latest_fill: datetime | None = None
        for sym, want_side in expected.items():
            leg = by_sym[sym]
            status = (leg.status.value if hasattr(leg.status, "value") else str(leg.status)).lower()
            fqty = float(leg.filled_qty or 0)
            favg = leg.filled_avg_price
            if status != "filled" or favg is None or fqty != want_contracts:
                ok = False
                break
            if _side_str(leg.side) != want_side:
                ok = False
                break
            sign = 1.0 if want_side == "sell" else -1.0
            close_cash += sign * float(favg) * fqty * 100.0
            fill_ts = getattr(leg, "filled_at", None) or getattr(o, "filled_at", None)
            if isinstance(fill_ts, datetime) and (latest_fill is None or fill_ts > latest_fill):
                latest_fill = fill_ts
        if not ok:
            continue

        closed_at = (latest_fill or getattr(o, "filled_at", None)
                     or getattr(o, "updated_at", None) or datetime.now(timezone.utc))
        matches.append((
            closed_at if isinstance(closed_at, datetime) else datetime.now(timezone.utc),
            {
                "close_cash": round(close_cash, 2),
                "closed_at": (closed_at.isoformat() if isinstance(closed_at, datetime)
                              else datetime.now(timezone.utc).isoformat()),
                "close_order_id": str(o.id),
                "contracts": want_contracts,
            },
        ))

    if not matches:
        return None
    # Earliest full close wins (if the position were somehow closed and
    # reopened, the first close is the one that pairs with this open row).
    matches.sort(key=lambda m: m[0])
    return matches[0][1]


def _autoclose_spread(supabase_env: dict, spread_id: int, pnl: float,
                      closed_at: str, reason: str) -> bool:
    """Write the close for one spread. Own short-lived connection so a
    failure here is isolated and simply leaves the spread unhealed (the
    caller then escalates it like any other phantom)."""
    try:
        conn = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"],
            port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"],
            password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""update {_SCHEMA}.spreads
                            set status = 'closed', closed_at = %s,
                                close_reason = %s, pnl = %s
                            where id = %s and status = 'open'""",
                        (closed_at, reason, round(pnl, 2), spread_id),
                    )
                    return cur.rowcount == 1
        finally:
            conn.close()
    except Exception as exc:
        print(f"WARNING: auto-close DB write failed for spread #{spread_id}: {exc}",
              file=sys.stderr)
        return False


def _try_selfheal_phantoms(open_spreads: list[dict], phantom: set[str], orphan: set[str],
                           leg_issues: list[str], client, supabase_env: dict) -> set[int]:
    """For each DB-open spread whose every leg has vanished from the broker
    (and which isn't tangled in any orphan / leg-consistency issue), try to
    pair it with its real closing fill and write the close. Returns the set
    of spread ids that were successfully auto-closed."""
    healed: set[int] = set()
    issue_ids = {int(tok[1:]) for issue in leg_issues for tok in issue.split()
                 if tok.startswith("#") and tok[1:].isdigit()}

    for s in open_spreads:
        legs = _spread_leg_symbols(s)
        if not legs or not legs.issubset(phantom):
            continue  # some leg still present at broker -> not a clean vanish
        if legs & orphan or int(s["id"]) in issue_ids:
            continue  # entangled with a real anomaly -> must escalate

        fill = _find_closing_fill(client, s)
        if fill is None:
            continue  # no unambiguous closing order -> leave for the estop

        open_credit = float(s.get("credit") or 0.0)  # whole position, $ (+ = received)
        pnl = open_credit + fill["close_cash"]
        reason = (
            f"auto-reconcile 2026: closing order {fill['close_order_id']} "
            f"({fill['contracts']}x) flattened all legs at the broker; Paco's "
            f"cycle never wrote the close back. pnl = open credit {open_credit:+.2f} "
            f"+ close cash {fill['close_cash']:+.2f} (excl. fees)."
        )
        if _autoclose_spread(supabase_env, int(s["id"]), pnl, fill["closed_at"], reason):
            healed.add(int(s["id"]))
            print(
                f"AUTO-CLOSED spread #{s['id']} ({s.get('symbol')}, "
                f"{sorted(legs)}): matched closing order {fill['close_order_id']}, "
                f"pnl={pnl:+.2f}. No estop.",
            )
    return healed


def _engage_estop(reason: str) -> None:
    print(f"RECONCILE MISMATCH -- engaging estop: {reason}", file=sys.stderr)
    if not _ZEROCLAW_BIN.exists():
        print(f"WARNING: {_ZEROCLAW_BIN} not found -- could not engage estop", file=sys.stderr)
        return
    result = subprocess.run(
        [str(_ZEROCLAW_BIN), "estop", "--level", "kill-all"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: estop engage failed: {result.stderr.strip()}", file=sys.stderr)
    else:
        print("Estop engaged -- Paco's tool calls are blocked until a human resumes "
              "(`zeroclaw estop resume`) after investigating.", file=sys.stderr)


def main() -> int:
    _backup_tracking_db()

    try:
        supabase_env = _load_env(_SUPABASE_ENV)
        alpaca_env = _load_env(_ALPACA_ENV)
    except FileNotFoundError as exc:
        _engage_estop(f"could not load required .env: {exc}")
        return 2

    try:
        import os
        for k, v in alpaca_env.items():
            os.environ[k] = v
        from alpaca_client import AlpacaClient

        client = AlpacaClient()
        positions = client.get_positions()
    except Exception as exc:
        _engage_estop(f"broker positions unavailable: {exc}")
        return 2

    try:
        conn = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"],
            port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"],
            password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"select * from {_SCHEMA}.spreads where status = 'open'")
                open_spreads = list(cur.fetchall())
        conn.close()
    except Exception as exc:
        _engage_estop(f"local book unavailable: {exc}")
        return 2

    broker_syms = _option_symbols_from_positions(positions)
    db_syms = _db_leg_symbols(open_spreads)

    phantom = db_syms - broker_syms
    orphan = broker_syms - db_syms
    leg_issues = _leg_consistency_issues(open_spreads, positions)

    # Self-heal the benign case (a clean close Paco's cycle didn't write
    # back) BEFORE deciding whether to escalate. Anything that can't be
    # matched to an exact closing fill stays in `phantom` and still trips
    # the estop below.
    healed_ids: set[int] = set()
    if phantom:
        try:
            healed_ids = _try_selfheal_phantoms(
                open_spreads, phantom, orphan, leg_issues, client, supabase_env,
            )
        except Exception as exc:
            print(f"WARNING: self-heal pass errored, treating all phantoms as real: {exc}",
                  file=sys.stderr)
        if healed_ids:
            open_spreads = [s for s in open_spreads if int(s["id"]) not in healed_ids]
            db_syms = _db_leg_symbols(open_spreads)
            phantom = db_syms - broker_syms

    if phantom or orphan or leg_issues:
        reasons = []
        if phantom:
            reasons.append(f"DB-open legs missing at broker: {sorted(phantom)}")
        if orphan:
            reasons.append(f"broker option legs missing from DB: {sorted(orphan)}")
        reasons.extend(leg_issues)
        _engage_estop("; ".join(reasons))
        return 1

    healed_note = f" ({len(healed_ids)} benign close(s) auto-reconciled)" if healed_ids else ""
    print(
        f"Reconciliation OK: {len(broker_syms)} broker option leg(s), "
        f"{len(open_spreads)} DB-open spread(s).{healed_note}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
