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
) -> int:
    """Logs one Hermes tick. Returns the new cycle id so a resulting spread
    row can reference it — the dashboard's "last N decisions" view and the
    per-spread "why did the agent open this" trace both read off this link.
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.cycles (candidates, decision, reasoning, error)
            values (%s, %s, %s, %s)
            returning id
            """,
            (json.dumps(candidates), decision, reasoning, error),
        )
        row = cur.fetchone()
        return row[0]


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
) -> int:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.spreads
                (underlying, direction, expiration, short_strike, long_strike,
                 short_symbol, long_symbol, contracts, credit_received, max_loss,
                 alpaca_order_ids, cycle_id, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')
            returning id
            """,
            (
                underlying, direction, expiration, short_strike, long_strike,
                short_symbol, long_symbol,
                contracts, credit_received, max_loss, json.dumps(alpaca_order_ids), cycle_id,
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


def record_account_snapshot(
    equity: float,
    last_equity: float | None,
    cash: float | None,
    open_spreads_count: int,
    daily_pl: float | None,
    daily_pl_pct: float | None,
) -> None:
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            insert into {_schema()}.account_snapshots
                (equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct),
        )
