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

`should_force_close` is a separate, unconditional exit trigger (not part of
the entry gate above): a spread opened late in the week could otherwise
still be open, unrealized, and undemonstrated when the contest ends — this
closes it regardless of profit/loss once expiration or the contest deadline
is imminent (2026-08-26 research pass; see ONE_PAGER.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

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
    existing_exposure: dict[str, float] | None = None,
    underlying: str | None = None,
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

    if existing_exposure is not None and underlying is not None:
        projected_exposure = existing_exposure.get(underlying, 0) + max_loss
        concentration_cap = equity * limits.max_concentration_pct
        if projected_exposure > concentration_cap:
            reasons.append(
                f"projected exposure ${projected_exposure:.2f} for {underlying} "
                f"exceeds {limits.max_concentration_pct:.0%} concentration cap "
                f"(${concentration_cap:.2f})"
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


def should_force_close(
    *,
    expiration: date,
    now_utc: datetime | None = None,
) -> tuple[bool, str] | tuple[bool, None]:
    """Unconditional exit — fires independent of should_close's profit/loss
    checks. Two triggers, either sufficient on its own:
    1. Expiration is tomorrow or sooner (assignment/pin risk on American-
       style equity options isn't worth carrying into the final session).
    2. The contest deadline itself is within 2 hours — nothing should still
       be open, undemonstrated, when judging starts.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.date()

    dte = (expiration - today).days
    if dte <= 1:
        return True, f"force-close: only {dte} day(s) to expiration"

    contest_end = datetime.fromisoformat(config.risk.contest_end_utc)
    if now_utc >= contest_end - timedelta(hours=2):
        return True, "force-close: contest deadline is within 2 hours"

    return False, None
