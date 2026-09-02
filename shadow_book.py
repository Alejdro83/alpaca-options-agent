"""Shadow book — counterfactual P&L tracking for alternative policies,
evaluated on the same gate-approved candidate menu (or the same real fill)
the real LLM cycle saw.

Each cycle, after the real decision is recorded, `open_counterfactuals`
opens virtual positions for the `shadow` (mechanical rule) and `random`
policies. `manage_open` marks everything to market and closes by the same
risk_gate rules as the real book — never touching Alpaca for orders, only
reading quotes via executor_mcp.get_spread_mark / get_iron_condor_mark.

`open_llm_mirror` (2026-08-29, research pass) is a second, independent
kind of counterfactual: it mirrors the REAL LLM pick (same underlying,
strike, credit, contracts as the position bot.py actually opened) under
two alternative EXIT rules instead of alternative SELECTION rules --
`llm_tight_stop` (1x credit stop) and `llm_no_stop` (no stop-loss trigger
at all, only profit target / force-close). This tests a specific,
concrete research finding (single-source, not peer-reviewed, so not
strong enough to change the real book's own 2x stop outright): for
short-DTE credit spreads, a tight stop or no stop each reportedly beat a
middle-ground multiple like 2x. `manage_open` applies the matching
risk_gate.should_close override per policy.

Every function is wrapped in try/except that logs and swallows — this
module must NEVER be able to affect the real trading path, matching the
non-fatal-failure convention already used throughout run_cycle().

Bug fixed 2026-08-28 (real book): P&L was computed per-contract but
recorded without multiplying by contracts held. The shadow book always
multiplies by `contracts` — see manage_open's realized_pnl calculation.
"""
from __future__ import annotations

import logging
import random as _random
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

import psycopg2
import psycopg2.extras

from config import config

logger = logging.getLogger(__name__)


@contextmanager
def _connection() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(
        host=config.supabase.db_host,
        port=config.supabase.db_port,
        dbname=config.supabase.db_name,
        user=config.supabase.db_user,
        password=config.supabase.db_password,
        sslmode="require",
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _schema() -> str:
    return config.supabase.schema


def record_open(
    cycle_id: int | None,
    policy: str,
    candidate: dict,
    plan: Any,
    contracts: int,
    same_as_llm: bool,
) -> int | None:
    """Writes one row to shadow_positions. Branches on plan.direction to
    distinguish vertical (SpreadPlan) from iron_condor (IronCondorPlan)
    — same check run_cycle() already uses.

    Returns the new row id, or None on failure (non-fatal).
    """
    # Debit-spread candidates (2026-09-02 overlay) are skipped here rather
    # than mirrored: `shadow_positions` has no `structure` column, and
    # manage_open below reads marks/should_close with the credit-spread
    # convention hardcoded (mirrors_get_spread_mark's default) -- opening
    # one here would silently mismark it (proceeds read as a cost), the
    # same class of gap iron condors would have hit before this module's
    # own strategy-branching was added. This module never touches the real
    # trading path either way (see module docstring) -- a missing shadow/
    # random/mirror row for one candidate is a small, honest gap in the
    # comparison, not a real-money risk.
    if getattr(plan, "structure", "credit") == "debit":
        logger.info("Shadow book: skipping debit-spread candidate %s (not yet supported here)", plan.underlying)
        return None
    is_iron_condor = plan.direction == "iron_condor"

    if is_iron_condor:
        short_strike = plan.short_put_strike
        long_strike = plan.long_put_strike
        short_symbol = plan.short_put_symbol
        long_symbol = plan.long_put_symbol
        call_short_strike = plan.short_call_strike
        call_long_strike = plan.long_call_strike
        call_short_symbol = plan.short_call_symbol
        call_long_symbol = plan.long_call_symbol
        direction = None
    else:
        short_strike = plan.short_strike
        long_strike = plan.long_strike
        short_symbol = plan.short_symbol
        long_symbol = plan.long_symbol
        call_short_strike = None
        call_long_strike = None
        call_short_symbol = None
        call_long_symbol = None
        direction = plan.direction

    try:
        with _connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {_schema()}.shadow_positions
                    (policy, cycle_id, underlying, strategy, direction, expiration,
                     short_strike, long_strike, short_symbol, long_symbol,
                     call_short_strike, call_long_strike, call_short_symbol, call_long_symbol,
                     contracts, credit_received, max_loss, status, same_as_llm)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
                returning id
                """,
                (
                    policy,
                    cycle_id,
                    plan.underlying,
                    "iron_condor" if is_iron_condor else "vertical",
                    direction,
                    plan.expiration if isinstance(plan.expiration, str) else plan.expiration.isoformat(),
                    short_strike, long_strike, short_symbol, long_symbol,
                    call_short_strike, call_long_strike, call_short_symbol, call_long_symbol,
                    contracts,
                    plan.credit_estimate,
                    plan.max_loss,
                    same_as_llm,
                ),
            )
            row = cur.fetchone()
            return row[0]
    except Exception:
        logger.exception("Failed to record shadow position (non-fatal)")
        return None


def get_open() -> list[dict[str, Any]]:
    """All status='open' shadow positions."""
    with _connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"select * from {_schema()}.shadow_positions where status = 'open' order by opened_at")
        return list(cur.fetchall())


def _close_position(position_id: int, status: str, realized_pnl: float | None) -> None:
    """Close a shadow position — update status, realized_pnl, closed_at,
    and clear unrealized_mark. Mirrors db.record_spread_close's shape.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.shadow_positions
            set status = %s, realized_pnl = %s, unrealized_mark = null, closed_at = now()
            where id = %s
            """,
            (status, realized_pnl, position_id),
        )


def _update_unrealized(position_id: int, unrealized_mark: float) -> None:
    """Update the unrealized mark on a still-open shadow position."""
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.shadow_positions
            set unrealized_mark = %s
            where id = %s
            """,
            (unrealized_mark, position_id),
        )


def open_counterfactuals(
    cycle_id: int | None,
    candidates: list[dict],
    llm_selected: list[str],
    shadow_selected: list[str],
    equity: float,
    max_risk_pct: float,
) -> None:
    """Called once per cycle from run_cycle(), right after the real decision
    journal is recorded. Builds the random selection (matched trade count,
    seeded deterministically from cycle_id), then calls record_open for
    both policies' picks.

    The random policy picks from the full candidate list (same menu the
    LLM saw), with the same number of trades as the LLM actually took —
    isolating SELECTION quality, not trade frequency. If the LLM abstained,
    random abstains too.

    Wrapped in try/except — must never affect the real trading path.
    """
    try:
        _open_counterfactuals_inner(
            cycle_id, candidates, llm_selected, shadow_selected, equity, max_risk_pct,
        )
    except Exception:
        logger.exception("Shadow book: open_counterfactuals failed (non-fatal)")


def _open_counterfactuals_inner(
    cycle_id: int | None,
    candidates: list[dict],
    llm_selected: list[str],
    shadow_selected: list[str],
    equity: float,
    max_risk_pct: float,
) -> None:
    llm_set = set(llm_selected)

    # Build candidate lookup by ticker
    by_ticker: dict[str, dict] = {}
    for c in candidates:
        by_ticker[c["ticker"]] = c

    # Shadow policy: use _shadow_select's picks (already computed)
    for ticker in shadow_selected:
        c = by_ticker.get(ticker)
        if c is None:
            continue
        plan = c["_plan"]
        contracts = _optimal_contracts(equity, plan.max_loss, max_risk_pct)
        record_open(
            cycle_id=cycle_id,
            policy="shadow",
            candidate=c,
            plan=plan,
            contracts=contracts,
            same_as_llm=ticker in llm_set,
        )

    # Random policy: matched trade count, deterministic seed
    num_llm_trades = len(llm_selected)
    if num_llm_trades > 0 and candidates:
        rng = _random.Random(cycle_id)
        random_picks = rng.sample(candidates, min(num_llm_trades, len(candidates)))
        for c in random_picks:
            plan = c["_plan"]
            contracts = _optimal_contracts(equity, plan.max_loss, max_risk_pct)
            record_open(
                cycle_id=cycle_id,
                policy="random",
                candidate=c,
                plan=plan,
                contracts=contracts,
                same_as_llm=c["ticker"] in llm_set,
            )


def open_llm_mirror(cycle_id: int | None, candidate: dict, plan: Any, contracts: int) -> None:
    """Mirrors a REAL LLM pick under the two exit-rule counterfactual
    policies (2026-08-29, research pass) -- called right after bot.py's
    real open succeeds, with the exact same plan/contracts as the real
    position, so `llm_tight_stop`/`llm_no_stop` diverge from the real book
    ONLY in how they exit, never in what/how much was entered. Wrapped in
    try/except -- must never affect the real trading path.
    """
    try:
        record_open(
            cycle_id=cycle_id, policy="llm_tight_stop", candidate=candidate,
            plan=plan, contracts=contracts, same_as_llm=True,
        )
        record_open(
            cycle_id=cycle_id, policy="llm_no_stop", candidate=candidate,
            plan=plan, contracts=contracts, same_as_llm=True,
        )
    except Exception:
        logger.exception("Shadow book: open_llm_mirror failed (non-fatal)")


def _optimal_contracts(equity: float, max_loss_per_contract: float, max_risk_pct: float = 0.02) -> int:
    """Size contracts so total max loss stays within risk budget.
    Same function as bot._optimal_contracts — duplicated here to avoid a
    circular import (bot imports shadow_book, shadow_book can't import bot).
    """
    if max_loss_per_contract <= 0:
        return 1
    dollar_budget = equity * max_risk_pct
    contracts = int(dollar_budget // max_loss_per_contract)
    return max(contracts, 1)


async def manage_open(mcp) -> None:
    """Called once per cycle from run_cycle(), right after
    manage_open_spreads (the real book's mark/close loop).

    For each open shadow position: fetch a mark, check
    risk_gate.should_force_close (expiration) and risk_gate.should_close
    (profit target / stop), close or update unrealized_mark.

    Wrapped in try/except per-position AND around the whole function —
    never raises, matching the non-fatal convention.
    """
    try:
        await _manage_open_inner(mcp)
    except Exception:
        logger.exception("Shadow book: manage_open failed (non-fatal)")


async def _manage_open_inner(mcp) -> None:
    import executor_mcp
    import risk_gate

    positions = get_open()
    if not positions:
        return

    for pos in positions:
        try:
            expiration = datetime.strptime(str(pos["expiration"]), "%Y-%m-%d").date()
            force_close, force_reason = risk_gate.should_force_close(expiration=expiration)

            is_iron_condor = pos.get("strategy") == "iron_condor"

            try:
                if is_iron_condor:
                    mark = await executor_mcp.get_iron_condor_mark(
                        mcp,
                        short_put_symbol=pos["short_symbol"],
                        long_put_symbol=pos["long_symbol"],
                        short_call_symbol=pos["call_short_symbol"],
                        long_call_symbol=pos["call_long_symbol"],
                    )
                else:
                    mark = await executor_mcp.get_spread_mark(mcp, pos["short_symbol"], pos["long_symbol"])
            except Exception:
                logger.exception("Failed to get mark for shadow position %s", pos["id"])
                if not force_close:
                    continue
                mark = None

            if force_close:
                should_close, reason = True, force_reason
            elif mark is None:
                continue
            else:
                # Exit-rule counterfactuals (2026-08-29): llm_tight_stop/
                # llm_no_stop mirror a real LLM pick but exit differently --
                # 'shadow'/'random' (selection counterfactuals) keep the
                # real book's own default (2x) stop unchanged.
                policy = pos.get("policy")
                if policy == "llm_tight_stop":
                    should_close, reason = risk_gate.should_close(
                        credit_received=float(pos["credit_received"]),
                        current_mark=mark,
                        stop_loss_multiple_override=1.0,
                    )
                elif policy == "llm_no_stop":
                    should_close, reason = risk_gate.should_close(
                        credit_received=float(pos["credit_received"]),
                        current_mark=mark,
                        disable_stop=True,
                    )
                else:
                    should_close, reason = risk_gate.should_close(
                        credit_received=float(pos["credit_received"]),
                        current_mark=mark,
                    )
            if not should_close:
                if mark is not None:
                    # Update unrealized mark for dashboard display.
                    # Multiply by contracts: mark is per-contract.
                    contracts_held = int(pos.get("contracts") or 1)
                    unrealized = (float(pos["credit_received"]) - mark) * contracts_held
                    _update_unrealized(pos["id"], unrealized)
                continue

            # Close the position
            if mark is None:
                realized_pnl = None
                status = "closed_expiry"
            else:
                # credit_received/mark are both per-contract — multiply by
                # contracts held. Same bug fixed in the real book's
                # manage_open_spreads (2026-08-28).
                contracts_held = int(pos.get("contracts") or 1)
                realized_pnl = (float(pos["credit_received"]) - mark) * contracts_held
                status = "closed_expiry" if force_close else (
                    "closed_profit" if realized_pnl > 0 else "closed_stop"
                )
            _close_position(pos["id"], status, realized_pnl)
        except Exception:
            logger.exception("Shadow book: error processing position %s (non-fatal)", pos.get("id"))
