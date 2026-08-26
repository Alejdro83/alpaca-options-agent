"""Deterministic risk gate — the hard backstop the LLM decision layer cannot
override. This is what the hackathon's required one-pager's "risk gates"
section describes: every one of these checks runs in plain Python, after
the LLM has proposed a trade, not as a prompt instruction the model could
ignore or rationalize past.

Gates, in order, any one of which blocks the trade:
1. Daily loss circuit breaker — no new spreads once today's account P&L
   breaches -max_daily_loss_pct (mirrors trading_bot/config.py's own
   validated 3% breaker).
2. Max concurrent spreads — caps total open positions regardless of how
   many attractive signals show up in one cycle.
3. Per-spread max loss — a spread whose defined max loss (width - credit)
   exceeds max_loss_per_spread_pct of current equity is rejected outright,
   never resized down silently (a silently-shrunk position is a different
   trade than the one that was reasoned about).
4. DTE window — rejects anything outside [min_dte, max_dte], since the
   entire judged window is ~5 trading days and this keeps every position's
   fate resolved on a timescale the judges can actually see.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from config import config


@dataclass
class RiskCheckResult:
    allowed: bool
    reasons: list[str]


def check_new_spread(
    *,
    equity: float,
    daily_pl_pct: float,
    open_spreads_count: int,
    max_loss: float,
    expiration: date,
    today: date,
) -> RiskCheckResult:
    reasons: list[str] = []
    limits = config.risk

    if daily_pl_pct <= -limits.max_daily_loss_pct:
        reasons.append(
            f"daily P&L {daily_pl_pct:.2%} already breaches the "
            f"-{limits.max_daily_loss_pct:.0%} circuit breaker"
        )

    if open_spreads_count >= limits.max_concurrent_spreads:
        reasons.append(
            f"{open_spreads_count} spreads already open, "
            f"at the {limits.max_concurrent_spreads} concurrent cap"
        )

    max_loss_cap = equity * limits.max_loss_per_spread_pct
    if max_loss > max_loss_cap:
        reasons.append(
            f"max loss ${max_loss:,.2f} exceeds the "
            f"{limits.max_loss_per_spread_pct:.0%} of equity cap (${max_loss_cap:,.2f})"
        )

    dte = (expiration - today).days
    if not (limits.min_dte <= dte <= limits.max_dte):
        reasons.append(
            f"{dte} DTE is outside the allowed [{limits.min_dte}, {limits.max_dte}] window"
        )

    return RiskCheckResult(allowed=not reasons, reasons=reasons)


def should_close(
    *,
    credit_received: float,
    current_mark: float,
    is_credit_spread: bool = True,
) -> tuple[bool, str] | tuple[bool, None]:
    """`current_mark` is the current cost to close (debit to buy back the
    spread). Credit spreads profit as this shrinks toward zero.
    """
    limits = config.risk
    profit_captured_pct = 1 - (current_mark / credit_received) if credit_received else 0
    if profit_captured_pct >= limits.profit_target_pct:
        return True, f"profit target hit: {profit_captured_pct:.0%} of max credit captured"
    if current_mark >= credit_received * limits.stop_loss_multiple:
        return True, (
            f"stop hit: cost to close (${current_mark:.2f}) reached "
            f"{limits.stop_loss_multiple}x credit received (${credit_received:.2f})"
        )
    return False, None
