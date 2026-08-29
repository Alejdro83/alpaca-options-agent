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
"""
from __future__ import annotations

import logging

import db

logger = logging.getLogger(__name__)


def _legs_for_spread(spread: dict) -> list[str]:
    """All option symbols for one open spread row -- 2 for a vertical, 4
    for an iron condor (put side + call side)."""
    legs = [spread["short_symbol"], spread["long_symbol"]]
    if spread.get("strategy") == "iron_condor":
        legs += [spread["call_short_symbol"], spread["call_long_symbol"]]
    return [l for l in legs if l]


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

    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    per_spread: list[dict] = []
    legs_missing_greeks = 0

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
        per_spread.append({
            "spread_id": s["id"], "underlying": s["underlying"],
            "strategy": s.get("strategy", "vertical"), **{k: round(v, 4) for k, v in spread_net.items()},
        })

    if legs_missing_greeks:
        logger.info(
            "portfolio_greeks: %d leg(s) had no broker greeks available (indicative feed only "
            "populates greeks for held positions -- this can lag right after a fresh open)",
            legs_missing_greeks,
        )

    db.record_portfolio_greeks_snapshot(
        net_delta=round(net["delta"], 4), net_gamma=round(net["gamma"], 4),
        net_theta=round(net["theta"], 4), net_vega=round(net["vega"], 4),
        net_rho=round(net["rho"], 4), per_spread=per_spread,
    )
