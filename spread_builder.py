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
from datetime import date, datetime, timedelta, timezone

from black_scholes import bs_delta
from config import config
from mcp_client import AlpacaMCP

logger = logging.getLogger(__name__)

_contract_cache: dict[tuple, list[dict]] = {}


@dataclass
class SpreadPlan:
    underlying: str
    direction: str  # 'bull_put' | 'bear_call' (credit) | 'bull_call' | 'bear_put' (debit)
    expiration: date
    short_strike: float
    long_strike: float
    short_symbol: str
    long_symbol: str
    credit_estimate: float
    max_loss: float
    # 'credit' (default, unchanged behavior) | 'debit' (2026-09-02 overlay,
    # see build_debit_spread). For a debit spread, `short_strike`/
    # `short_symbol` mean the leg this project BUYS (the near-the-money,
    # delta-targeted leg) and `long_strike`/`long_symbol` mean the leg it
    # SELLS (further OTM, reduces cost) -- the OPPOSITE of what those names
    # mean for a credit spread. `credit_estimate` is NEGATIVE for a debit
    # spread (money paid, not received) -- see executor_mcp.py/risk_gate.py
    # for how each side of this project reads that sign. `max_loss` stays
    # uniformly positive (the debit paid itself, capped) regardless of
    # structure, so risk_gate/position-sizing need no changes at all.
    structure: str = "credit"


@dataclass
class IronCondorPlan:
    """A put credit spread and a call credit spread at the same expiration,
    same width, sold simultaneously — profits if the underlying stays
    between the two short strikes through expiration, no directional view
    required. `direction` is hardcoded to the literal "iron_condor" (not a
    real long/short signal direction) so it flows through the existing
    `plan.direction`-keyed code paths (DB `direction` column, log messages,
    dashboard labels) with minimal special-casing elsewhere.
    """
    underlying: str
    direction: str  # always "iron_condor"
    expiration: date
    short_put_strike: float
    long_put_strike: float
    short_call_strike: float
    long_call_strike: float
    short_put_symbol: str
    long_put_symbol: str
    short_call_symbol: str
    long_call_symbol: str
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

# Real bug caught 2026-08-28 (see _select_vertical_leg): the closest-liquid-
# delta strike can, at a thin/stale-quote instant, be nowhere near the
# actual target delta. 0.15 is generous enough not to reject a normal pick
# on a coarse strike grid, while still catching a wrong-regime fallback
# (e.g. the ~0.79-delta case that motivated this).
MAX_DELTA_DEVIATION = 0.15


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


def _select_vertical_leg(
    ticker: str,
    option_type: str,
    exp_contracts: list[dict],
    snap_by_symbol: dict[str, dict],
    spot_price: float,
    dte_days: int,
    realized_vol: float,
    is_lower_long: bool,
    width_override: float | None = None,
    target_delta_override: float | None = None,
) -> tuple[dict, dict, float, float, float] | None:
    """Shared strike-selection + liquidity logic for one side of a vertical
    (either a standalone bull put/bear call, or one wing of an iron condor).
    `is_lower_long` is True when the long leg sits BELOW the short strike
    (a put side: further OTM = lower), False when it sits above (a call
    side: further OTM = higher).

    Returns (short_contract, long_contract, short_strike, long_strike,
    credit_estimate) or None if no liquid/quotable pair exists for this
    side — never a half-built leg.
    """
    limits = config.risk

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
            ticker, option_type, len(exp_contracts),
            limits.min_open_interest, limits.max_bid_ask_spread_pct * 100,
        )
        return None

    effective_target_delta = target_delta_override if target_delta_override is not None else limits.short_leg_target_delta
    liquid_candidates.sort(key=lambda cd: abs(cd[1] - effective_target_delta))
    short_contract, short_delta = liquid_candidates[0]
    short_strike = float(short_contract["strike_price"])

    # Real bug caught testing the iron condor build against live data
    # 2026-08-28: at a thin/stale-quote instant, every genuinely OTM strike
    # can have bid=0 and fail the liquidity gate above, leaving only
    # deep-ITM strikes as "liquid" -- one such moment picked a short strike
    # with delta ~0.79 against a 0.17 target. "Closest available liquid
    # delta" isn't the same claim as "close enough to the delta this
    # project's risk profile assumes" -- a spread whose short leg is nowhere
    # near the intended delta must not reach execution just because it was
    # technically the least-bad liquid option this instant.
    if abs(short_delta - effective_target_delta) > MAX_DELTA_DEVIATION:
        logger.warning(
            "%s %s: closest liquid strike's delta (%.2f) is too far from target (%.2f) "
            "-- likely a thin/stale-quote moment leaving only illiquid or wrong-regime "
            "strikes as 'liquid', skipping",
            ticker, option_type, short_delta, effective_target_delta,
        )
        return None

    # Long leg: `spread_width_dollars` further out-of-the-money than the
    # short strike — lower strike for a put spread (further OTM = lower),
    # higher strike for a call spread (further OTM = higher).
    effective_width = width_override if width_override is not None else limits.spread_width_dollars
    target_long_strike = (
        short_strike - effective_width
        if is_lower_long
        else short_strike + effective_width
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
        logger.info("%s long %s leg (%s) fails the liquidity gate, skipping", ticker, option_type, long_contract["symbol"])
        return None

    short_snap = snap_by_symbol.get(short_contract["symbol"], {})
    short_mid = _mid_from_snapshot(short_snap)
    long_mid = _mid_from_snapshot(long_snap)
    if short_mid is None or long_mid is None:
        logger.warning("Missing quotes for %s %s leg, skipping", ticker, option_type)
        return None

    credit_estimate = round((short_mid - long_mid) * 100, 2)  # per 1 contract, $ not cents
    return short_contract, long_contract, short_strike, target_long_strike, credit_estimate


async def build_spread(
    mcp: AlpacaMCP,
    ticker: str,
    signal_direction: str,
    spot_price: float,
    realized_vol: float,
    width_override: float | None = None,
) -> SpreadPlan | None:
    """signal_direction is the vendored Signal's own 'long'/'short' field.
    `spot_price` is the underlying's current mid quote, `realized_vol` the
    annualized realized-vol estimate (see black_scholes.realized_vol_from_bars)
    used as the IV proxy for delta. Returns None (never a half-built spread)
    if the chain doesn't have a clean, liquid expiration/strike pair in the
    configured windows — a skipped cycle is always safer than a guessed one.
    """
    limits = config.risk
    today = datetime.now(timezone.utc).date()
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

    leg = _select_vertical_leg(
        ticker, option_type, exp_contracts, snap_by_symbol,
        spot_price, dte_days, realized_vol, is_lower_long=is_bull_put,
        width_override=width_override,
    )
    if leg is None:
        return None
    short_contract, long_contract, short_strike, target_long_strike, credit_estimate = leg

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


async def build_debit_spread(
    mcp: AlpacaMCP,
    ticker: str,
    signal_direction: str,
    spot_price: float,
    realized_vol: float,
) -> SpreadPlan | None:
    """Directional DEBIT vertical (2026-09-02 overlay) -- bull call spread on
    a 'long' signal, bear put spread on a 'short' signal. BUYS the near-the-
    money leg (delta near config.risk.debit_leg_target_delta -- the actual
    directional bet) and SELLS a further-OTM leg `spread_width_dollars`
    away to reduce cost, the mirror image of build_spread's credit vertical.

    Reuses `_select_vertical_leg` unchanged: its "delta-targeted leg near
    the money" + "width-offset leg further OTM" geometry is IDENTICAL for a
    debit spread, only the buy/sell roles invert -- what that function calls
    the "short" (delta-targeted) contract is the leg THIS function buys, and
    what it calls "long" (width-offset) is the leg this function sells. Its
    returned `(short_mid - long_mid)` is therefore (buy_mid - sell_mid) here
    -- the debit paid per contract, not a credit -- which this function
    negates before storing, matching SpreadPlan.structure='debit' convention
    (see its own docstring).

    Same "a skipped cycle is safer than a guessed one" discipline as
    build_spread: returns None (never a half-built or economically
    nonsensical spread) if the chain lacks a liquid pair, if the resulting
    debit isn't positive, if the delta of the leg being bought falls outside
    [debit_delta_sanity_min, debit_delta_sanity_max] (mirrors the exact band
    already proven on Paco's mcp_risk_proxy backstop), or if the debit paid
    would be >= the strike width (arbitrage-nonsensical, almost certainly a
    stale/crossed quote).
    """
    limits = config.risk
    today = datetime.now(timezone.utc).date()
    min_exp = today + timedelta(days=limits.min_dte)
    max_exp = today + timedelta(days=limits.max_dte)

    is_bull_call = signal_direction == "long"
    option_type = "call" if is_bull_call else "put"
    # Further-OTM = higher strike for a call, lower strike for a put --
    # same geometry build_spread already relies on, just keyed off
    # option_type directly here since both directions map to the same rule.
    is_lower_long = option_type == "put"

    contracts = await _fetch_contracts(mcp, ticker, option_type, min_exp, max_exp)
    if not contracts:
        logger.info("No %s contracts for %s in [%s, %s] (debit spread)", option_type, ticker, min_exp, max_exp)
        return None

    contracts.sort(key=lambda c: c.get("expiration_date", ""))
    chosen_expiration = contracts[0]["expiration_date"]
    exp_contracts = [c for c in contracts if c.get("expiration_date") == chosen_expiration]
    dte_days = (datetime.strptime(chosen_expiration, "%Y-%m-%d").date() - today).days

    symbols = [c["symbol"] for c in exp_contracts]
    snap_by_symbol = await _fetch_snapshots(mcp, symbols)

    leg = _select_vertical_leg(
        ticker, option_type, exp_contracts, snap_by_symbol,
        spot_price, dte_days, realized_vol, is_lower_long=is_lower_long,
        target_delta_override=limits.debit_leg_target_delta,
    )
    if leg is None:
        return None
    buy_contract, sell_contract, buy_strike, sell_strike, raw_diff = leg

    # _select_vertical_leg's MAX_DELTA_DEVIATION check already confirms the
    # buy leg's delta is within 0.15 of debit_leg_target_delta -- this is an
    # ADDITIONAL absolute sanity band (mirrors Paco's proven
    # _DEBIT_DELTA_SANITY_MIN/MAX), independent of whatever the target
    # happens to be configured to.
    buy_delta = abs(bs_delta(
        spot=spot_price, strike=buy_strike, dte_days=dte_days,
        volatility=realized_vol, option_type=option_type,
    ))
    if not (limits.debit_delta_sanity_min <= buy_delta <= limits.debit_delta_sanity_max):
        logger.info(
            "%s debit %s buy leg delta (%.2f) outside the sane [%.2f, %.2f] "
            "range, skipping",
            ticker, option_type, buy_delta, limits.debit_delta_sanity_min, limits.debit_delta_sanity_max,
        )
        return None

    # raw_diff = buy_mid - sell_mid (the shared helper's "credit" formula,
    # here interpreted as debit paid since we buy the near-the-money leg).
    debit_paid = raw_diff
    if debit_paid <= 0:
        logger.info("%s debit spread has non-positive debit (%.2f), skipping", ticker, debit_paid)
        return None

    width_dollars = abs(buy_strike - sell_strike) * 100
    if debit_paid >= width_dollars:
        logger.warning(
            "%s debit spread paid $%.2f for a $%.2f-wide structure -- "
            "arbitrage-nonsensical, almost certainly a stale/crossed quote, skipping",
            ticker, debit_paid, width_dollars,
        )
        return None

    return SpreadPlan(
        underlying=ticker,
        direction="bull_call" if is_bull_call else "bear_put",
        expiration=datetime.strptime(chosen_expiration, "%Y-%m-%d").date(),
        short_strike=buy_strike,
        long_strike=sell_strike,
        short_symbol=buy_contract["symbol"],
        long_symbol=sell_contract["symbol"],
        credit_estimate=round(-debit_paid, 2),
        max_loss=round(debit_paid, 2),
        structure="debit",
    )


async def build_iron_condor(
    mcp: AlpacaMCP,
    ticker: str,
    spot_price: float,
    realized_vol: float,
    target_delta_override: float | None = None,
) -> IronCondorPlan | None:
    """Builds a put credit spread AND a call credit spread at the SAME
    expiration, sold together as one structure — called by find_candidates()
    when `signals.regime.RegimeDetector` classifies the underlying as
    RANGING (ADX below threshold, vol_ratio not elevated either): no real
    trend strength to back a directional bet, so a neutral structure is
    offered instead of forcing one. Reuses every liquidity/delta/strike-
    width rule `build_spread` already applies, independently on each side,
    via `_select_vertical_leg`.

    Returns None (never a half-built structure) if either side fails its
    liquidity/credit gate, if the two sides can't agree on a common
    expiration, or if the resulting max_loss isn't positive — exactly the
    same "a skipped cycle is safer than a guessed one" discipline as
    `build_spread`.
    """
    limits = config.risk
    today = datetime.now(timezone.utc).date()
    min_exp = today + timedelta(days=limits.min_dte)
    max_exp = today + timedelta(days=limits.max_dte)

    put_contracts = await _fetch_contracts(mcp, ticker, "put", min_exp, max_exp)
    call_contracts = await _fetch_contracts(mcp, ticker, "call", min_exp, max_exp)
    if not put_contracts or not call_contracts:
        logger.info(
            "%s missing %s contracts in [%s, %s], can't build an iron condor",
            ticker, "puts" if not put_contracts else "calls", min_exp, max_exp,
        )
        return None

    # Both sides must share one expiration -- pick the nearest one listed on
    # BOTH the put and call chains (in practice these almost always match,
    # but never assumed).
    put_expirations = {c["expiration_date"] for c in put_contracts}
    call_expirations = {c["expiration_date"] for c in call_contracts}
    common_expirations = sorted(put_expirations & call_expirations)
    if not common_expirations:
        logger.info(
            "%s has no expiration listed on both put and call chains in [%s, %s], skipping iron condor",
            ticker, min_exp, max_exp,
        )
        return None
    chosen_expiration = common_expirations[0]
    dte_days = (datetime.strptime(chosen_expiration, "%Y-%m-%d").date() - today).days

    put_exp_contracts = [c for c in put_contracts if c.get("expiration_date") == chosen_expiration]
    call_exp_contracts = [c for c in call_contracts if c.get("expiration_date") == chosen_expiration]

    # Real bug caught testing against the live account: `get_option_snapshot`
    # enforces a hard 100-symbol limit ("HTTP 400: symbol limit is 100").
    # build_spread never hits this because it only ever asks for one side's
    # symbols (already bounded under 100 by _fetch_contracts' own `limit:
    # 100` on get_option_contracts); an iron condor asks for BOTH sides, so
    # combining put+call symbols into one snapshot call can push past 100 on
    # a wide chain. Two separate per-side calls keep each request under the
    # same bound build_spread already relies on, with no new API dependency.
    put_snap_by_symbol = await _fetch_snapshots(mcp, [c["symbol"] for c in put_exp_contracts])
    call_snap_by_symbol = await _fetch_snapshots(mcp, [c["symbol"] for c in call_exp_contracts])
    snap_by_symbol = {**put_snap_by_symbol, **call_snap_by_symbol}

    put_leg = _select_vertical_leg(
        ticker, "put", put_exp_contracts, snap_by_symbol,
        spot_price, dte_days, realized_vol, is_lower_long=True,
        target_delta_override=target_delta_override,
    )
    if put_leg is None:
        return None
    call_leg = _select_vertical_leg(
        ticker, "call", call_exp_contracts, snap_by_symbol,
        spot_price, dte_days, realized_vol, is_lower_long=False,
        target_delta_override=target_delta_override,
    )
    if call_leg is None:
        return None

    short_put, long_put, short_put_strike, long_put_strike, put_credit = put_leg
    short_call, long_call, short_call_strike, long_call_strike, call_credit = call_leg

    if put_credit <= 0:
        logger.info("%s iron condor put side has non-positive credit (%.2f), skipping", ticker, put_credit)
        return None
    if call_credit <= 0:
        logger.info("%s iron condor call side has non-positive credit (%.2f), skipping", ticker, call_credit)
        return None

    # A real, sane iron condor needs its short strikes straddling spot (short
    # put below, short call above) -- if delta selection on a wild/illiquid
    # chain ever inverted that, the structure isn't a real condor anymore
    # and must not reach execution.
    if short_put_strike >= short_call_strike:
        logger.warning(
            "%s iron condor short strikes are inverted or overlapping (short put %.2f >= short call %.2f), skipping",
            ticker, short_put_strike, short_call_strike,
        )
        return None

    put_width = abs(short_put_strike - long_put_strike)
    call_width = abs(short_call_strike - long_call_strike)
    # max_loss = width*100 - total credit is only correct because both sides
    # share the SAME width (both built from config.risk.spread_width_dollars
    # against the same chain) -- at expiration the underlying can't
    # simultaneously be below the put spread and above the call spread, so
    # exactly one side can ever be the loser, and its max loss is offset by
    # the credit already banked on the side that expired worthless. Enforced
    # explicitly here (not just assumed) since a strike snapped to the
    # nearest available increment (see _select_vertical_leg) could in
    # principle produce unequal widths on a sparse chain.
    if round(put_width, 2) != round(call_width, 2):
        logger.warning(
            "%s iron condor put width (%.2f) != call width (%.2f) -- refusing, "
            "the max_loss formula assumes equal widths",
            ticker, put_width, call_width,
        )
        return None

    credit_estimate = round(put_credit + call_credit, 2)
    max_loss = round(put_width * 100 - credit_estimate, 2)
    if max_loss <= 0:
        logger.warning(
            "%s iron condor has non-positive max_loss (%.2f = width %.2f - total credit %.2f) "
            "-- almost certainly a stale/bad quote, skipping",
            ticker, max_loss, put_width * 100, credit_estimate,
        )
        return None

    # Iron-condor-specific floor (tastytrade rule of thumb, 2026-08-28):
    # reject if the total credit collected is too small a fraction of the
    # width being risked -- e.g. below 1/3 of a $5 wing is under $1.67.
    # max_loss > 0 alone doesn't catch a technically-valid but not-worth-
    # the-risk structure (barely any premium for the full width at stake).
    # Verticals don't have an equivalent check yet -- kept iron-condor-only
    # rather than applied retroactively without the same research behind it
    # there.
    width_dollars = put_width * 100
    min_credit = width_dollars * config.risk.min_credit_to_width_pct
    if credit_estimate < min_credit:
        logger.info(
            "%s iron condor credit $%.2f is below the %.0f%% min-credit-to-width "
            "floor ($%.2f of $%.2f width) -- not worth the risk, skipping",
            ticker, credit_estimate, config.risk.min_credit_to_width_pct * 100,
            min_credit, width_dollars,
        )
        return None

    return IronCondorPlan(
        underlying=ticker,
        direction="iron_condor",
        expiration=datetime.strptime(chosen_expiration, "%Y-%m-%d").date(),
        short_put_strike=short_put_strike,
        long_put_strike=long_put_strike,
        short_call_strike=short_call_strike,
        long_call_strike=long_call_strike,
        short_put_symbol=short_put["symbol"],
        long_put_symbol=long_put["symbol"],
        short_call_symbol=short_call["symbol"],
        long_call_symbol=long_call["symbol"],
        credit_estimate=credit_estimate,
        max_loss=max_loss,
    )
