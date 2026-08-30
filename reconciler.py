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

    if reasons:
        logger.error("Reconciliation mismatch: %s", "; ".join(reasons))
    else:
        logger.info(
            "Reconciliation OK: %d broker option legs, %d DB-open spread(s)",
            len(broker_syms), len(open_spreads),
        )

    return ReconcileResult(ok=not reasons, reasons=reasons, broker_option_symbols=broker_syms, db_leg_symbols=db_syms)
