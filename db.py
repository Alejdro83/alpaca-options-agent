"""Writes agent state to the `alpaca_hackathon` schema in Supabase — the
same Postgres project Agent Bazaar uses, kept in its own schema/namespace so
this hackathon's data never touches the marketplace's tables (see
supabase/alpaca_hackathon_schema.sql for the DDL).

Direct Postgres, not the Supabase REST API/PostgREST — `alpaca_hackathon`
isn't in that project's "exposed schemas" list (changing that needs a
dashboard setting only the account owner can flip), and direct Postgres
avoids that dependency entirely. The Vercel dashboard reads the same way,
server-side, via a Next.js API route — never from the browser.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
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


def record_cycle(
    candidates: list[dict[str, Any]],
    decision: str,
    reasoning: str,
    error: str | None = None,
    generation: int = 0,
) -> int:
    """Logs one Hermes tick. Returns the new cycle id so a resulting spread
    row can reference it — the dashboard's "last N decisions" view and the
    per-spread "why did the agent open this" trace both read off this link.

    `generation` tags which evolved-parameter generation (see
    overnight_evolution.py / evolution_history table) was active for this
    cycle — 0 means "no promoted evolution yet, running config.py
    defaults." Needed so a generation's REAL P&L can be measured later
    (get_realized_pnl_by_generation), not just its one-day simulated replay.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.cycles (candidates, decision, reasoning, error, generation)
            values (%s, %s, %s, %s, %s)
            returning id
            """,
            (json.dumps(candidates), decision, reasoning, error, generation),
        )
        row = cur.fetchone()
        return row[0]


def update_cycle_decision(cycle_id: int, decision: str, reasoning: str) -> None:
    """Corrects a cycle row written as a "pending" placeholder (needed early
    so record_spread_open has a cycle_id to reference) once the real outcome
    is known. Real bug fixed 2026-08-28: the previous version of this
    caller path hardcoded decision="opened" at insert time and only
    re-inserted a *second* row if the outcome turned out to be "skipped" —
    an "error" outcome (LLM picked a candidate but opening it raised) was
    never corrected and stayed mislabeled "opened" in the cycles table
    forever, and a "skipped" outcome left two rows for one cycle. This
    UPDATE replaces both re-insert paths so exactly one row exists per
    cycle with its true final decision.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.cycles
            set decision = %s, reasoning = %s
            where id = %s
            """,
            (decision, reasoning, cycle_id),
        )


def record_spread_open(
    underlying: str,
    direction: str,
    expiration: str,
    short_strike: float,
    long_strike: float,
    short_symbol: str,
    long_symbol: str,
    contracts: int,
    credit_received: float,
    max_loss: float,
    alpaca_order_ids: list[str],
    cycle_id: int,
    generation: int = 0,
    strategy: str = "vertical",
    structure: str = "credit",
    call_short_strike: float | None = None,
    call_long_strike: float | None = None,
    call_short_symbol: str | None = None,
    call_long_symbol: str | None = None,
) -> int:
    """`short_strike`/`long_strike`/`short_symbol`/`long_symbol` mean "the
    only side" for a vertical, and "the PUT side" for an iron condor; the
    call_* params (all None for a vertical) hold the call side — see
    supabase/alpaca_hackathon_schema_iron_condor.sql for the migration this
    matches. `strategy` defaults to 'vertical' to match the DB column's own
    default, but is passed explicitly here rather than relying on that
    default alone from the application code.

    `structure` ('credit' default | 'debit', 2026-09-02 overlay — see
    spread_builder.build_debit_spread / supabase/
    alpaca_hackathon_schema_debit.sql): for 'debit', `credit_received` is
    NEGATIVE (the debit paid) and `short_strike`/`short_symbol` mean the
    leg this project BOUGHT rather than sold — see SpreadPlan's own
    docstring for the full sign/role convention.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.spreads
                (underlying, direction, expiration, short_strike, long_strike,
                 short_symbol, long_symbol, contracts, credit_received, max_loss,
                 alpaca_order_ids, cycle_id, status, generation, strategy, structure,
                 call_short_strike, call_long_strike, call_short_symbol, call_long_symbol)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                underlying, direction, expiration, short_strike, long_strike,
                short_symbol, long_symbol,
                contracts, credit_received, max_loss, json.dumps(alpaca_order_ids), cycle_id,
                generation, strategy, structure,
                call_short_strike, call_long_strike, call_short_symbol, call_long_symbol,
            ),
        )
        row = cur.fetchone()
        return row[0]


def record_spread_close(spread_id: int, status: str, realized_pnl: float | None) -> None:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.spreads
            set status = %s, realized_pnl = %s, closed_at = now()
            where id = %s
            """,
            (status, realized_pnl, spread_id),
        )


def reconcile_spread_close(spread_id: int, fill_pnl: float) -> None:
    """Update realized_pnl with actual fill data instead of estimated marks.

    Call this after fetching real fill prices from Alpaca (e.g. via
    get_orders or portfolio history) to correct the estimate recorded
    at close time.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.spreads
            set realized_pnl = %s
            where id = %s
            """,
            (fill_pnl, spread_id),
        )


def get_open_spreads() -> list[dict[str, Any]]:
    with _connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"select * from {_schema()}.spreads where status = 'open' order by opened_at")
        return list(cur.fetchall())


def _ensure_decision_journal_table() -> None:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_schema()}.decision_journal (
                id SERIAL PRIMARY KEY,
                cycle_id INTEGER REFERENCES {_schema()}.cycles(id),
                candidates JSONB,
                llm_selected JSONB,
                llm_reasoning TEXT,
                shadow_selected JSONB,
                gate_rejections JSONB,
                pre_trade_rejections JSONB,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)


_decision_journal_ready = False


def record_decision_journal(
    cycle_id: int,
    candidates: list[dict],
    llm_selected: list[str],
    llm_reasoning: str,
    shadow_selected: list[str],
    gate_rejections: list[dict],
    pre_trade_rejections: list[dict],
) -> None:
    global _decision_journal_ready
    try:
        if not _decision_journal_ready:
            _ensure_decision_journal_table()
            _decision_journal_ready = True
        with _connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {_schema()}.decision_journal
                    (cycle_id, candidates, llm_selected, llm_reasoning,
                     shadow_selected, gate_rejections, pre_trade_rejections)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    cycle_id,
                    json.dumps(candidates),
                    json.dumps(llm_selected),
                    llm_reasoning,
                    json.dumps(shadow_selected),
                    json.dumps(gate_rejections),
                    json.dumps(pre_trade_rejections),
                ),
            )
    except Exception:
        logger.exception("Failed to record decision journal (non-fatal)")


def _ensure_evolution_history_table() -> None:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_schema()}.evolution_history (
                id SERIAL PRIMARY KEY,
                generation INTEGER NOT NULL,
                ran_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                decision TEXT NOT NULL,
                params_before JSONB,
                params_after JSONB,
                reason TEXT NOT NULL,
                simulated_metrics JSONB,
                real_metrics JSONB,
                reverted_at TIMESTAMPTZ,
                reverted_reason TEXT
            )
        """)


_evolution_history_ready = False


def record_evolution_history(
    generation: int,
    decision: str,
    params_before: dict | None,
    params_after: dict | None,
    reason: str,
    simulated_metrics: dict | None = None,
    real_metrics: dict | None = None,
) -> int:
    """Append-only audit trail for overnight_evolution.py — every night's
    run gets a row here, `decision` in ('promoted', 'held', 'auto_reverted',
    'manual_revert') for a real run, or ('would_promote', 'would_hold',
    'would_auto_revert') for a --dry-run run (2026-08-30) — the "would_*"
    prefix is deliberate so a real revert lookup (which matches the exact
    string 'promoted') can never mistake a dry-run row for a real one.
    Whether or not anything actually changed, this gets a row. Unlike
    evolved_params.json (which only ever holds the *current* state) or
    evolution_report.md (overwritten every run), this is never overwritten
    — the whole point is to be able to look back at every generation ever
    tried and why, and to revert to any of them (see revert_evolution.py).
    """
    global _evolution_history_ready
    if not _evolution_history_ready:
        _ensure_evolution_history_table()
        _evolution_history_ready = True
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.evolution_history
                (generation, decision, params_before, params_after, reason,
                 simulated_metrics, real_metrics)
            values (%s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                generation, decision,
                json.dumps(params_before) if params_before is not None else None,
                json.dumps(params_after) if params_after is not None else None,
                reason,
                json.dumps(simulated_metrics) if simulated_metrics is not None else None,
                json.dumps(real_metrics) if real_metrics is not None else None,
            ),
        )
        row = cur.fetchone()
        return row[0]


def mark_generation_reverted(generation: int, reverted_reason: str) -> None:
    """Marks the ('promoted', generation) row as reverted — found by
    generation number, latest first, so re-promoting the same generation
    number twice (shouldn't happen, generations are monotonic) still marks
    the most recent one.
    """
    global _evolution_history_ready
    if not _evolution_history_ready:
        _ensure_evolution_history_table()
        _evolution_history_ready = True
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            update {_schema()}.evolution_history
            set reverted_at = now(), reverted_reason = %s
            where id = (
                select id from {_schema()}.evolution_history
                where generation = %s and decision = 'promoted' and reverted_at is null
                order by ran_at desc limit 1
            )
            """,
            (reverted_reason, generation),
        )


def get_evolution_history(limit: int = 50) -> list[dict[str, Any]]:
    global _evolution_history_ready
    if not _evolution_history_ready:
        _ensure_evolution_history_table()
        _evolution_history_ready = True
    with _connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"select * from {_schema()}.evolution_history order by ran_at desc limit %s",
            (limit,),
        )
        return list(cur.fetchall())


def get_realized_pnl_by_generation(generation: int) -> dict[str, Any]:
    """Real (not simulated) trade count + total/avg realized_pnl for closed
    spreads opened under a given parameter generation — the actual evidence
    Layer 2's auto-revert check compares across generations, as opposed to
    overnight_evolution.py's own one-day synthetic replay.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            select count(*), coalesce(sum(realized_pnl), 0), coalesce(avg(realized_pnl), 0)
            from {_schema()}.spreads
            where generation = %s and status != 'open' and realized_pnl is not null
            """,
            (generation,),
        )
        count, total, avg = cur.fetchone()
        return {"generation": generation, "closed_trades": count, "total_pnl": float(total), "avg_pnl": float(avg)}


def record_account_snapshot(
    equity: float,
    last_equity: float | None,
    cash: float | None,
    open_spreads_count: int,
    daily_pl: float | None,
    daily_pl_pct: float | None,
    spy_price: float | None = None,
) -> None:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.account_snapshots
                (equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct, spy_price)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct, spy_price),
        )


def record_portfolio_greeks_snapshot(
    net_delta: float,
    net_gamma: float,
    net_theta: float,
    net_vega: float,
    net_rho: float,
    per_spread: list[dict],
    beta_weighted_delta: float | None = None,
) -> None:
    """Real broker-computed net portfolio Greeks (2026-08-29) -- see
    portfolio_greeks.py's own docstring. Monitoring only, best-effort:
    caller (portfolio_greeks.record_portfolio_greeks) already wraps this
    non-fatally, so a DB hiccup here never touches the real trading path.
    `beta_weighted_delta` is None when the caller couldn't get real price
    data for every underlying that cycle -- recorded as unknown, never as 0.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.portfolio_greeks_snapshots
                (net_delta, net_gamma, net_theta, net_vega, net_rho, per_spread, beta_weighted_delta)
            values (%s, %s, %s, %s, %s, %s, %s)
            """,
            (net_delta, net_gamma, net_theta, net_vega, net_rho, json.dumps(per_spread), beta_weighted_delta),
        )
