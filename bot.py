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
from signals.indicators import compute_atr
from signals.swing import generate_swing_signals
from signals.trend_filter import TrendFilter

import black_scholes
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


def _passes_volatility_filter(bars_df: pd.DataFrame) -> bool:
    """Realized-volatility-percentile proxy for true IV rank (2026-08-26
    research pass — see config.VolatilityFilter's docstring for why a proxy,
    not the real thing). Reuses the same ~400-day daily bars the trend
    filter already fetched, no extra API calls: ranks today's N-day ATR%
    against its own trailing-year distribution, and requires it to sit at or
    above the configured percentile before entering a NEW credit spread —
    elevated realized vol is a reasonable stand-in for "premium is rich
    relative to its own recent history," which is what actually matters for
    a premium seller.
    """
    vol_cfg = config.volatility
    if not vol_cfg.enabled:
        return True
    atr = compute_atr(bars_df["high"], bars_df["low"], bars_df["close"], period=vol_cfg.lookback_window)
    atr_pct = (atr / bars_df["close"]).dropna()
    if len(atr_pct) < vol_cfg.lookback_window * 2:
        # Not enough history to rank meaningfully — fail open rather than
        # silently blocking every candidate for a newly-listed or thin name.
        return True
    percentile = atr_pct.rank(pct=True).iloc[-1]
    return percentile >= vol_cfg.min_percentile


def _fetch_daily_bars(client: AlpacaClient, ticker: str) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=TREND_FILTER_LOOKBACK_DAYS)
    bars = client.get_bars(
        ticker, TimeFrame.Day,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        limit=300,
    )
    return pd.DataFrame(bars)


def _apply_trend_and_volatility_filters(client: AlpacaClient, signals: list) -> list[tuple]:
    """Returns (signal, realized_vol) pairs for survivors — realized_vol is
    the annualized estimate `spread_builder.build_spread` feeds into
    `black_scholes.bs_delta` as the IV proxy, computed here (not re-fetched
    later) since this is already pulling the daily bars it needs.
    """
    trend_filter = TrendFilter()
    kept = []
    for sig in signals:
        try:
            bars_df = _fetch_daily_bars(client, sig.ticker)
        except Exception:
            logger.exception("Failed to fetch bars for %s, skipping", sig.ticker)
            continue

        try:
            trend_result = trend_filter.check(bars_df, sig.direction)
            trend_allowed = trend_result.allowed
        except Exception:
            logger.exception("Trend filter failed for %s, allowing", sig.ticker)
            trend_allowed = True
        if not trend_allowed:
            continue

        try:
            if not _passes_volatility_filter(bars_df):
                logger.info("%s rejected: realized vol below the configured percentile", sig.ticker)
                continue
        except Exception:
            logger.exception("Volatility filter failed for %s, allowing", sig.ticker)

        realized_vol = black_scholes.realized_vol_from_bars(bars_df)
        kept.append((sig, realized_vol))
    return kept


async def manage_open_spreads(mcp: AlpacaMCP) -> list[str]:
    notes = []
    for spread in db.get_open_spreads():
        expiration = datetime.strptime(str(spread["expiration"]), "%Y-%m-%d").date()
        force_close, force_reason = risk_gate.should_force_close(expiration=expiration)

        try:
            mark = await executor_mcp.get_spread_mark(mcp, spread["short_symbol"], spread["long_symbol"])
        except Exception:
            logger.exception("Failed to get mark for spread %s", spread["id"])
            if not force_close:
                continue
            mark = None

        if force_close:
            should_close, reason = True, force_reason
        elif mark is None:
            continue
        else:
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
            if mark is None:
                # Force-closed without ever getting a fresh mark (quote fetch
                # failed) — still worth closing out ahead of expiration/the
                # contest deadline, but the realized P&L is genuinely unknown
                # until the fill confirms, not silently reported as $0.
                realized_pnl = None
                status = "closed_expiry"
                notes.append(f"Force-closed {spread['underlying']} {spread['direction']}: {reason} (P&L unknown, mark unavailable)")
            else:
                realized_pnl = float(spread["credit_received"]) - mark
                status = "closed_expiry" if force_close else ("closed_profit" if realized_pnl > 0 else "closed_stop")
                notes.append(f"Closed {spread['underlying']} {spread['direction']}: {reason} (P&L ${realized_pnl:+.2f})")
            db.record_spread_close(spread["id"], status, realized_pnl)
        except Exception as exc:
            logger.exception("Failed to close spread %s", spread["id"])
            notes.append(f"ERROR closing {spread['underlying']}: {exc}")
    return notes


async def find_candidates(mcp: AlpacaMCP, client: AlpacaClient, account: dict, open_count: int) -> list[dict]:
    universe = get_universe()
    filtered = filter_universe(universe, client)
    tickers = [c.symbol for c in filtered]
    signals = generate_swing_signals(tickers, client)
    signals_with_vol = _apply_trend_and_volatility_filters(client, signals)

    today = datetime.now(timezone.utc).date()
    candidates = []
    for sig, realized_vol in signals_with_vol:
        try:
            spot = client.get_latest_quote(sig.ticker)
            spot_mid = (spot["ask_price"] + spot["bid_price"]) / 2
            plan = await build_spread(mcp, sig.ticker, sig.direction, spot_price=spot_mid, realized_vol=realized_vol)
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
