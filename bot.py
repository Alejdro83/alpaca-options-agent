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
from signals.regime import Regime, RegimeDetector

import black_scholes
import db
import executor_mcp
import llm_reasoner
import risk_gate
from mcp_client import AlpacaMCP
from spread_builder import IronCondorPlan, SpreadPlan, _mid_from_snapshot, build_iron_condor, build_spread

from pathlib import Path
import json as _json

# Evolved parameter overrides — loaded from state/evolved_params.json at the
# start of each run_cycle(). Empty dict means "use config defaults."
_evolved_overrides: dict = {}
# Which generation is active right now — tags every cycle/spread this run
# opens so a generation's REAL P&L can be measured later (db.py's
# get_realized_pnl_by_generation), not just overnight_evolution.py's own
# one-day simulated replay. 0 == no evolution promoted yet, config defaults.
_current_generation: int = 0


def _load_evolved_params() -> None:
    global _evolved_overrides, _current_generation
    path = Path(__file__).resolve().parent / "state" / "evolved_params.json"
    if not path.exists():
        _evolved_overrides = {}
        _current_generation = 0
        return
    try:
        data = _json.loads(path.read_text())
        _evolved_overrides = {k: v for k, v in data.items()
                              if k not in ("evolved_at", "generation", "promotion_reason")}
        _current_generation = int(data.get("generation", 0))
        logger.info("Loaded evolved params (gen %s): %s",
                     _current_generation, _evolved_overrides)
    except Exception:
        logger.exception("Failed to load evolved_params.json, using config defaults")
        _evolved_overrides = {}
        _current_generation = 0


def _risk(attr: str):
    """Return evolved value if present, else config.risk default."""
    if attr in _evolved_overrides:
        return _evolved_overrides[attr]
    return getattr(config.risk, attr)


def _vol(attr: str):
    """Return evolved value if present, else config.volatility default."""
    if attr in _evolved_overrides:
        return _evolved_overrides[attr]
    return getattr(config.volatility, attr)


# `basicConfig`'s default StreamHandler writes to stderr, not stdout — but
# run_options_cron.sh redirects stderr into stdout (`2>&1`) before deciding
# whether there's anything worth delivering, so every INFO-level screening
# line (dozens per cycle: "23/503 tickers passed filters", each rejection
# reason, every MCP call) rode along regardless of the docstring's stated
# "silent unless something happened" intent — confirmed directly: a single
# no-op cycle produced 61 lines of output, all delivered as if noteworthy.
# Real, user-visible symptom (2026-08-27): Alex had to ask Hermes to stop
# forwarding these to Telegram entirely ("me llena de mensajes raros") and
# reroute to Discord instead — which only relocates the noise, it doesn't
# fix it. Routing the log handler to a file instead restores the original
# design: stdout carries only the deliberate print(note) calls below.
LOG_DIR = Path(__file__).resolve().parent / "state"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filename=str(LOG_DIR / "bot.log"),
)
logger = logging.getLogger(__name__)

# Same lookback trading_bot/bot.py uses for its own trend filter — EMA200 +
# a buffer, in calendar days rather than trading days to survive
# weekends/holidays comfortably.
TREND_FILTER_LOOKBACK_DAYS = 400


def _realized_vol_percentile(bars_df: pd.DataFrame) -> float | None:
    """Where this ticker's current realized vol (20-day ATR%) ranks against
    its own trailing-year distribution — None means "not enough history to
    rank meaningfully," treated as fail-open by callers, same as before.
    Split out from the old _passes_volatility_filter so the adaptive
    threshold below can see every candidate's percentile before deciding
    what bar to hold the whole cycle to (see _apply_trend_and_volatility_filters).
    """
    atr = compute_atr(bars_df["high"], bars_df["low"], bars_df["close"], period=_vol("lookback_window"))
    atr_pct = (atr / bars_df["close"]).dropna()
    if len(atr_pct) < _vol("lookback_window") * 2:
        return None
    return float(atr_pct.rank(pct=True).iloc[-1])


def _fetch_daily_bars(client: AlpacaClient, ticker: str) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=TREND_FILTER_LOOKBACK_DAYS)
    bars = client.get_bars(
        ticker, TimeFrame.Day,
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
        limit=300,
    )
    return pd.DataFrame(bars)


def _vixy_regime_percentile(client: AlpacaClient) -> float | None:
    """Percentile-based proxy for market-wide vol regime using VIXY (a
    VIX-futures ETF) — NOT the real VIX index (^VIX is unavailable via
    Alpaca's data API, confirmed live: "invalid symbol: ^VIX").

    VIXY's absolute price has no stable relationship to the real VIX level
    (it has decayed from hundreds to ~$17-18 due to structural contango
    roll costs), so raw price thresholds like "VIX < 15" would silently
    misclassify essentially always. Instead, this ranks VIXY's current
    close against its own trailing-year daily range using the same
    rolling-window + `.rank(pct=True)` technique as
    `_realized_vol_percentile` — a 0-1 percentile where 0 means "VIXY is
    near its 1-year low" and 1 means "near its 1-year high."

    Returns None if insufficient history (fail-open: treat as mid-regime,
    no adjustment).
    """
    try:
        bars_df = _fetch_daily_bars(client, "VIXY")
    except Exception:
        logger.exception("Failed to fetch VIXY bars, skipping VIXY regime overlay")
        return None
    if bars_df.empty or "close" not in bars_df.columns or len(bars_df) < 60:
        logger.info("VIXY has insufficient data (%d rows), skipping VIXY regime overlay", len(bars_df))
        return None
    close = bars_df["close"].dropna()
    if len(close) < 60:
        return None
    return float(close.rank(pct=True).iloc[-1])


def _apply_trend_and_volatility_filters(client: AlpacaClient, signals: list) -> list[tuple]:
    """Returns (signal, realized_vol, regime) triples for survivors —
    realized_vol is the annualized estimate `spread_builder.build_spread`/
    `build_iron_condor` feed into `black_scholes.bs_delta` as the IV proxy,
    computed here (not re-fetched later) since this is already pulling the
    daily bars it needs. `regime` is a `signals.regime.Regime` enum value.

    Real bug fixed 2026-08-28 (first pass): this used to thread
    `trend_direction` ('bullish'/'bearish'/'neutral') through, routing
    find_candidates() to build_iron_condor on trend_direction=='neutral'.
    trend_direction comes from EMA50-vs-EMA200 (a DIRECTION read); 'neutral'
    only fires on exact EMA equality, which real data essentially never
    produces (confirmed live: 0 of 20 real signals came back neutral in one
    check). Fixed to route on raw ADX<threshold instead (a genuine "no real
    trend strength" read).

    Second pass, same day: a teammate's strategy research proposed the
    fuller ADX+vol_ratio 2x2 regime matrix (TRENDING / RANGING /
    VOLATILE_TRENDING / VOLATILE_RANGING) as the real basis for choosing
    between a directional vertical and an iron condor. Turned out
    `signals/regime.py` (vendored from trading_bot/ at project start,
    never imported by anything here — see the commit that recovered
    signals/ and screening/ into git the same day) already implements
    exactly this, with the same default thresholds (ADX 25, vol_ratio 1.5)
    the research cited. Wired in here instead of reimplementing it.
    TrendFilter is kept alongside it for its own, different job: the
    direction-vs-trend `allowed` gate (don't trade long against a
    confirmed bearish EMA trend) has no equivalent in RegimeDetector,
    which only classifies the underlying's own regime, not a signal's
    proposed direction against it.

    Two passes: trend filter first (unchanged), then the volatility filter
    with an adaptive threshold — see config.VolatilityFilter's docstring.
    The adaptive rule needs every trend-survivor's percentile computed up
    front to decide whether *this cycle* is unusually low-vol across the
    board (relax) versus this one ticker just being quiet (still reject).
    """
    trend_filter = TrendFilter()
    regime_detector = RegimeDetector()
    trend_survivors: list[tuple] = []  # (sig, bars_df, regime)
    for sig in signals:
        try:
            bars_df = _fetch_daily_bars(client, sig.ticker)
        except Exception:
            logger.exception("Failed to fetch bars for %s, skipping", sig.ticker)
            continue

        # A real failure mode hit live: get_bars() can return an empty list
        # for a symbol (observed for BAC) -- pd.DataFrame([]) has no columns
        # at all, so bars_df["close"] KeyErrors. The original code only
        # guarded the trend-filter step against this and then immediately
        # crashed the *entire cycle* (not just this ticker) on the very next
        # line, in realized_vol_from_bars -- a single bad symbol took down
        # every other candidate with it. Skip cleanly instead.
        if bars_df.empty or "close" not in bars_df.columns:
            logger.info("%s has no usable daily bars this cycle, skipping", sig.ticker)
            continue

        try:
            trend_allowed = trend_filter.check(bars_df, sig.direction).allowed
        except Exception:
            logger.exception("Trend filter failed for %s, allowing", sig.ticker)
            trend_allowed = True
        if not trend_allowed:
            continue

        try:
            regime = regime_detector.detect(bars_df).regime
        except Exception:
            logger.exception("Regime detection failed for %s, treating as RANGING", sig.ticker)
            # Unknown regime: route to an iron condor (no directional view
            # required) rather than silently trusting the swing model's own
            # guessed direction with zero regime confirmation behind it --
            # same conservative "don't guess a direction you can't confirm"
            # choice made elsewhere in this project.
            regime = Regime.RANGING

        trend_survivors.append((sig, bars_df, regime))

    min_percentile = _vol("min_percentile")

    if _vol("enabled") and trend_survivors:
        percentiles: list[float] = []
        for sig, bars_df, _regime in trend_survivors:
            try:
                pct = _realized_vol_percentile(bars_df)
            except Exception:
                logger.exception("Volatility percentile failed for %s, treating as unrankable", sig.ticker)
                pct = None
            if pct is not None:
                percentiles.append(pct)

        if percentiles:
            rejection_rate = sum(1 for p in percentiles if p < min_percentile) / len(percentiles)
            if rejection_rate > _vol("max_rejection_rate_before_relax"):
                logger.info(
                    "Volatility filter would reject %.0f%% of %d rankable candidates at "
                    "percentile %.2f -- relaxing to %.2f for this cycle only (adaptive rule, "
                    "not a permanent change; see config.VolatilityFilter)",
                    rejection_rate * 100, len(percentiles), min_percentile,
                    _vol("relaxed_min_percentile"),
                )
                min_percentile = _vol("relaxed_min_percentile")

    kept = []
    for sig, bars_df, regime in trend_survivors:
        if _vol("enabled"):
            try:
                pct = _realized_vol_percentile(bars_df)
            except Exception:
                logger.exception("Volatility filter failed for %s, allowing", sig.ticker)
                pct = None
            if pct is not None and pct < min_percentile:
                logger.info(
                    "%s rejected: realized vol percentile %.2f below %.2f",
                    sig.ticker, pct, min_percentile,
                )
                continue

        realized_vol = black_scholes.realized_vol_from_bars(bars_df)
        kept.append((sig, realized_vol, regime))
    return kept


def _optimal_contracts(equity: float, max_loss_per_contract: float, max_risk_pct: float = 0.02) -> int:
    """Size contracts so total max loss stays within risk budget."""
    if max_loss_per_contract <= 0:
        return 1
    dollar_budget = equity * max_risk_pct
    contracts = int(dollar_budget // max_loss_per_contract)
    return max(contracts, 1)


async def manage_open_spreads(mcp: AlpacaMCP) -> list[str]:
    notes = []
    for spread in db.get_open_spreads():
        expiration = datetime.strptime(str(spread["expiration"]), "%Y-%m-%d").date()
        force_close, force_reason = risk_gate.should_force_close(expiration=expiration)

        # `strategy` defaults to 'vertical' at the DB column level, but an
        # old row read back with a driver that doesn't apply column
        # defaults on select (or any future strategy value this code
        # doesn't yet know) must still fall onto the pre-existing 2-symbol
        # path rather than erroring or silently doing nothing.
        is_iron_condor = spread.get("strategy") == "iron_condor"

        try:
            if is_iron_condor:
                mark = await executor_mcp.get_iron_condor_mark(
                    mcp,
                    short_put_symbol=spread["short_symbol"],
                    long_put_symbol=spread["long_symbol"],
                    short_call_symbol=spread["call_short_symbol"],
                    long_call_symbol=spread["call_long_symbol"],
                )
            else:
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
            if is_iron_condor:
                await executor_mcp.close_iron_condor(
                    mcp,
                    short_put_symbol=spread["short_symbol"],
                    long_put_symbol=spread["long_symbol"],
                    short_call_symbol=spread["call_short_symbol"],
                    long_call_symbol=spread["call_long_symbol"],
                    contracts=spread["contracts"],
                )
            else:
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
                # credit_received/mark are both per-contract (get_spread_mark
                # never multiplies by position size) — multiply by the real
                # contracts held or P&L is understated whenever contracts>1,
                # the same class of bug fixed in the entry path above.
                contracts_held = int(spread.get("contracts") or 1)
                realized_pnl = (float(spread["credit_received"]) - mark) * contracts_held
                status = "closed_expiry" if force_close else ("closed_profit" if realized_pnl > 0 else "closed_stop")
                notes.append(f"Closed {spread['underlying']} {spread['direction']}: {reason} (P&L ${realized_pnl:+.2f})")
            db.record_spread_close(spread["id"], status, realized_pnl)
        except Exception as exc:
            logger.exception("Failed to close spread %s", spread["id"])
            notes.append(f"ERROR closing {spread['underlying']}: {exc}")
    return notes


async def find_candidates(
    mcp: AlpacaMCP, client: AlpacaClient, account: dict, open_count: int
) -> tuple[list[dict], list[dict]]:
    universe = get_universe()
    filtered = filter_universe(universe, client)
    tickers = [c.symbol for c in filtered]
    signals = generate_swing_signals(tickers, client)
    signals_with_vol = _apply_trend_and_volatility_filters(client, signals)

    today = datetime.now(timezone.utc).date()

    existing_exposure: dict[str, float] = {}
    open_iron_condor_count = 0
    iron_condor_total_exposure = 0.0
    for s in db.get_open_spreads():
        underlying = s["underlying"]
        # Real bug found 2026-08-28 (while fixing the audit's pre-trade-
        # recheck gap): db.spreads.max_loss is PER CONTRACT (never
        # multiplied when recorded, see db.record_spread_open's callers) --
        # summing it raw here understated real total exposure per
        # underlying by a factor of `contracts` for any position sized
        # above 1, silently weakening the 20%-of-equity concentration cap
        # this dict feeds into (risk_gate.check_new_spread).
        contracts_held = int(s.get("contracts") or 1)
        max_loss_total = float(s.get("max_loss", 0)) * contracts_held
        existing_exposure[underlying] = existing_exposure.get(underlying, 0) + max_loss_total
        if s.get("strategy") == "iron_condor":
            open_iron_condor_count += 1
            iron_condor_total_exposure += max_loss_total

    candidates = []
    gate_rejections: list[dict] = []

    # VIXY regime overlay (2026-08-28): compute once per cycle to avoid
    # extra API calls per candidate. This is a PERCENTILE-BASED VIXY PROXY
    # for market-wide vol regime — explicitly NOT the real VIX index or
    # literal threshold levels. See _vixy_regime_percentile's docstring.
    vixy_pct = _vixy_regime_percentile(client)
    if vixy_pct is not None:
        logger.info("VIXY regime percentile: %.2f", vixy_pct)

    # Low VIXY percentile (<0.33): vol-of-vol is low for its own year,
    # iron condor strikes can be closer to ATM. Rule: use
    # min(short_leg_target_delta * 1.5, 0.30) as the target delta.
    # Rationale: 0.17 * 1.5 = 0.255, capped at 0.30 to stay within a
    # sensible range even if the base delta is ever raised. This moves
    # strikes closer to ATM (more premium collected, lower win rate) when
    # the vol environment is calm — a defensible tilt, not a backtested
    # edge.
    ic_target_delta_override: float | None = None
    if vixy_pct is not None and vixy_pct < 0.33:
        ic_target_delta_override = min(config.risk.short_leg_target_delta * 1.5, 0.30)
        logger.info(
            "VIXY percentile %.2f < 0.33 (low): iron condor target delta "
            "overridden to %.2f (closer to ATM)",
            vixy_pct, ic_target_delta_override,
        )

    tickers_for_quotes = [sig.ticker for sig, _, _ in signals_with_vol]
    try:
        snapshots = client.get_snapshots(tickers_for_quotes) if tickers_for_quotes else {}
    except Exception:
        logger.exception("Failed to batch-fetch snapshots, falling back to per-symbol quotes")
        snapshots = {}

    for sig, realized_vol, regime in signals_with_vol:
        # Regime -> strategy, per signals.regime.RegimeDetector (2x2 on ADX
        # and vol_ratio=vol_20d/vol_60d_avg, matching a teammate's strategy
        # research 2026-08-28 -- see _apply_trend_and_volatility_filters'
        # docstring for why this module rather than a reimplementation):
        # TRENDING / VOLATILE_TRENDING -> directional vertical (real trend
        # strength backs a direction). RANGING -> iron condor (no trend, no
        # elevated vol -- calm range, sell premium both sides).
        # VOLATILE_RANGING -> skip: no trend to lean on AND vol is
        # elevated, i.e. the range itself may not hold -- the research
        # flagged this cell as "wider-wing IC or skip"; skip is the
        # conservative choice until wider-wing sizing is actually built and
        # tested, matching this project's standing "a skipped cycle is
        # always safer than a guessed one" rule.
        if regime == Regime.VOLATILE_RANGING:
            logger.info("%s regime is VOLATILE_RANGING -- no trend to lean on and vol is elevated, skipping", sig.ticker)
            continue
        is_iron_condor = regime == Regime.RANGING

        # VIXY regime overlay: high percentile (>0.67) means elevated
        # vol-of-vol proxy — skip iron condor routing entirely for this
        # cycle (directional verticals still allowed). This is a
        # percentile-based VIXY proxy, NOT a real VIX threshold.
        if is_iron_condor and vixy_pct is not None and vixy_pct > 0.67:
            logger.info(
                "%s regime is RANGING but VIXY percentile %.2f > 0.67 "
                "(elevated vol-of-vol proxy) -- skipping iron condor for this cycle",
                sig.ticker, vixy_pct,
            )
            continue
        try:
            snap = snapshots.get(sig.ticker)
            if snap and snap.get("latest_ask") is not None and snap.get("latest_bid") is not None:
                spot_mid = (snap["latest_ask"] + snap["latest_bid"]) / 2
            else:
                spot = client.get_latest_quote(sig.ticker)
                spot_mid = (spot["ask_price"] + spot["bid_price"]) / 2
            if is_iron_condor:
                plan = await build_iron_condor(mcp, sig.ticker, spot_price=spot_mid, realized_vol=realized_vol, target_delta_override=ic_target_delta_override)
            else:
                # VOLATILE_TRENDING regime: use a wider spread width to
                # capture more premium in elevated-vol conditions (see
                # config.risk.volatile_trending_width_dollars). TRENDING
                # keeps the standard width (no override).
                w_override = config.risk.volatile_trending_width_dollars if regime == Regime.VOLATILE_TRENDING else None
                plan = await build_spread(mcp, sig.ticker, sig.direction, spot_price=spot_mid, realized_vol=realized_vol, width_override=w_override)
        except Exception:
            logger.exception(
                "Failed to build %s for %s",
                "iron condor" if is_iron_condor else "spread", sig.ticker,
            )
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
            existing_exposure=existing_exposure,
            underlying=sig.ticker,
            strategy="iron_condor" if is_iron_condor else "vertical",
            open_iron_condor_count=open_iron_condor_count,
            open_iron_condor_exposure=iron_condor_total_exposure,
        )
        if not check.allowed:
            logger.info("%s rejected by risk gate: %s", sig.ticker, check.reasons)
            gate_rejections.append({"ticker": sig.ticker, "reasons": check.reasons})
            continue
        candidates.append({
            "ticker": sig.ticker,
            "strategy": "iron_condor" if is_iron_condor else "vertical",
            # direction/strength/signal_reasoning come from the directional
            # swing signal -- meaningless for an iron condor (it has no
            # directional view by construction), so left None rather than
            # showing the LLM a guessed direction the trend filter didn't
            # confirm.
            "direction": None if is_iron_condor else sig.direction,
            "strength": None if is_iron_condor else sig.strength,
            "signal_reasoning": None if is_iron_condor else sig.reasoning,
            "credit_estimate": plan.credit_estimate,
            "max_loss": plan.max_loss,
            "expiration": plan.expiration.isoformat(),
            "_plan": plan,
        })
    return candidates, gate_rejections


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


async def _pre_trade_check(
    mcp: AlpacaMCP,
    plan: SpreadPlan,
    account: dict,
    open_count: int,
    existing_exposure: dict[str, float] | None = None,
) -> tuple[bool, str | None, SpreadPlan]:
    """Last-second validation before sending an order to Alpaca.

    Re-fetches fresh option quotes for both legs, recomputes the credit
    estimate, and re-runs risk_gate.check_new_spread.  If the credit has
    shrunk by more than 20 % relative to the original estimate the trade
    is skipped — the market moved against us between candidate screening
    and the LLM's decision.

    Real gap found in a 2026-08-28 audit: this used to call
    risk_gate.check_new_spread without existing_exposure/underlying,
    meaning the 20%-per-underlying concentration cap was only ever
    checked once, at the top of find_candidates() against the cycle's
    starting snapshot -- never re-verified against what run_cycle() has
    actually opened so far THIS cycle. If the LLM selected two candidates
    on the same underlying in one cycle, the second one's final gate
    would not have caught it. `existing_exposure` is now threaded through
    from run_cycle()'s own running tally (updated after each successful
    open), closing that gap.
    """
    result = await mcp.call(
        "get_option_snapshot",
        {"symbols": f"{plan.short_symbol},{plan.long_symbol}", "feed": "indicative"},
    )
    snap_by_symbol = (result or {}).get("data", {}).get("snapshots", {})
    short_snap = snap_by_symbol.get(plan.short_symbol, {})
    long_snap = snap_by_symbol.get(plan.long_symbol, {})

    short_mid = _mid_from_snapshot(short_snap)
    long_mid = _mid_from_snapshot(long_snap)
    if short_mid is None or long_mid is None:
        return False, "fresh quotes unavailable for one or both legs", plan

    now = datetime.now(timezone.utc)
    # Real bug caught 2026-08-27: this only logged a warning on a stale
    # quote and traded on it anyway. A quote hours old (market-closed
    # remnant, or a genuine feed outage) is exactly what produced a
    # nonsensical spread (credit exceeding the strike width) that day —
    # now a hard block, not just a log line.
    for label, snap in [("short", short_snap), ("long", long_snap)]:
        ts_str = snap.get("latestQuote", {}).get("t")
        if ts_str:
            try:
                quote_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age = now - quote_ts
                if age > timedelta(minutes=15):
                    return False, f"{label} leg quote is {age} old (>15 min) — stale, refusing to trade on it", plan
            except (ValueError, TypeError):
                pass

    fresh_credit = round((short_mid - long_mid) * 100, 2)
    if fresh_credit <= 0:
        return False, f"fresh credit is non-positive (${fresh_credit:.2f})", plan

    shrink_pct = (plan.credit_estimate - fresh_credit) / plan.credit_estimate
    if shrink_pct > 0.20:
        return (
            False,
            f"credit shrank {shrink_pct:.0%} (original ${plan.credit_estimate:.2f} → fresh ${fresh_credit:.2f})",
            plan,
        )

    width_dollars = abs(plan.short_strike - plan.long_strike) * 100
    updated_max_loss = round(width_dollars - fresh_credit, 2)
    if updated_max_loss <= 0:
        # Same sanity check as spread_builder.build_spread — a fresh
        # requote can hit this too, not just the initial build.
        return False, f"fresh max_loss is non-positive (${updated_max_loss:.2f}), refusing to trade", plan
    updated_plan = SpreadPlan(
        underlying=plan.underlying,
        direction=plan.direction,
        expiration=plan.expiration,
        short_strike=plan.short_strike,
        long_strike=plan.long_strike,
        short_symbol=plan.short_symbol,
        long_symbol=plan.long_symbol,
        credit_estimate=fresh_credit,
        max_loss=updated_max_loss,
    )

    today = now.date()
    check = risk_gate.check_new_spread(
        equity=float(account["equity"]),
        daily_pl_pct=float(account.get("daily_pl_pct") or 0.0),
        open_spreads_count=open_count,
        max_loss=updated_max_loss,
        expiration=plan.expiration,
        today=today,
        existing_exposure=existing_exposure,
        underlying=plan.underlying,
        strategy="vertical",
    )
    if not check.allowed:
        return False, f"risk gate rejected on fresh quotes: {check.reasons}", updated_plan

    return True, None, updated_plan


async def _pre_trade_check_iron_condor(
    mcp: AlpacaMCP,
    plan: IronCondorPlan,
    account: dict,
    open_count: int,
    existing_exposure: dict[str, float] | None = None,
    open_iron_condor_count: int = 0,
    open_iron_condor_exposure: float = 0.0,
) -> tuple[bool, str | None, IronCondorPlan]:
    """Same last-second-validation discipline as `_pre_trade_check`, applied
    to all 4 iron condor legs in one snapshot call instead of 2: fresh
    quotes required on every leg, a >15min-stale quote on ANY leg hard-
    blocks (same real bug this guards against as the vertical path — see
    `_pre_trade_check`'s docstring), credit shrink and non-positive
    max_loss re-checked against fresh mids, and the risk gate re-run —
    now including the concentration and max-concurrent-iron-condor caps
    (same 2026-08-28 audit gap `_pre_trade_check` fixed; `existing_exposure`/
    `open_iron_condor_count` come from run_cycle()'s running tally, updated
    after each successful open this cycle, not just the cycle-start
    snapshot).
    """
    symbols = [plan.short_put_symbol, plan.long_put_symbol, plan.short_call_symbol, plan.long_call_symbol]
    result = await mcp.call(
        "get_option_snapshot",
        {"symbols": ",".join(symbols), "feed": "indicative"},
    )
    snap_by_symbol = (result or {}).get("data", {}).get("snapshots", {})
    legs = [
        ("short put", plan.short_put_symbol),
        ("long put", plan.long_put_symbol),
        ("short call", plan.short_call_symbol),
        ("long call", plan.long_call_symbol),
    ]

    snaps = {sym: snap_by_symbol.get(sym, {}) for _, sym in legs}
    mids = {}
    for label, sym in legs:
        mid = _mid_from_snapshot(snaps[sym])
        if mid is None:
            return False, f"fresh quote unavailable for {label} leg ({sym})", plan
        mids[sym] = mid

    now = datetime.now(timezone.utc)
    for label, sym in legs:
        ts_str = snaps[sym].get("latestQuote", {}).get("t")
        if ts_str:
            try:
                quote_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                age = now - quote_ts
                if age > timedelta(minutes=15):
                    return False, f"{label} leg quote is {age} old (>15 min) — stale, refusing to trade on it", plan
            except (ValueError, TypeError):
                pass

    fresh_put_credit = round((mids[plan.short_put_symbol] - mids[plan.long_put_symbol]) * 100, 2)
    fresh_call_credit = round((mids[plan.short_call_symbol] - mids[plan.long_call_symbol]) * 100, 2)
    if fresh_put_credit <= 0 or fresh_call_credit <= 0:
        return (
            False,
            f"fresh credit is non-positive on one side (put ${fresh_put_credit:.2f}, call ${fresh_call_credit:.2f})",
            plan,
        )
    fresh_credit = round(fresh_put_credit + fresh_call_credit, 2)

    shrink_pct = (plan.credit_estimate - fresh_credit) / plan.credit_estimate
    if shrink_pct > 0.20:
        return (
            False,
            f"credit shrank {shrink_pct:.0%} (original ${plan.credit_estimate:.2f} → fresh ${fresh_credit:.2f})",
            plan,
        )

    put_width_dollars = abs(plan.short_put_strike - plan.long_put_strike) * 100
    call_width_dollars = abs(plan.short_call_strike - plan.long_call_strike) * 100
    if round(put_width_dollars, 2) != round(call_width_dollars, 2):
        # Should never happen (build_iron_condor already refuses unequal
        # widths), but the max_loss formula below is only valid if it holds
        # -- re-checked here rather than assumed on a plan built earlier.
        return False, "put/call widths no longer match on re-check, refusing to trade", plan

    updated_max_loss = round(put_width_dollars - fresh_credit, 2)
    if updated_max_loss <= 0:
        # Same sanity check as spread_builder.build_iron_condor — a fresh
        # requote can hit this too, not just the initial build.
        return False, f"fresh max_loss is non-positive (${updated_max_loss:.2f}), refusing to trade", plan

    min_credit = put_width_dollars * config.risk.min_credit_to_width_pct
    if fresh_credit < min_credit:
        # Same min-credit-to-width floor as build_iron_condor — a fresh
        # requote shrinking credit can drop below it even if the original
        # build passed, not just widen max_loss.
        return (
            False,
            f"fresh credit ${fresh_credit:.2f} is below the "
            f"{config.risk.min_credit_to_width_pct:.0%} min-credit-to-width floor "
            f"(${min_credit:.2f})",
            plan,
        )

    updated_plan = IronCondorPlan(
        underlying=plan.underlying,
        direction=plan.direction,
        expiration=plan.expiration,
        short_put_strike=plan.short_put_strike,
        long_put_strike=plan.long_put_strike,
        short_call_strike=plan.short_call_strike,
        long_call_strike=plan.long_call_strike,
        short_put_symbol=plan.short_put_symbol,
        long_put_symbol=plan.long_put_symbol,
        short_call_symbol=plan.short_call_symbol,
        long_call_symbol=plan.long_call_symbol,
        credit_estimate=fresh_credit,
        max_loss=updated_max_loss,
    )

    today = now.date()
    check = risk_gate.check_new_spread(
        equity=float(account["equity"]),
        daily_pl_pct=float(account.get("daily_pl_pct") or 0.0),
        open_spreads_count=open_count,
        max_loss=updated_max_loss,
        expiration=plan.expiration,
        today=today,
        existing_exposure=existing_exposure,
        underlying=plan.underlying,
        strategy="iron_condor",
        open_iron_condor_count=open_iron_condor_count,
        open_iron_condor_exposure=open_iron_condor_exposure,
    )
    if not check.allowed:
        return False, f"risk gate rejected on fresh quotes: {check.reasons}", updated_plan

    return True, None, updated_plan


def _shadow_select(candidates: list[dict], remaining_budget: int) -> list[str]:
    """Mechanical baseline: rank by strength * (credit/max_loss), pick top N.

    Real bug caught while wiring up iron condors: `strength` is explicitly
    `None` (not absent) on an iron_condor candidate (see find_candidates —
    it has no directional signal by construction), so `c.get("strength", 0)`
    returned `None`, not the intended `0` default, and `strength * rr` would
    have raised TypeError the first time an iron condor reached here. An
    iron condor has no directional conviction to weigh, so it's scored on
    risk/reward alone — same framing given to the LLM in llm_reasoner's
    SYSTEM_PROMPT.
    """
    if not candidates or remaining_budget <= 0:
        return []
    scored = []
    for c in candidates:
        credit = c.get("credit_estimate", 0)
        max_loss = c.get("max_loss", 1)
        rr = credit / max_loss if max_loss > 0 else 0
        strength = c.get("strength")
        score = rr if strength is None else strength * rr
        scored.append((c["ticker"], score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [ticker for ticker, _ in scored[:remaining_budget]]


async def run_cycle() -> None:
    # Kill switch — file-based, works even if DB is down
    PAUSE_FILE = Path(__file__).resolve().parent / "state" / "PAUSE"
    if PAUSE_FILE.exists():
        print("KILL SWITCH ACTIVE — skipping this cycle")
        return

    _load_evolved_params()

    client = AlpacaClient()
    account = client.get_account()
    daily_pl, daily_pl_pct = _daily_pl(account)
    account["daily_pl"] = daily_pl
    account["daily_pl_pct"] = daily_pl_pct

    async with AlpacaMCP() as mcp:
        close_notes = await manage_open_spreads(mcp)

        try:
            open_spreads = db.get_open_spreads()
        except Exception:
            logger.exception("Failed to read open spreads from DB, assuming 0")
            open_spreads = []
        remaining_budget = max(0, _risk("max_concurrent_spreads") - len(open_spreads))

        # Running tallies for the pre-trade re-check below (2026-08-28 audit
        # fix): find_candidates()'s own concentration/IC-count checks only
        # see this cycle's STARTING snapshot. If the LLM selects more than
        # one candidate this cycle, each one actually opened here must
        # update these before the next candidate's pre-trade check runs,
        # or a same-cycle multi-select could blow past the concentration
        # cap or the max-concurrent-iron-condor cap without either final
        # gate ever seeing it.
        running_exposure: dict[str, float] = {}
        running_ic_count = 0
        running_ic_exposure = 0.0
        for s in open_spreads:
            # Same per-contract-vs-total fix as find_candidates()'s
            # existing_exposure -- db.spreads.max_loss is per contract.
            contracts_held = int(s.get("contracts") or 1)
            max_loss_total = float(s.get("max_loss", 0)) * contracts_held
            running_exposure[s["underlying"]] = running_exposure.get(s["underlying"], 0) + max_loss_total
            if s.get("strategy") == "iron_condor":
                running_ic_count += 1
                running_ic_exposure += max_loss_total
        running_spread_count = len(open_spreads)

        # Defense in depth, added 2026-08-27 after a real incident: this
        # bot is only ever meant to open NEW positions while the market is
        # actually open (the cron schedule already covers that in the
        # common case, but a manual/out-of-schedule invocation has no such
        # guard). Options market orders get rejected by Alpaca outside
        # market hours anyway (confirmed live: HTTP 422, "options market
        # orders are only allowed during market hours") -- checking here
        # avoids wasting a full screening pass building candidates that can
        # never actually execute, and closes the exact gap that produced a
        # phantom "opened" db record that evening (see executor_mcp.py's
        # _extract_order_ids for the other half of that fix). Managing
        # already-open spreads still runs regardless -- force-close-by-
        # deadline shouldn't wait on this check.
        try:
            market_open = client.get_clock()["is_open"]
        except Exception:
            logger.exception("Failed to check market clock, assuming closed (fail safe, not fail open)")
            market_open = False

        open_notes = []
        candidates = []
        slim_candidates: list[dict] = []
        decision = "skipped"
        reasoning = (
            "No eligible candidates this cycle." if market_open
            else "Market is closed — not screening for new candidates this cycle."
        )
        gate_rejections: list[dict] = []
        pre_trade_rejections: list[dict] = []
        shadow_selected: list[str] = []
        llm_selected: list[str] = []
        cycle_id: int | None = None

        if remaining_budget > 0 and market_open:
            candidates, gate_rejections = await find_candidates(mcp, client, account, len(open_spreads))
            slim_candidates = [{k: v for k, v in c.items() if k != "_plan"} for c in candidates]

            outcome = llm_reasoner.decide(slim_candidates, remaining_budget)
            reasoning = outcome["reasoning"]
            llm_selected = outcome["selected"]
            selected_tickers = set(llm_selected)

            shadow_selected = _shadow_select(slim_candidates, remaining_budget)

            # "pending" placeholder: cycle_id is needed below so
            # record_spread_open can reference it, but the real outcome
            # (opened/skipped/error) isn't known until the loop below runs.
            # Corrected via db.update_cycle_decision() once it is — see the
            # bug this replaced in update_cycle_decision's own docstring.
            try:
                cycle_id = db.record_cycle(slim_candidates, "pending", reasoning, generation=_current_generation)
            except Exception:
                logger.exception("Failed to record cycle to DB")
                cycle_id = None

            for c in candidates:
                if c["ticker"] not in selected_tickers:
                    continue
                plan = c["_plan"]
                is_iron_condor = plan.direction == "iron_condor"
                try:
                    if is_iron_condor:
                        allowed, reason, plan = await _pre_trade_check_iron_condor(
                            mcp, plan, account, running_spread_count,
                            existing_exposure=running_exposure,
                            open_iron_condor_count=running_ic_count,
                            open_iron_condor_exposure=running_ic_exposure,
                        )
                    else:
                        allowed, reason, plan = await _pre_trade_check(
                            mcp, plan, account, running_spread_count,
                            existing_exposure=running_exposure,
                        )
                    if not allowed:
                        logger.info("Pre-trade check blocked %s: %s", plan.underlying, reason)
                        open_notes.append(f"Pre-trade check blocked {plan.underlying}: {reason}")
                        pre_trade_rejections.append({"ticker": c["ticker"], "reason": reason})
                        continue
                    contracts = _optimal_contracts(
                        equity=float(account["equity"]),
                        max_loss_per_contract=plan.max_loss,
                        max_risk_pct=_risk("max_loss_per_spread_pct"),
                    )
                    # Real bug caught in review 2026-08-27: this used to be
                    # computed AFTER open_spread(mcp, plan) was already
                    # called without a contracts= argument, so the real
                    # order on Alpaca was always 1 contract regardless of
                    # what got recorded in the DB — a genuine mismatch
                    # between what actually executed and what we'd report.
                    # Kill switch check before each spread open
                    if Path(__file__).resolve().parent.joinpath("state", "PAUSE").exists():
                        print("KILL SWITCH ACTIVE — aborting remaining opens")
                        break

                    if is_iron_condor:
                        order_ids = await executor_mcp.open_iron_condor(mcp, plan, contracts=contracts)
                    else:
                        order_ids = await executor_mcp.open_spread(mcp, plan, contracts=contracts)
                    try:
                        if is_iron_condor:
                            db.record_spread_open(
                                underlying=plan.underlying,
                                direction=plan.direction,
                                expiration=plan.expiration.isoformat(),
                                short_strike=plan.short_put_strike,
                                long_strike=plan.long_put_strike,
                                short_symbol=plan.short_put_symbol,
                                long_symbol=plan.long_put_symbol,
                                contracts=contracts,
                                credit_received=plan.credit_estimate,
                                max_loss=plan.max_loss,
                                alpaca_order_ids=order_ids,
                                cycle_id=cycle_id,
                                generation=_current_generation,
                                strategy="iron_condor",
                                call_short_strike=plan.short_call_strike,
                                call_long_strike=plan.long_call_strike,
                                call_short_symbol=plan.short_call_symbol,
                                call_long_symbol=plan.long_call_symbol,
                            )
                        else:
                            db.record_spread_open(
                                underlying=plan.underlying,
                                direction=plan.direction,
                                expiration=plan.expiration.isoformat(),
                                short_strike=plan.short_strike,
                                long_strike=plan.long_strike,
                                short_symbol=plan.short_symbol,
                                long_symbol=plan.long_symbol,
                                contracts=contracts,
                                credit_received=plan.credit_estimate,
                                max_loss=plan.max_loss,
                                alpaca_order_ids=order_ids,
                                cycle_id=cycle_id,
                                generation=_current_generation,
                                strategy="vertical",
                            )
                    except Exception:
                        logger.exception("Failed to record spread open to DB (order already sent to Alpaca)")
                    open_notes.append(
                        f"Opened {plan.underlying} {plan.direction} x{contracts} contract(s): "
                        f"credit ${plan.credit_estimate * contracts:.2f} total "
                        f"(${plan.credit_estimate:.2f}/contract), "
                        f"max loss ${plan.max_loss * contracts:.2f} total"
                    )
                    decision = "opened"
                    # Update running tallies so the next candidate's
                    # pre-trade check in this same cycle sees the
                    # exposure we just added (same 2026-08-28 audit fix
                    # discipline as the initial tally above).
                    running_spread_count += 1
                    max_loss_just_opened = plan.max_loss * contracts
                    running_exposure[plan.underlying] = running_exposure.get(plan.underlying, 0) + max_loss_just_opened
                    if is_iron_condor:
                        running_ic_count += 1
                        running_ic_exposure += max_loss_just_opened
                except Exception as exc:
                    logger.exception("Failed to open spread for %s", plan.underlying)
                    open_notes.append(f"ERROR opening {plan.underlying}: {exc}")
                    decision = "error"

        # Real bug fixed 2026-08-28: this used to re-insert a second cycle
        # row only for the "skipped" outcome, and left "opened" (the
        # placeholder hardcoded above) standing uncorrected for "error" —
        # a candidate the LLM picked but failed to open stayed mislabeled
        # "opened" in the cycles table forever. Now every outcome (opened/
        # skipped/error) is written exactly once, via insert-if-missing or
        # update-if-pending, never both.
        if cycle_id is None:
            try:
                cycle_id = db.record_cycle(slim_candidates, decision, reasoning, generation=_current_generation)
            except Exception:
                logger.exception("Failed to record cycle to DB (non-fatal)")
        else:
            try:
                db.update_cycle_decision(cycle_id, decision, reasoning)
            except Exception:
                logger.exception("Failed to update cycle decision (non-fatal)")

        if cycle_id is not None:
            try:
                db.record_decision_journal(
                    cycle_id=cycle_id,
                    candidates=slim_candidates,
                    llm_selected=llm_selected,
                    llm_reasoning=reasoning,
                    shadow_selected=shadow_selected,
                    gate_rejections=gate_rejections,
                    pre_trade_rejections=pre_trade_rejections,
                )
            except Exception:
                logger.exception("Failed to record decision journal (non-fatal)")

        try:
            db.record_account_snapshot(
                equity=float(account["equity"]),
                last_equity=float(account.get("last_equity")) if account.get("last_equity") else None,
                cash=float(account.get("cash")) if account.get("cash") else None,
                open_spreads_count=len(open_spreads),
                daily_pl=float(account.get("daily_pl")) if account.get("daily_pl") else None,
                daily_pl_pct=float(account.get("daily_pl_pct")) if account.get("daily_pl_pct") else None,
            )
        except Exception:
            logger.exception("Failed to record account snapshot (non-fatal)")

        for note in close_notes + open_notes:
            print(note)


if __name__ == "__main__":
    asyncio.run(run_cycle())
