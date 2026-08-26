"""Turns a (ticker, direction) signal from the vendored screening/signals
modules into a concrete credit vertical spread: an expiration, a short
strike near the target delta, and a long strike `spread_width_dollars`
further out-of-the-money.

Bull put spread on a 'long' signal (sell a put, buy a further-OTM put —
profits if the underlying stays flat or rises). Bear call spread on a
'short' signal (sell a call, buy a further-OTM call — profits if the
underlying stays flat or falls). Both are defined-risk: max loss is fixed
at (width - credit received) the moment the spread opens, which is exactly
what risk_gate.check_new_spread checks against.

NOTE ON FIELD NAMES: Alpaca's `get_option_chain`/`get_option_snapshot` MCP
tools return contract data with strike/expiration/greeks fields — the exact
key names here (`strike_price`, `expiration_date`, `greeks.delta`,
`latest_quote.bid_price`/`ask_price`, and now also `open_interest` for the
liquidity gate) match Alpaca's documented options schema, but this has NOT
yet been smoke-tested against a live response (blocked on the hackathon's
dedicated account existing — see plan step "MCP smoke test" before wiring
this into the cron job). Treat the parsing helpers here as the first thing
to verify, not as already-proven — `smoke_test.py` should be extended to
print `open_interest` specifically alongside greeks once real credentials
exist.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from config import config
from mcp_client import AlpacaMCP

logger = logging.getLogger(__name__)


@dataclass
class SpreadPlan:
    underlying: str
    direction: str  # 'bull_put' | 'bear_call'
    expiration: date
    short_strike: float
    long_strike: float
    short_symbol: str
    long_symbol: str
    credit_estimate: float
    max_loss: float


def _mid(quote: dict) -> float | None:
    bid = quote.get("bid_price")
    ask = quote.get("ask_price")
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2


def _passes_liquidity(snap: dict) -> bool:
    """Per-contract liquidity gate (2026-08-26 research pass) — equity-level
    liquidity (ScreeningFilters.min_avg_volume) is a poor proxy for options
    liquidity specifically; a heavily-traded stock can still have a thin
    market on a given strike/expiration. Checked on every leg individually,
    never averaged across a spread — one illiquid leg makes the whole
    spread hard to exit cleanly regardless of how liquid the other leg is.
    """
    open_interest = snap.get("open_interest")
    if open_interest is None or int(open_interest) < config.risk.min_open_interest:
        return False
    quote = snap.get("latest_quote", {})
    mid = _mid(quote)
    if mid is None or mid <= 0:
        return False
    bid, ask = float(quote["bid_price"]), float(quote["ask_price"])
    spread_pct = (ask - bid) / mid
    return spread_pct <= config.risk.max_bid_ask_spread_pct


async def build_spread(mcp: AlpacaMCP, ticker: str, signal_direction: str) -> SpreadPlan | None:
    """signal_direction is the vendored Signal's own 'long'/'short' field.
    Returns None (never a half-built spread) if the chain doesn't have a
    clean expiration/strike pair in the configured windows — a skipped
    cycle is always safer than a guessed one.
    """
    limits = config.risk
    today = datetime.now().date()
    min_exp = today + timedelta(days=limits.min_dte)
    max_exp = today + timedelta(days=limits.max_dte)

    chain = await mcp.call(
        "get_option_chain",
        {
            "underlying_symbol": ticker,
            "expiration_date_gte": min_exp.isoformat(),
            "expiration_date_lte": max_exp.isoformat(),
        },
    )
    contracts = chain if isinstance(chain, list) else chain.get("contracts", chain.get("option_contracts", []))
    if not contracts:
        logger.info("No option contracts for %s in [%s, %s]", ticker, min_exp, max_exp)
        return None

    is_bull_put = signal_direction == "long"
    option_type = "put" if is_bull_put else "call"

    # Group by expiration, prefer the nearest one inside the window (more
    # theta decay realized within the judged period).
    same_type = [c for c in contracts if c.get("type", c.get("option_type", "")).lower() == option_type]
    if not same_type:
        return None
    same_type.sort(key=lambda c: c.get("expiration_date", ""))
    chosen_expiration = same_type[0].get("expiration_date")
    exp_contracts = [c for c in same_type if c.get("expiration_date") == chosen_expiration]

    # Fetch snapshots (greeks + quotes) for this expiration's strikes to find
    # the one nearest the target short-leg delta.
    symbols = [c["symbol"] for c in exp_contracts if c.get("symbol")]
    snapshots = await mcp.call("get_option_snapshot", {"symbols": symbols})
    snap_by_symbol = snapshots if isinstance(snapshots, dict) else {s["symbol"]: s for s in snapshots}

    def delta_of(symbol: str) -> float | None:
        snap = snap_by_symbol.get(symbol, {})
        greeks = snap.get("greeks", {})
        d = greeks.get("delta")
        return abs(float(d)) if d is not None else None

    candidates = [(c, delta_of(c["symbol"])) for c in exp_contracts]
    candidates = [(c, d) for c, d in candidates if d is not None]
    if not candidates:
        logger.warning("No greeks available for %s %s chain, skipping", ticker, chosen_expiration)
        return None

    liquid_candidates = [
        (c, d) for c, d in candidates if _passes_liquidity(snap_by_symbol.get(c["symbol"], {}))
    ]
    if not liquid_candidates:
        logger.info(
            "%s %s chain has %d strikes but none pass the liquidity gate "
            "(min OI %d, max spread %.0f%%), skipping",
            ticker, chosen_expiration, len(candidates),
            limits.min_open_interest, limits.max_bid_ask_spread_pct * 100,
        )
        return None

    liquid_candidates.sort(key=lambda cd: abs(cd[1] - limits.short_leg_target_delta))
    short_contract, _ = liquid_candidates[0]
    short_strike = float(short_contract["strike_price"])

    # Long leg: `spread_width_dollars` further out-of-the-money than the
    # short strike — lower strike for a put spread (further OTM = lower),
    # higher strike for a call spread (further OTM = higher).
    target_long_strike = (
        short_strike - config.risk.spread_width_dollars
        if is_bull_put
        else short_strike + config.risk.spread_width_dollars
    )
    same_exp_by_strike = {float(c["strike_price"]): c for c in exp_contracts}
    if target_long_strike not in same_exp_by_strike:
        # Snap to the closest available strike rather than failing outright —
        # standard option chains aren't guaranteed to have every $5 increment.
        closest_strike = min(same_exp_by_strike, key=lambda k: abs(k - target_long_strike))
        target_long_strike = closest_strike
    long_contract = same_exp_by_strike[target_long_strike]
    if not _passes_liquidity(snap_by_symbol.get(long_contract["symbol"], {})):
        # Reject outright rather than silently walking to the next strike —
        # a silently-substituted long leg changes the spread's actual width
        # and max loss from what was reasoned about (2026-08-26 research pass).
        logger.info("%s long leg (%s) fails the liquidity gate, skipping", ticker, long_contract["symbol"])
        return None

    short_quote = snap_by_symbol.get(short_contract["symbol"], {}).get("latest_quote", {})
    long_quote = snap_by_symbol.get(long_contract["symbol"], {}).get("latest_quote", {})
    short_mid = _mid(short_quote)
    long_mid = _mid(long_quote)
    if short_mid is None or long_mid is None:
        logger.warning("Missing quotes for %s spread legs, skipping", ticker)
        return None

    credit_estimate = round((short_mid - long_mid) * 100, 2)  # per 1 contract, $ not cents
    width_dollars = abs(short_strike - target_long_strike) * 100
    max_loss = round(width_dollars - credit_estimate, 2)

    if credit_estimate <= 0:
        logger.info("%s spread has non-positive credit (%.2f), skipping", ticker, credit_estimate)
        return None

    return SpreadPlan(
        underlying=ticker,
        direction="bull_put" if is_bull_put else "bear_call",
        expiration=datetime.strptime(chosen_expiration, "%Y-%m-%d").date(),
        short_strike=short_strike,
        long_strike=target_long_strike,
        short_symbol=short_contract["symbol"],
        long_symbol=long_contract["symbol"],
        credit_estimate=credit_estimate,
        max_loss=max_loss,
    )
