"""Real broker-computed Greeks for currently open positions (2026-08-29,
from cross-checking Alpaca's Market Data OpenAPI spec against this
project's own long-standing "no broker Greeks available" finding).

That finding is still correct for CANDIDATE SELECTION: get_option_snapshot
returns null greeks/impliedVolatility for any contract not currently held
as a position -- confirmed live, repeatedly, across SPY/AAPL/JPM and even
NVDA strikes adjacent to ones actually held. spread_builder.py's
Black-Scholes proxy remains necessary there and this module changes
nothing about it.

What IS real and newly confirmed: Alpaca DOES return real, populated
greeks/impliedVolatility for contracts the account currently holds as
positions (verified live against the real NVDA iron condor: non-null
delta/gamma/theta/vega/rho and impliedVolatility on all 4 legs, and null
on adjacent NVDA strikes not held). This is a monitoring-only enrichment
on top of ALREADY-open positions -- it changes no decision, no execution,
no P&L calculation. Deliberately kept separate from manage_open_spreads
(the real close/P&L path) rather than folded into it, so a failure or bug
here can never affect a real close.

Also computes beta-weighted delta (2026-08-29, from reviewing a similar
open-source dashboard's approach): "this portfolio moves like N shares of
SPY" -- the standard tastytrade/thinkorswim framing, expressing net
delta-dollar exposure per underlying in SPY-equivalent share terms via
each underlying's beta against SPY. That other project's version used
Black-Scholes-derived Greeks with no visible beta source; this one
computes beta from real trailing daily returns (BETA_LOOKBACK_DAYS) rather
than a hardcoded/assumed table, consistent with this project's standing
rule against fabricating a number it can instead measure. Falls back to
beta=1.0 ("moves like the market") only when real history is genuinely
unavailable, always logged when it happens.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

BETA_LOOKBACK_DAYS = 95  # ~65 trading days after weekends/holidays -- enough for a stable regression
BETA_DEFAULT = 1.0  # fallback when real history is unavailable -- "moves like the market" is the honest default, not 0


def _legs_for_spread(spread: dict) -> list[str]:
    """All option symbols for one open spread row -- 2 for a vertical, 4
    for an iron condor (put side + call side)."""
    legs = [spread["short_symbol"], spread["long_symbol"]]
    if spread.get("strategy") == "iron_condor":
        legs += [spread["call_short_symbol"], spread["call_long_symbol"]]
    return [l for l in legs if l]


async def _fetch_daily_closes(mcp, symbol: str) -> dict[str, float] | None:
    """{date_str: close} for the trailing BETA_LOOKBACK_DAYS, or None on
    failure/insufficient data. Real bug caught building this (2026-08-29):
    get_stock_bars via MCP silently returns zero bars on this paper account
    without feed='iex' -- SIP is 403/empty on recent data for paper
    accounts, same reason alpaca_client.py's own get_bars() already forces
    DataFeed.IEX for the direct-SDK path. This is the first caller to hit
    the same gap through the MCP tool specifically.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=BETA_LOOKBACK_DAYS)
    try:
        result = await mcp.call("get_stock_bars", {
            "symbols": symbol, "timeframe": "1Day", "feed": "iex", "limit": 100,
            "start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d"),
        })
    except Exception:
        logger.exception("Failed to fetch bars for %s (beta calc)", symbol)
        return None
    bars = (result or {}).get("data", {}).get("bars", {}).get(symbol, [])
    if len(bars) < 20:
        return None
    return {b["t"][:10]: float(b["c"]) for b in bars}


def _returns_from_closes(closes: dict[str, float]) -> dict[str, float]:
    """Daily % returns keyed by the LATER date of each consecutive pair,
    so two return series can be aligned by date even if one has a gap the
    other doesn't (a real, if rare, data hazard -- never assume equal-length
    series line up positionally)."""
    dates = sorted(closes)
    returns = {}
    for prev, cur in zip(dates, dates[1:]):
        if closes[prev]:
            returns[cur] = (closes[cur] - closes[prev]) / closes[prev]
    return returns


def _compute_beta(stock_returns: dict[str, float], spy_returns: dict[str, float]) -> float | None:
    """beta = cov(stock, spy) / var(spy), over dates present in both series.
    None (not BETA_DEFAULT) when there isn't enough overlap -- the caller
    decides the fallback so this stays a pure, testable calculation.
    """
    shared_dates = sorted(set(stock_returns) & set(spy_returns))
    if len(shared_dates) < 15:
        return None
    x = [stock_returns[d] for d in shared_dates]
    y = [spy_returns[d] for d in shared_dates]
    spy_var = statistics.variance(y)
    if spy_var == 0:
        return None
    mean_x, mean_y = statistics.mean(x), statistics.mean(y)
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / (len(x) - 1)
    return cov / spy_var


async def record_portfolio_greeks(mcp) -> None:
    """Called once per cycle from run_cycle(), independent of
    manage_open_spreads. Fetches real greeks for every leg of every open
    spread in one batch call (well under get_option_snapshot's 100-symbol
    limit even at the max_concurrent_spreads=5 cap), sums them into net
    portfolio exposure, and records a snapshot. Never raises -- a failure
    here must never affect the real trading path.
    """
    try:
        await _record_portfolio_greeks_inner(mcp)
    except Exception:
        logger.exception("record_portfolio_greeks failed (non-fatal, monitoring only)")


async def _record_portfolio_greeks_inner(mcp) -> None:
    open_spreads = db.get_open_spreads()
    if not open_spreads:
        return

    all_symbols: list[str] = []
    for s in open_spreads:
        all_symbols.extend(_legs_for_spread(s))
    if not all_symbols:
        return

    result = await mcp.call(
        "get_option_snapshot",
        {"symbols": ",".join(sorted(set(all_symbols))), "feed": "indicative"},
    )
    snapshots = (result or {}).get("data", {}).get("snapshots", {})

    # Beta-weighted delta (2026-08-29): "how many SPY-equivalent shares does
    # this portfolio move like" -- computed from REAL trailing daily returns
    # (BETA_LOOKBACK_DAYS), never a hardcoded/assumed beta table, matching
    # this project's own standing rule against fabricating numbers it can
    # instead measure. One fetch for SPY, one per unique underlying held
    # (never redundant across multiple spreads on the same name).
    spy_closes = await _fetch_daily_closes(mcp, "SPY")
    spy_returns = _returns_from_closes(spy_closes) if spy_closes else {}
    spy_price = spy_closes[max(spy_closes)] if spy_closes else None  # most recent close

    beta_by_underlying: dict[str, float] = {}
    price_by_underlying: dict[str, float] = {}
    for underlying in sorted({s["underlying"] for s in open_spreads}):
        closes = await _fetch_daily_closes(mcp, underlying)
        if not closes:
            logger.info("portfolio_greeks: no bars for %s, beta defaults to %.1f", underlying, BETA_DEFAULT)
            beta_by_underlying[underlying] = BETA_DEFAULT
            price_by_underlying[underlying] = None
            continue
        price_by_underlying[underlying] = closes[max(closes)]
        beta = _compute_beta(_returns_from_closes(closes), spy_returns) if spy_returns else None
        if beta is None:
            logger.info("portfolio_greeks: insufficient overlap to compute beta for %s, defaulting to %.1f",
                        underlying, BETA_DEFAULT)
        beta_by_underlying[underlying] = beta if beta is not None else BETA_DEFAULT

    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    per_spread: list[dict] = []
    legs_missing_greeks = 0
    beta_weighted_delta = 0.0
    # Tracks whether EVERY spread got a real contribution -- one spread
    # missing price data must only mark the aggregate incomplete, never
    # silently stop later spreads (in iteration order) from contributing
    # their own share. Caught in review before this ever ran for real.
    beta_weighted_delta_complete = spy_price is not None

    for s in open_spreads:
        contracts = int(s.get("contracts") or 1)
        is_iron_condor = s.get("strategy") == "iron_condor"
        # position_intent per leg: short legs are -100 shares-equivalent
        # (sold, so their greeks contribute with a flipped sign to net
        # portfolio exposure), long legs are +100. Multiplying by
        # `contracts` scales to the real position size.
        leg_signs = [("short_symbol", -1), ("long_symbol", 1)]
        if is_iron_condor:
            leg_signs += [("call_short_symbol", -1), ("call_long_symbol", 1)]

        spread_net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        for field, sign in leg_signs:
            symbol = s.get(field)
            if not symbol:
                continue
            greeks = snapshots.get(symbol, {}).get("greeks")
            if greeks is None:
                legs_missing_greeks += 1
                continue
            for g in spread_net:
                spread_net[g] += sign * float(greeks.get(g, 0.0)) * contracts

        for g in net:
            net[g] += spread_net[g]

        # Beta-weighted delta contribution of this spread: share-equivalent
        # delta (option delta is per-share; one contract = 100 shares) times
        # the underlying's own price gives dollar delta; dividing by SPY's
        # price and scaling by beta expresses that dollar exposure in
        # "SPY-equivalent shares" -- the standard framing (tastytrade,
        # thinkorswim) for "this portfolio moves like N shares of SPY".
        underlying_price = price_by_underlying.get(s["underlying"])
        spread_beta_weighted = None
        if spy_price is not None and underlying_price:
            share_equiv_delta = spread_net["delta"] * 100
            dollar_delta = share_equiv_delta * underlying_price
            spread_beta_weighted = dollar_delta / spy_price * beta_by_underlying[s["underlying"]]
            beta_weighted_delta += spread_beta_weighted
        else:
            beta_weighted_delta_complete = False  # this spread's own price (or SPY's) is missing -- total is now a partial sum, not the real one

        per_spread.append({
            "spread_id": s["id"], "underlying": s["underlying"],
            "strategy": s.get("strategy", "vertical"), **{k: round(v, 4) for k, v in spread_net.items()},
            "beta": round(beta_by_underlying.get(s["underlying"], BETA_DEFAULT), 3),
            "beta_weighted_delta": round(spread_beta_weighted, 2) if spread_beta_weighted is not None else None,
        })

    if legs_missing_greeks:
        logger.info(
            "portfolio_greeks: %d leg(s) had no broker greeks available (indicative feed only "
            "populates greeks for held positions -- this can lag right after a fresh open)",
            legs_missing_greeks,
        )
    if not beta_weighted_delta_complete:
        logger.info("portfolio_greeks: beta-weighted delta is a partial sum (missing price data for some underlying)")

    db.record_portfolio_greeks_snapshot(
        net_delta=round(net["delta"], 4), net_gamma=round(net["gamma"], 4),
        net_theta=round(net["theta"], 4), net_vega=round(net["vega"], 4),
        net_rho=round(net["rho"], 4), per_spread=per_spread,
        beta_weighted_delta=round(beta_weighted_delta, 2) if beta_weighted_delta_complete else None,
    )
