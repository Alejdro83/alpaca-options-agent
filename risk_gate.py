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
from screening.correlation_clusters import cluster_for


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
    strategy: str = "vertical",
    open_iron_condor_count: int = 0,
    open_iron_condor_exposure: float = 0.0,
    cluster_exposure: dict[str, float] | None = None,
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

    # Separate, tighter cap for iron condors (2026-08-28) -- see
    # config.RiskLimits.max_concurrent_iron_condors' own docstring for why
    # this needs its own limit rather than relying on max_concurrent_spreads
    # alone.
    if strategy == "iron_condor" and open_iron_condor_count >= limits.max_concurrent_iron_condors:
        reasons.append(
            f"{open_iron_condor_count} iron condors already open, "
            f"at the {limits.max_concurrent_iron_condors} concurrent cap"
        )

    # Real equity-percentage cap on total iron condor exposure (2026-08-28)
    # -- aggregates max_loss*contracts across ALL open iron condors, not
    # just per-underlying. This is the functional gate that
    # max_concurrent_iron_condors' count-based approximation does NOT
    # provide (see its own docstring and config.py's capital-allocation
    # comment).
    if strategy == "iron_condor":
        ic_cap = equity * limits.max_iron_condor_equity_pct
        if open_iron_condor_exposure + max_loss > ic_cap:
            reasons.append(
                f"iron condor exposure ${open_iron_condor_exposure:,.2f} + "
                f"new max loss ${max_loss:,.2f} would exceed the "
                f"{limits.max_iron_condor_equity_pct:.0%} equity cap (${ic_cap:,.2f})"
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

    # Correlation-cluster concentration (2026-08-29, research pass) -- a
    # real gap the per-underlying cap above does NOT cover: several
    # concurrent spreads on DIFFERENT mega-cap tech names aren't
    # independent bets, since those names' pairwise correlation spikes in
    # stress (see screening/correlation_clusters.py). `underlying` not
    # being in any defined cluster (cluster_for returns None) means this
    # check simply doesn't apply -- it is never a rejection reason on its
    # own.
    if cluster_exposure is not None and underlying is not None:
        cluster = cluster_for(underlying)
        if cluster is not None:
            projected_cluster_exposure = cluster_exposure.get(cluster, 0) + max_loss
            cluster_cap = equity * limits.max_cluster_concentration_pct
            if projected_cluster_exposure > cluster_cap:
                reasons.append(
                    f"projected exposure ${projected_cluster_exposure:.2f} for the "
                    f"'{cluster}' correlation cluster (adding {underlying}) exceeds "
                    f"{limits.max_cluster_concentration_pct:.0%} cluster cap "
                    f"(${cluster_cap:.2f})"
                )

    return RiskCheckResult(allowed=not reasons, reasons=reasons)


def should_close(
    *,
    credit_received: float,
    current_mark: float,
    is_credit_spread: bool = True,
    width: float | None = None,
    stop_loss_multiple_override: float | None = None,
    disable_stop: bool = False,
) -> tuple[bool, str] | tuple[bool, None]:
    """For a CREDIT spread (is_credit_spread=True, the default -- unchanged
    behavior): `current_mark` is the current cost to close (debit to buy
    back the spread). Profits as this shrinks toward zero.

    For a DEBIT spread (is_credit_spread=False, added 2026-09-02 alongside
    the debit-spread overlay -- see spread_builder.build_debit_spread):
    `credit_received` is NEGATIVE (this project's storage convention --
    its magnitude is the debit paid), `current_mark` means PROCEEDS from
    closing right now (the mirror image of a credit spread's "cost to
    close" -- see executor_mcp.get_spread_mark's structure param), and
    `width` (the strike width in dollars, e.g. 500 for a $5-wide spread) is
    required to know the max possible gain. Mirrors the exact formula
    already proven on Paco's AGENTS.md closing procedure: profit target is
    measured against MAX GAIN (width - debit paid), not max credit, since a
    debit spread has no "credit" to capture a percentage of; the stop is a
    FLOOR on proceeds (they fall toward zero, not a ceiling they rise
    toward).

    `stop_loss_multiple_override`/`disable_stop` (2026-08-29, research
    pass, credit-spread-only -- see shadow_book.py's counterfactual
    policies): a single-source but concrete backtest finding suggested a
    MIDDLE stop-loss multiple like this project's real default (2x) may be
    the worst of both worlds for short-DTE credit spreads specifically --
    a tight stop (~1x) or no stop at all each outperformed a 2x stop in
    that backtest. Not strong enough evidence to change the real book's
    default on its own (single vendor source, not peer-reviewed) -- these
    params exist so shadow_book.py can run tight-stop/no-stop as
    counterfactual policies against real live decisions, same "let real
    data decide" approach already used for vertical-vs-iron-condor and
    shadow-vs-random. Default behavior (no args passed) is completely
    unchanged. `disable_stop` also works for a debit spread's floor;
    `stop_loss_multiple_override` has no debit-spread equivalent yet (no
    counterfactual policy needs it there today).
    """
    limits = config.risk

    if not is_credit_spread:
        if width is None:
            raise ValueError("should_close needs `width` for a debit spread (max possible gain at expiration)")
        debit_paid = -credit_received
        if debit_paid <= 0:
            # Malformed (credit_received wasn't actually negative) -- never
            # actionable, same "don't guess" discipline as the credit path.
            return False, None
        max_gain = width - debit_paid
        profit_captured_pct = (current_mark - debit_paid) / max_gain if max_gain > 0 else 0
        if profit_captured_pct >= limits.debit_profit_target_pct:
            return True, (
                f"profit target hit: {profit_captured_pct:.0%} of max gain captured "
                f"(proceeds ${current_mark:.2f} vs debit paid ${debit_paid:.2f})"
            )
        if not disable_stop:
            stop_floor = debit_paid * limits.debit_stop_pct
            if current_mark <= stop_floor:
                return True, (
                    f"stop hit: proceeds (${current_mark:.2f}) fell to "
                    f"{limits.debit_stop_pct:.0%} of debit paid (${debit_paid:.2f})"
                )
        return False, None

    profit_captured_pct = 1 - (current_mark / credit_received) if credit_received else 0
    if profit_captured_pct >= limits.profit_target_pct:
        return True, f"profit target hit: {profit_captured_pct:.0%} of max credit captured"
    if not disable_stop:
        multiple = stop_loss_multiple_override if stop_loss_multiple_override is not None else limits.stop_loss_multiple
        if current_mark >= credit_received * multiple:
            return True, (
                f"stop hit: cost to close (${current_mark:.2f}) reached "
                f"{multiple}x credit received (${credit_received:.2f})"
            )
    return False, None


def is_near_stop(*, credit_received: float, current_mark: float, near_pct: float = 0.80) -> bool:
    """Early-warning signal for the adaptive cron frequency (2026-08-29) --
    True once `current_mark` has closed `near_pct` of the way to the stop
    threshold (`credit_received * stop_loss_multiple`), distinct from
    should_close's own hard 100% trigger. Lets run_cycle() check more
    often while a position is getting close to a real stop, without
    changing should_close's own trigger point at all.
    """
    if not credit_received:
        return False
    limits = config.risk
    stop_threshold = credit_received * limits.stop_loss_multiple
    return current_mark >= stop_threshold * near_pct


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
