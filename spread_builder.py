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

REAL API SHAPES (verified against the live account 2026-08-26, replacing an
earlier version's guessed field names — see git history for what was wrong):
- `get_option_contracts` (NOT get_option_chain) is the structural chain
  listing: response is `{"data": {"option_contracts": [...], "next_page_token": ...}}`,
  each contract a dict with STRING-typed `strike_price`/`open_interest`
  (nullable), plus `symbol`, `expiration_date`, `type` ("call"/"put").
  Param name is `underlying_symbols` (plural, comma-separated string),
  unlike get_option_chain's `underlying_symbol` (singular) — a real,
  easy-to-miss inconsistency in Alpaca's own tool schemas.
- `get_option_snapshot` response is `{"data": {"snapshots": {symbol: {...}}}}`
  — one level deeper than assumed originally — and each snapshot's quote is
  under camelCase `latestQuote: {bp, ap, bs, as, ...}` (bid/ask price/size),
  NOT `latest_quote.bid_price`/`ask_price`.
- NO GREEKS AVAILABLE on this account on any feed: `feed=opra` 403s with
  "OPRA agreement is not signed" (real-time OPRA data requires Alpaca's
  paid Algo Trader Plus subscription — confirmed via Alpaca's own forum,
  not just this account's error message), and `feed=indicative` (the free
  tier) returns a quote with no `greeks` key at all. Delta is computed
  in-process instead — see black_scholes.py's module docstring for why
  this is a reasonable proxy, not a hack.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from black_scholes import bs_delta
from config import config
from mcp_client import AlpacaMCP

logger = logging.getLogger(__name__)

_contract_cache: dict[tuple, list[dict]] = {}


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


def _mid_from_snapshot(snap: dict) -> float | None:
    quote = snap.get("latestQuote")
    if not quote:
        return None
    bid, ask = quote.get("bp"), quote.get("ap")
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2


LONG_LEG_MAX_SPREAD_PCT = 0.25


def _passes_liquidity(contract: dict, snap: dict, max_spread_override: float | None = None) -> bool:
    """Per-contract liquidity gate (2026-08-26 research pass) — equity-level
    liquidity (ScreeningFilters.min_avg_volume) is a poor proxy for options
    liquidity specifically. Checked on every leg individually, never
    averaged across a spread.

    `open_interest` enforced only when the API actually returns a value —
    verified directly against the live account that Alpaca's free/paper
    tier returns `open_interest: null` for real, currently-liquid contracts
    (confirmed on near-the-money SPY weekly puts with tight, tradeable
    spreads) — evidently a data-availability gap on this feed, not a
    genuine liquidity signal. Treating null as "reject" would silently
    reject nearly everything, including the most liquid instrument that
    exists; treating it as "unknown, don't penalize" and leaning on the
    bid-ask spread check — which the same live test showed DOES return
    real, usable values — is the honest choice here. The threshold still
    applies whenever a real number comes back.
    """
    oi_raw = contract.get("open_interest")
    if oi_raw is not None and int(oi_raw) < config.risk.min_open_interest:
        return False
    mid = _mid_from_snapshot(snap)
    if mid is None or mid <= 0:
        return False
    quote = snap["latestQuote"]
    bid, ask = float(quote["bp"]), float(quote["ap"])
    spread_pct = (ask - bid) / mid
    threshold = max_spread_override if max_spread_override is not None else config.risk.max_bid_ask_spread_pct
    return spread_pct <= threshold


async def _fetch_contracts(mcp: AlpacaMCP, ticker: str, option_type: str, min_exp: date, max_exp: date) -> list[dict]:
    cache_key = (ticker, option_type, min_exp.isoformat(), max_exp.isoformat())
    if cache_key in _contract_cache:
        return _contract_cache[cache_key]
    result = await mcp.call(
        "get_option_contracts",
        {
            "underlying_symbols": ticker,
            "type": option_type,
            "status": "active",
            "expiration_date_gte": min_exp.isoformat(),
            "expiration_date_lte": max_exp.isoformat(),
            "limit": 100,
        },
    )
    contracts = (result or {}).get("data", {}).get("option_contracts", [])
    _contract_cache[cache_key] = contracts
    return contracts


async def _fetch_snapshots(mcp: AlpacaMCP, symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    result = await mcp.call(
        "get_option_snapshot",
        {"symbols": ",".join(symbols), "feed": "indicative"},
    )
    return (result or {}).get("data", {}).get("snapshots", {})


async def build_spread(
    mcp: AlpacaMCP,
    ticker: str,
    signal_direction: str,
    spot_price: float,
    realized_vol: float,
) -> SpreadPlan | None:
    """signal_direction is the vendored Signal's own 'long'/'short' field.
    `spot_price` is the underlying's current mid quote, `realized_vol` the
    annualized realized-vol estimate (see black_scholes.realized_vol_from_bars)
    used as the IV proxy for delta. Returns None (never a half-built spread)
    if the chain doesn't have a clean, liquid expiration/strike pair in the
    configured windows — a skipped cycle is always safer than a guessed one.
    """
    limits = config.risk
    today = datetime.now().date()
    min_exp = today + timedelta(days=limits.min_dte)
    max_exp = today + timedelta(days=limits.max_dte)

    is_bull_put = signal_direction == "long"
    option_type = "put" if is_bull_put else "call"

    contracts = await _fetch_contracts(mcp, ticker, option_type, min_exp, max_exp)
    if not contracts:
        logger.info("No %s contracts for %s in [%s, %s]", option_type, ticker, min_exp, max_exp)
        return None

    # Prefer the nearest expiration inside the window (more theta decay
    # realized within the judged period).
    contracts.sort(key=lambda c: c.get("expiration_date", ""))
    chosen_expiration = contracts[0]["expiration_date"]
    exp_contracts = [c for c in contracts if c.get("expiration_date") == chosen_expiration]
    dte_days = (datetime.strptime(chosen_expiration, "%Y-%m-%d").date() - today).days

    symbols = [c["symbol"] for c in exp_contracts]
    snap_by_symbol = await _fetch_snapshots(mcp, symbols)

    def delta_of(contract: dict) -> float:
        strike = float(contract["strike_price"])
        return abs(bs_delta(
            spot=spot_price, strike=strike, dte_days=dte_days,
            volatility=realized_vol, option_type=option_type,
        ))

    liquid_candidates = [
        (c, delta_of(c)) for c in exp_contracts
        if _passes_liquidity(c, snap_by_symbol.get(c["symbol"], {}))
    ]
    if not liquid_candidates:
        logger.info(
            "%s %s chain has %d strikes but none pass the liquidity gate "
            "(min OI %d, max spread %.0f%%), skipping",
            ticker, chosen_expiration, len(exp_contracts),
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
        short_strike - limits.spread_width_dollars
        if is_bull_put
        else short_strike + limits.spread_width_dollars
    )
    same_exp_by_strike = {float(c["strike_price"]): c for c in exp_contracts}
    if target_long_strike not in same_exp_by_strike:
        # Snap to the closest available strike rather than failing outright —
        # standard option chains aren't guaranteed to have every $5 increment.
        closest_strike = min(same_exp_by_strike, key=lambda k: abs(k - target_long_strike))
        target_long_strike = closest_strike
    long_contract = same_exp_by_strike[target_long_strike]

    long_snap = snap_by_symbol.get(long_contract["symbol"], {})
    if not _passes_liquidity(long_contract, long_snap, max_spread_override=LONG_LEG_MAX_SPREAD_PCT):
        logger.info("%s long leg (%s) fails the liquidity gate, skipping", ticker, long_contract["symbol"])
        return None

    short_snap = snap_by_symbol.get(short_contract["symbol"], {})
    short_mid = _mid_from_snapshot(short_snap)
    long_mid = _mid_from_snapshot(long_snap)
    if short_mid is None or long_mid is None:
        logger.warning("Missing quotes for %s spread legs, skipping", ticker)
        return None

    credit_estimate = round((short_mid - long_mid) * 100, 2)  # per 1 contract, $ not cents
    width_dollars = abs(short_strike - target_long_strike) * 100
    max_loss = round(width_dollars - credit_estimate, 2)

    if credit_estimate <= 0:
        logger.info("%s spread has non-positive credit (%.2f), skipping", ticker, credit_estimate)
        return None

    # Real bug caught 2026-08-27: credit_estimate > 0 alone doesn't rule out
    # a nonsensical spread — if credit exceeds the strike width (stale or
    # crossed quotes, or the long strike snapping to something closer than
    # the intended width), max_loss goes negative, meaning "risk-free
    # profit" on paper. A spread whose own defined risk is negative or zero
    # is not a real credit spread and must never reach execution.
    if max_loss <= 0:
        logger.warning(
            "%s spread has non-positive max_loss (%.2f = width %.2f - credit %.2f) "
            "-- almost certainly a stale/bad quote, skipping",
            ticker, max_loss, width_dollars, credit_estimate,
        )
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
