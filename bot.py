"""Main cycle for the Alpaca AI Trading Agents Hackathon submission.

Pipeline, once per invocation (scheduled ~every 30min during market hours by
Hermes — see run_options_cron.sh):

  1. Manage existing open spreads: pull each one's current mark via MCP,
     apply risk_gate.should_close (profit target / stop), close via MCP if
     triggered, record to Supabase.
  2. Screen for new candidates: reuse trading_bot's vendored screening +
     swing-horizon signals + TrendFilter, unmodified.
  3. For each candidate that clears risk_gate.check_new_spread, build a
     concrete SpreadPlan via MCP (spread_builder).
  4. Hand the surviving, risk-approved candidates to llm_reasoner — the
     actual "autonomous AI agent" decision of which (if any) to act on.
  5. Open the LLM's selected spread(s) via MCP, record to Supabase.
  6. Record an account snapshot every cycle regardless of whether anything
     traded, so the dashboard's equity curve never has gaps.

Prints a short summary to stdout ONLY when something happened (opened,
closed, or errored) — mirrors trading_bot/run_paper_cron.sh's own
silent-unless-noteworthy convention, since Hermes forwards stdout to
Telegram and an empty-cycle spam every 30 minutes would be exactly the
"the agent is annoying" outcome that pattern was built to avoid.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.timeframe import TimeFrame

from alpaca_client import AlpacaClient
from config import config
from screening.universe import get_universe
from screening.filters import filter_universe
from signals.swing import generate_swing_signals
from signals.trend_filter import TrendFilter

import db
import executor_mcp
import llm_reasoner
import risk_gate
from mcp_client import AlpacaMCP
from spread_builder import build_spread

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Same lookback trading_bot/bot.py uses for its own trend filter — EMA200 +
# a buffer, in calendar days rather than trading days to survive
# weekends/holidays comfortably.
TREND_FILTER_LOOKBACK_DAYS = 400


def _apply_trend_filter(client: AlpacaClient, signals: list) -> list:
    trend_filter = TrendFilter()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=TREND_FILTER_LOOKBACK_DAYS)
    kept = []
    for sig in signals:
        try:
            bars = client.get_bars(
                sig.ticker, TimeFrame.Day,
                start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                limit=300,
            )
            result = trend_filter.check(pd.DataFrame(bars), sig.direction)
        except Exception:
            logger.exception("Trend filter failed for %s, allowing", sig.ticker)
            kept.append(sig)
            continue
        if result.allowed:
            kept.append(sig)
    return kept


async def manage_open_spreads(mcp: AlpacaMCP) -> list[str]:
    notes = []
    for spread in db.get_open_spreads():
        try:
            mark = await executor_mcp.get_spread_mark(mcp, spread["short_symbol"], spread["long_symbol"])
        except Exception:
            logger.exception("Failed to get mark for spread %s", spread["id"])
            continue
        if mark is None:
            continue
        should_close, reason = risk_gate.should_close(
            credit_received=float(spread["credit_received"]),
            current_mark=mark,
        )
        if not should_close:
            continue
        try:
            await executor_mcp.close_spread(
                mcp,
                short_symbol=spread["short_symbol"],
                long_symbol=spread["long_symbol"],
                contracts=spread["contracts"],
            )
            realized_pnl = float(spread["credit_received"]) - mark
            db.record_spread_close(spread["id"], "closed_profit" if realized_pnl > 0 else "closed_stop", realized_pnl)
            notes.append(f"Closed {spread['underlying']} {spread['direction']}: {reason} (P&L ${realized_pnl:+.2f})")
        except Exception as exc:
            logger.exception("Failed to close spread %s", spread["id"])
            notes.append(f"ERROR closing {spread['underlying']}: {exc}")
    return notes


async def find_candidates(mcp: AlpacaMCP, client: AlpacaClient, account: dict, open_count: int) -> list[dict]:
    universe = get_universe()
    filtered = filter_universe(universe, client)
    tickers = [c.symbol for c in filtered]
    signals = generate_swing_signals(tickers, client)
    signals = _apply_trend_filter(client, signals)

    today = datetime.now(timezone.utc).date()
    candidates = []
    for sig in signals:
        try:
            plan = await build_spread(mcp, sig.ticker, sig.direction)
        except Exception:
            logger.exception("Failed to build spread for %s", sig.ticker)
            continue
        if plan is None:
            continue
        check = risk_gate.check_new_spread(
            equity=float(account["equity"]),
            daily_pl_pct=float(account.get("daily_pl_pct") or 0.0),
            open_spreads_count=open_count,
            max_loss=plan.max_loss,
            expiration=plan.expiration,
            today=today,
        )
        if not check.allowed:
            logger.info("%s rejected by risk gate: %s", sig.ticker, check.reasons)
            continue
        candidates.append({
            "ticker": sig.ticker,
            "direction": sig.direction,
            "strength": sig.strength,
            "signal_reasoning": sig.reasoning,
            "credit_estimate": plan.credit_estimate,
            "max_loss": plan.max_loss,
            "expiration": plan.expiration.isoformat(),
            "_plan": plan,
        })
    return candidates


def _daily_pl(account: dict) -> tuple[float, float]:
    """`AlpacaClient.get_account()` only returns equity/last_equity, not a
    precomputed daily P&L — derived here rather than assuming a field the
    underlying client doesn't actually provide.
    """
    equity = float(account["equity"])
    last_equity = float(account["last_equity"])
    pl = equity - last_equity
    pl_pct = pl / last_equity if last_equity else 0.0
    return pl, pl_pct


async def run_cycle() -> None:
    client = AlpacaClient()
    account = client.get_account()
    daily_pl, daily_pl_pct = _daily_pl(account)
    account["daily_pl"] = daily_pl
    account["daily_pl_pct"] = daily_pl_pct

    async with AlpacaMCP() as mcp:
        close_notes = await manage_open_spreads(mcp)

        open_spreads = db.get_open_spreads()
        remaining_budget = max(0, config.risk.max_concurrent_spreads - len(open_spreads))

        open_notes = []
        candidates = []
        decision = "skipped"
        reasoning = "No eligible candidates this cycle."

        if remaining_budget > 0:
            candidates = await find_candidates(mcp, client, account, len(open_spreads))
            slim_candidates = [{k: v for k, v in c.items() if k != "_plan"} for c in candidates]
            outcome = llm_reasoner.decide(slim_candidates, remaining_budget)
            reasoning = outcome["reasoning"]
            selected_tickers = set(outcome["selected"])
            for c in candidates:
                if c["ticker"] not in selected_tickers:
                    continue
                plan = c["_plan"]
                try:
                    order_ids = await executor_mcp.open_spread(mcp, plan)
                    cycle_id = db.record_cycle(slim_candidates, "opened", reasoning)
                    db.record_spread_open(
                        underlying=plan.underlying,
                        direction=plan.direction,
                        expiration=plan.expiration.isoformat(),
                        short_strike=plan.short_strike,
                        long_strike=plan.long_strike,
                        short_symbol=plan.short_symbol,
                        long_symbol=plan.long_symbol,
                        contracts=1,
                        credit_received=plan.credit_estimate,
                        max_loss=plan.max_loss,
                        alpaca_order_ids=order_ids,
                        cycle_id=cycle_id,
                    )
                    open_notes.append(
                        f"Opened {plan.underlying} {plan.direction}: "
                        f"credit ${plan.credit_estimate:.2f}, max loss ${plan.max_loss:.2f}"
                    )
                    decision = "opened"
                except Exception as exc:
                    logger.exception("Failed to open spread for %s", plan.underlying)
                    open_notes.append(f"ERROR opening {plan.underlying}: {exc}")
                    decision = "error"

        if decision == "skipped":
            db.record_cycle([{k: v for k, v in c.items() if k != "_plan"} for c in candidates], decision, reasoning)

        db.record_account_snapshot(
            equity=float(account["equity"]),
            last_equity=float(account.get("last_equity")) if account.get("last_equity") else None,
            cash=float(account.get("cash")) if account.get("cash") else None,
            open_spreads_count=len(db.get_open_spreads()),
            daily_pl=float(account.get("daily_pl")) if account.get("daily_pl") else None,
            daily_pl_pct=float(account.get("daily_pl_pct")) if account.get("daily_pl_pct") else None,
        )

        for note in close_notes + open_notes:
            print(note)


if __name__ == "__main__":
    asyncio.run(run_cycle())
