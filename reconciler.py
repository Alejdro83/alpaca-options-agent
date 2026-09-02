"""Broker <-> local-book reconciliation.

Real gap found 2026-08-29 comparing against a competing team's (rookieriot)
weekend hardening pass. This project already had one incident from this
exact failure class (2026-08-27: a rejected order still produced a
phantom "open" row in Supabase — see `_extract_order_ids`'s docstring) —
that particular hole is closed, but nothing has ever cross-checked our
running `spreads` table against what Alpaca actually holds on an ongoing
basis. Alpaca is the source of truth; the local `spreads` table is a
journal that must match it. Unexplained divergence blocks new entries
(never blocks managing/closing what's already known-open — the force-close
deadline and stop-loss/profit-target logic must keep running regardless).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import db

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    broker_option_symbols: set[str] = field(default_factory=set)
    db_leg_symbols: set[str] = field(default_factory=set)

    @property
    def reason(self) -> str | None:
        return "; ".join(self.reasons) if self.reasons else None


def _option_symbols_from_positions(positions: list[dict[str, Any]]) -> set[str]:
    """Option OCC symbols contain digits and run well past 6 chars;
    equity tickers on this account never do. Conservative heuristic (same
    one rookieriot uses) rather than trusting an `asset_class` field the
    client wrapper doesn't currently surface.
    """
    out: set[str] = set()
    for p in positions:
        sym = str(p.get("symbol") or "")
        if not sym:
            continue
        if any(ch.isdigit() for ch in sym) or len(sym) > 6:
            out.add(sym)
    return out


def _db_leg_symbols(spreads: list[dict[str, Any]]) -> set[str]:
    legs: set[str] = set()
    for s in spreads:
        for col in ("short_symbol", "long_symbol", "call_short_symbol", "call_long_symbol"):
            if s.get(col):
                legs.add(s[col])
    return legs


# 2026-08-30 fix (found auditing Paco's own copy of this same pattern for
# a real gap -- see the sibling repo's mcp_risk_proxy/reconcile_paco.py):
# the symbol-set check above only proves every expected symbol EXISTS
# somewhere at the broker. It says nothing about quantity or direction --
# a short leg quietly filled at 5 contracts against a recorded 1 (or a
# leg that ended up on the wrong side) would pass silently. Unlike Paco's
# equivalent, this project's `spreads.contracts` column IS real ground
# truth (recorded at open, confirmed against the real fill since
# a68fa33), so this compares broker qty against what was actually
# intended, not just legs against each other.
_LEG_ROLES = {
    "short_symbol": "short", "long_symbol": "long",
    "call_short_symbol": "short", "call_long_symbol": "long",
}


def _leg_consistency_issues(spreads: list[dict[str, Any]], positions: list[dict[str, Any]]) -> list[str]:
    by_symbol = {p["symbol"]: p for p in positions}

    # Aggregate the expected NET signed position per leg symbol across ALL
    # open rows before comparing to the broker. The same structure can
    # legitimately be opened more than once (2026-09-02: two identical
    # 4-lot SMCI bear calls, cycles 208 and 210 -- separate fills, order
    # ids and rows) and Alpaca nets them into one position per symbol.
    # The old check compared each row's `contracts` against the broker's
    # *total* for that symbol and so false-positived on every row after
    # the first, hard-blocking new entries for hours. A signed sum (short
    # leg = -n, long leg = +n) also still catches a leg that ended up on
    # the wrong side, which a bare magnitude check would miss.
    expected_signed: dict[str, float] = {}
    rows_for_symbol: dict[str, list[str]] = {}
    for s in spreads:
        n = int(s.get("contracts") or 1)
        label = str(s.get("id", s.get("underlying", "?")))
        for col, role in _LEG_ROLES.items():
            symbol = s.get(col)
            if not symbol:
                continue
            expected_signed[symbol] = expected_signed.get(symbol, 0.0) + (-n if role == "short" else n)
            rows_for_symbol.setdefault(symbol, []).append(f"#{label}")

    issues: list[str] = []
    for symbol, expected in expected_signed.items():
        pos = by_symbol.get(symbol)
        if pos is None:
            continue  # phantom leg, already reported above
        actual = float(pos.get("qty") or 0)
        # Some SDK versions hand back an unsigned qty through the wrapper;
        # fall back to the explicit side field to restore the sign.
        if actual > 0 and str(pos.get("side") or "") == "short":
            actual = -actual
        if actual != expected:
            rows = ", ".join(rows_for_symbol[symbol])
            issues.append(
                f"{symbol} (spread {rows}): DB expects net {expected:+g} "
                f"contract(s), broker holds {actual:+g}"
            )
    return issues


def reconcile(client) -> ReconcileResult:
    """Compare Alpaca option positions to the DB's open spreads.

    - Legs in DB but not at the broker -> phantom local row (block).
    - Legs at the broker but not in DB -> orphaned broker position (block).
    Fails closed: any error fetching either side blocks new entries rather
    than assuming everything matches.
    """
    try:
        positions = client.get_positions()
    except Exception as exc:
        logger.exception("Reconciliation: failed to fetch broker positions")
        return ReconcileResult(ok=False, reasons=[f"broker positions unavailable: {exc}"])

    try:
        open_spreads = db.get_open_spreads()
    except Exception as exc:
        logger.exception("Reconciliation: failed to fetch local open spreads")
        return ReconcileResult(ok=False, reasons=[f"local book unavailable: {exc}"])

    broker_syms = _option_symbols_from_positions(positions)
    db_syms = _db_leg_symbols(open_spreads)

    phantom = db_syms - broker_syms
    orphan = broker_syms - db_syms

    reasons: list[str] = []
    if phantom:
        reasons.append(f"DB-open legs missing at broker: {sorted(phantom)}")
    if orphan:
        reasons.append(f"broker option legs missing from DB: {sorted(orphan)}")
    reasons.extend(_leg_consistency_issues(open_spreads, positions))

    if reasons:
        logger.error("Reconciliation mismatch: %s", "; ".join(reasons))
    else:
        logger.info(
            "Reconciliation OK: %d broker option legs, %d DB-open spread(s)",
            len(broker_syms), len(open_spreads),
        )

    return ReconcileResult(ok=not reasons, reasons=reasons, broker_option_symbols=broker_syms, db_leg_symbols=db_syms)
