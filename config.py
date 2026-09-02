from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val is not None else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val is not None else default


def _evolved_params() -> dict:
    """Reads state/evolved_params.json once (module-level, so it's read
    exactly once per process = once per cron tick). Returns {} if the
    file doesn't exist, can't parse, or has no promoted generation yet
    -- 0 evolved overrides means "use env-var/literal defaults", the
    existing behavior before this project had an evolution system."""
    path = Path(__file__).resolve().parent / "state" / "evolved_params.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {k: v for k, v in data.items()
                 if k not in ("evolved_at", "generation", "promotion_reason")}
    except Exception:
        return {}


_EVOLVED = _evolved_params()


def _evolved_or_env_float(key: str, env_name: str, default: float) -> float:
    if key in _EVOLVED:
        return float(_EVOLVED[key])
    return _env_float(env_name, default)


def _evolved_or_env_int(key: str, env_name: str, default: int) -> int:
    if key in _EVOLVED:
        return int(_EVOLVED[key])
    return _env_int(env_name, default)


@dataclass(frozen=True)
class AlpacaConfig:
    """Credentials for the hackathon's dedicated paper account — never the
    trading_bot/ account. Market data (bars/quotes used by the vendored
    screening/signals modules) is account-agnostic, so pointing everything
    at this one account keeps a single, unambiguous account ID for judging.
    """
    api_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY"))
    secret_key: str = field(default_factory=lambda: _env("ALPACA_SECRET_KEY"))
    base_url: str = field(
        default_factory=lambda: _env(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        )
    )
    data_url: str = field(
        default_factory=lambda: _env(
            "ALPACA_DATA_URL", "https://data.alpaca.markets"
        )
    )


@dataclass(frozen=True)
class ScreeningFilters:
    """Same liquid-universe filter values validated in trading_bot/ — carried
    over unchanged since the underlying-selection problem (liquid, mid/large
    cap, reasonable price, real ATR) doesn't change just because the
    executed instrument is now an options spread instead of shares.
    """
    min_avg_volume: int = field(default_factory=lambda: _env_int("MIN_AVG_VOLUME", 500_000))
    min_market_cap: float = field(default_factory=lambda: _env_float("MIN_MARKET_CAP", 5e9))
    max_market_cap: float = field(default_factory=lambda: _env_float("MAX_MARKET_CAP", 2e12))
    min_price: float = field(default_factory=lambda: _env_float("MIN_PRICE", 10.0))
    max_price: float = field(default_factory=lambda: _env_float("MAX_PRICE", 300.0))
    max_spread_pct: float = field(default_factory=lambda: _env_float("MAX_SPREAD_PCT", 0.5))
    min_atr_pct: float = field(default_factory=lambda: _env_float("MIN_ATR_PCT", 0.5))


@dataclass(frozen=True)
class OptionsRiskLimits:
    """The hackathon submission's "risk gates" — deliberately conservative
    given only ~5 trading days of judged activity (kickoff Fri afternoon
    through the following Fri morning) and a brand-new $100k account with no
    track record yet.

    Sized as % of account equity, not a fixed dollar figure, so the gates
    stay correct even if equity moves during the week.
    """
    max_loss_per_spread_pct: float = field(
        # Max defined loss (width - credit) for a single spread, as % of
        # equity at entry time. 2% mirrors trading_bot's own per-position
        # sizing discipline (it uses 10% of equity per position, but that's
        # notional stock exposure; a credit spread's *max loss* is a much
        # sharper number, so this is deliberately tighter).
        default_factory=lambda: _evolved_or_env_float("max_loss_per_spread_pct", "MAX_LOSS_PER_SPREAD_PCT", 0.02)
    )
    max_daily_loss_pct: float = field(
        # Same 3% circuit breaker as trading_bot/config.py's RiskLimits —
        # once daily realized+unrealized P&L breaches -3%, no new spreads
        # open for the rest of that session.
        default_factory=lambda: _env_float("MAX_DAILY_LOSS_PCT", 0.03)
    )
    max_concurrent_spreads: int = field(
        # 5 -> 7 (2026-08-31, Alex): deliberate volume-over-quality lever --
        # more small parallel bets instead of fewer larger ones, same
        # per-trade risk. Not independently backtested; concentration/
        # cluster caps (max_concentration_pct/max_cluster_concentration_pct)
        # already bound how correlated those extra slots can get.
        default_factory=lambda: _env_int("MAX_CONCURRENT_SPREADS", 7)
    )
    min_dte: int = field(
        # PENDING COMPARISON, 2026-08-28: set to 7 here to follow the
        # 3-strategy spec doc (external report, tastytrade-cited "more
        # theta in fewer days"), overriding this project's OWN 2026-08-27
        # walk-forward backtest (`backtest_optimize.py`), which found 10-21
        # outperforms 7-14 "with a large, sign-changing difference, not
        # marginal" (see README's "Parameter optimization pass" section
        # for the full writeup) -- that finding was never invalidated, just
        # deliberately overridden by the newer external spec. Real trading
        # data accumulating under 7-14 (tag-able by generation via
        # db.get_realized_pnl_by_generation, same mechanism the evolution
        # audit trail uses) is the real tiebreaker to watch for — if it
        # confirms the backtest's original finding, revert to 10/21.
        default_factory=lambda: _evolved_or_env_int("min_dte", "MIN_DTE", 7)
    )
    max_dte: int = field(
        # See min_dte's comment — same pending-comparison flag, was 21.
        default_factory=lambda: _evolved_or_env_int("max_dte", "MAX_DTE", 14)
    )
    short_leg_target_delta: float = field(
        # 16-18 delta, not 25 — published large-sample studies (tastytrade,
        # ~85% win rate at 15-delta vs ~71% at 30-delta) argue 25-30 delta is
        # fine in EXPECTED VALUE over hundreds of trades, but we only get a
        # handful of trades in a ~5-day judged window, where variance (one
        # loss in a 3-trade sample) dominates what judges actually see over
        # long-run expectancy. 16-delta is separately cited as close to the
        # theta-per-day sweet spot, so this isn't purely a win-rate-over-EV
        # trade-off for our case (2026-08-26 research pass).
        # 0.17 -> 0.13 (2026-08-31, Alex): pushes further in the same
        # direction as the research above -- smaller, higher-probability
        # wins, more of them, instead of chasing bigger premium per trade.
        # Deliberately a modest step, not a leap: the 2026-08-27 backtest
        # regression (0.17 vs 0.20, reverted after verify_backtest.py found
        # up to ~60% strike-rounding delta error on low-priced names) is the
        # concrete reason not to swing further without the same synthetic-
        # case verification. Not independently backtested at 0.13 --
        # watch real per-generation P&L via the evolution audit trail.
        default_factory=lambda: _evolved_or_env_float("short_leg_target_delta", "SHORT_LEG_TARGET_DELTA", 0.13)
    )
    spread_width_dollars: float = field(
        # Distance between short and long strikes. $5 wide is a clean,
        # common increment for the liquid large/mid-caps this screening
        # universe selects (see ScreeningFilters.min_price/max_price).
        default_factory=lambda: _evolved_or_env_float("spread_width_dollars", "SPREAD_WIDTH_DOLLARS", 5.0)
    )
    volatile_trending_width_dollars: float = field(
        # Wider spread width for VOLATILE_TRENDING regime (2026-08-28):
        # roughly double the standard $5 width. The rationale is that
        # elevated vol (vol_ratio > 1.5) with a strong trend (ADX > 25)
        # means larger expected moves, so a wider spread captures more
        # premium and gives the trade more room. NOTE: this exact $10
        # default is a defensible-but-not-precisely-researched choice;
        # unlike most of this project's other parameters, it has NOT been
        # backtested. Override via VOLATILE_TRENDING_WIDTH_DOLLARS if
        # live results suggest a different width.
        default_factory=lambda: _env_float("VOLATILE_TRENDING_WIDTH_DOLLARS", 10.0)
    )
    profit_target_pct: float = field(
        # 0.50 -> 0.30 (2026-08-31, Alex): standard credit-spread management
        # already reduces tail-risk exposure to gamma near expiry at 50%;
        # closing earlier at 30% is the same volume-over-quality lever as
        # the lower delta above -- recycles capital into a new trade sooner
        # instead of holding out for the last bit of theta, more completed
        # trades over the judged week rather than fewer, fuller ones. Not
        # independently backtested at 0.30 -- watch real per-generation P&L.
        default_factory=lambda: _evolved_or_env_float("profit_target_pct", "PROFIT_TARGET_PCT", 0.30)
    )
    stop_loss_multiple: float = field(
        # Close if the spread's mark-to-market loss reaches this multiple of
        # credit received (e.g. 2x credit received = stop out). Within the
        # commonly-cited 1.5-2x professional range — kept as-is, no evidence
        # this needs to move for our situation (2026-08-26 research pass).
        default_factory=lambda: _evolved_or_env_float("stop_loss_multiple", "STOP_LOSS_MULTIPLE", 2.0)
    )
    min_open_interest: int = field(
        # Per-contract liquidity gate, applied to BOTH legs — equity-level
        # liquidity (ScreeningFilters.min_avg_volume) is a poor proxy for
        # options liquidity specifically; a heavily-traded stock can still
        # have a thin market on a given strike/expiration. Rejects rather
        # than silently widening the spread search (2026-08-26 research pass).
        default_factory=lambda: _evolved_or_env_int("min_open_interest", "MIN_OPEN_INTEREST", 100)
    )
    max_bid_ask_spread_pct: float = field(
        # Max (ask - bid) / mid on a single leg's quote. 12% sits in the
        # commonly-cited 10-15% "tradeable" band for single-name equity
        # options (index/ETF options are usually much tighter, but this
        # screening universe is single names).
        default_factory=lambda: _evolved_or_env_float("max_bid_ask_spread_pct", "MAX_BID_ASK_SPREAD_PCT", 0.12)
    )
    max_concentration_pct: float = field(
        # No single underlying should represent more than this fraction of
        # equity — prevents one position from dominating the portfolio.
        default_factory=lambda: _env_float("MAX_CONCENTRATION_PCT", 0.20)
    )
    max_concurrent_iron_condors: int = field(
        # Separate, tighter cap than max_concurrent_spreads (5) -- a 4-leg
        # structure ties up more of the concentration/liquidity budget per
        # position than a 2-leg vertical, and the regime that produces iron
        # condor candidates (ADX below TrendFilter's threshold, i.e. no real
        # trend backing them) can affect much of the screening universe at
        # once, so nothing else stops every open slot from filling with
        # correlated range-bound bets on the same low-volatility stretch.
        #
        # COUNT-BASED APPROXIMATION of the team's 35%/35%/30% capital-
        # allocation spec (vertical-bull / vertical-bear / iron-condor):
        # this is NOT a real equity-percentage split. Because
        # _optimal_contracts sizes every position (regardless of strategy)
        # to roughly the same ~2% of equity risk, a count-based ratio
        # approximates a capital-based ratio reasonably when several
        # positions are open, but they are NOT the same mechanism and can
        # diverge sharply at low position counts (e.g., 1 open IC out of
        # 1 total open position is 100% by count but could be a small
        # fraction of total equity). See also max_iron_condor_equity_pct
        # for the real equity-percentage cap that this count-based cap
        # does NOT provide.
        default_factory=lambda: _env_int("MAX_CONCURRENT_IRON_CONDORS", 2)
    )
    min_credit_to_width_pct: float = field(
        # Iron-condor-specific floor: reject if total credit is below this
        # fraction of the (equal) wing width -- e.g. 1/3 of a $5 wing is
        # $1.67. A cited tastytrade rule of thumb for whether the premium
        # collected is worth the defined risk taken on; verticals don't have
        # an equivalent check today, kept iron-condor-only rather than
        # applied retroactively without the same research backing it there.
        default_factory=lambda: _env_float("MIN_CREDIT_TO_WIDTH_PCT", 1 / 3)
    )
    max_iron_condor_equity_pct: float = field(
        # Real equity-percentage cap on total iron condor exposure — the
        # functional gate that max_concurrent_iron_condors' count-based
        # approximation (see its own docstring) explicitly does NOT provide.
        # Aggregates max_loss * contracts across ALL open iron condors and
        # rejects a new IC if the total would exceed this fraction of
        # current equity. Mirrors max_concentration_pct's code shape, just
        # aggregated across all ICs instead of per-underlying.
        default_factory=lambda: _env_float("MAX_IRON_CONDOR_EQUITY_PCT", 0.30)
    )
    max_cluster_concentration_pct: float = field(
        # Real gap max_concentration_pct (per-underlying) does NOT cover
        # (2026-08-29, research pass): mega-cap tech pairwise correlations
        # spike toward ~0.9 in stress, so several concurrent spreads on
        # different mega-cap names aren't independent bets -- see
        # screening/correlation_clusters.py. Looser than the 20%
        # per-underlying cap since it aggregates multiple symbols by
        # design (two symbols at the per-underlying cap should still fit);
        # a starting value, not independently backtested -- watch real
        # per-generation P&L the same way every other threshold here is
        # watched, same evolution audit-trail mechanism.
        default_factory=lambda: _env_float("MAX_CLUSTER_CONCENTRATION_PCT", 0.40)
    )
    contest_end_utc: str = field(
        # Hard close-out deadline, independent of profit/loss — added
        # specifically because should_close() previously only fired on
        # profit-target/stop-loss, so a spread opened late in the week could
        # still be open and undemonstrated at judging time. See
        # risk_gate.should_force_close().
        default_factory=lambda: _env("CONTEST_END_UTC", "2026-09-04T15:00:00+00:00")
    )
    max_entry_slippage_pct: float = field(
        # Real gap found 2026-08-29 comparing against a competing team's
        # hardening pass: executor_mcp.py placed unbounded MARKET orders for
        # every multi-leg spread (a long-standing TODO in this file, never
        # closed) -- on a thin iron-condor strike, a market order can fill at
        # a materially worse net credit/debit than the mid this project's own
        # pre-trade gate just checked, with no floor. Orders are now
        # marketable LIMIT orders instead: accept no less than
        # checked_credit * (1 - this) on open, pay no more than
        # checked_debit * (1 + this) on close. 10% is deliberately generous
        # (this is about bounding a bad fill, not chasing best execution) --
        # not independently backtested.
        default_factory=lambda: _env_float("MAX_ENTRY_SLIPPAGE_PCT", 0.10)
    )
    order_poll_timeout_s: float = field(
        # Real gap found 2026-08-30 (same cross-check as max_entry_slippage_pct
        # above): a limit order is not guaranteed an immediate fill the way a
        # market order during market hours effectively is. Before this, a
        # spread was recorded "open" with an ESTIMATED credit the instant
        # Alpaca accepted the order, never confirming it actually filled --
        # harmless drift under market orders, a real correctness gap now
        # that entries are marketable limits (see executor_mcp.py). Bounded
        # short: candidates already passed the liquidity gate, so a
        # marketable limit should fill in seconds in the normal case: this
        # is a ceiling against a genuinely stuck order, not an expected wait.
        default_factory=lambda: _env_float("ORDER_POLL_TIMEOUT_S", 45.0)
    )
    order_poll_interval_s: float = field(
        default_factory=lambda: _env_float("ORDER_POLL_INTERVAL_S", 2.0)
    )
    debit_leg_target_delta: float = field(
        # Directional DEBIT spread overlay (2026-09-02): ported from the
        # complementary strategy validated first on Paco/mcp_risk_proxy
        # (see mcp_risk_proxy/server.py's own debit-spread comments and
        # AGENTS.md's "Debit-spread overlay" section) after real trading
        # produced zero fills for several days under credit-only verticals
        # + iron condors. A debit vertical BUYS the near-the-money leg (the
        # actual directional bet, unlike a credit spread's sold leg) and
        # SELLS a further-OTM leg to reduce cost -- profits from real price
        # movement, not time decay, which is the opposite exposure of this
        # project's existing two structures. 0.60 targets a leg with real
        # directional payoff (higher delta = more like owning the
        # underlying) while still capping cost via the sold leg. Not
        # independently backtested -- watch real per-generation P&L like
        # every other threshold in this file.
        default_factory=lambda: _evolved_or_env_float("debit_leg_target_delta", "DEBIT_LEG_TARGET_DELTA", 0.60)
    )
    debit_delta_sanity_min: float = field(
        # Sanity band on the BUY leg's computed delta (mirrors
        # MAX_DELTA_DEVIATION's role for credit spreads, but as an absolute
        # band rather than a deviation-from-target, matching the band
        # already proven on Paco's mcp_risk_proxy -- see its
        # _DEBIT_DELTA_SANITY_MIN/MAX constants).
        default_factory=lambda: _env_float("DEBIT_DELTA_SANITY_MIN", 0.35)
    )
    debit_delta_sanity_max: float = field(
        default_factory=lambda: _env_float("DEBIT_DELTA_SANITY_MAX", 0.80)
    )
    debit_min_adx: float = field(
        # Stricter than RegimeDetector's own ADX>25 TRENDING threshold
        # (signals/regime.py) -- a debit spread pays real premium up front
        # and only profits from actual movement, so it needs real
        # conviction behind it, not just "trending enough for a credit
        # vertical". Mirrors Paco's AGENTS.md "Debit-spread overlay" rule
        # (ADX > 35). Not independently backtested.
        default_factory=lambda: _env_float("DEBIT_MIN_ADX", 35.0)
    )
    debit_min_signal_strength: float = field(
        # Second half of the same conviction bar -- signals.swing's
        # `strength` is a 0-1 scale (min(abs(score)/2.0, 1.0)). Both this
        # AND debit_min_adx must clear before a debit spread is even
        # attempted; never fabricates conviction the way a lower bar would.
        # Not independently backtested.
        default_factory=lambda: _env_float("DEBIT_MIN_SIGNAL_STRENGTH", 0.65)
    )
    debit_profit_target_pct: float = field(
        # Debit-spread equivalent of profit_target_pct, but against MAX
        # GAIN (width - debit paid) rather than max credit, since a debit
        # spread's economics are the mirror image of a credit spread's --
        # see risk_gate.should_close's debit branch. Mirrors the value
        # already in use on Paco's AGENTS.md closing-formula section. Not
        # independently backtested.
        default_factory=lambda: _evolved_or_env_float("debit_profit_target_pct", "DEBIT_PROFIT_TARGET_PCT", 0.30)
    )
    debit_stop_pct: float = field(
        # Stop once proceeds-from-closing fall to this fraction of the
        # original debit paid (a FLOOR, the mirror image of
        # stop_loss_multiple's ceiling on a credit spread's cost to close).
        # Mirrors the value already in use on Paco. Not independently
        # backtested.
        default_factory=lambda: _evolved_or_env_float("debit_stop_pct", "DEBIT_STOP_PCT", 0.50)
    )


# Scope note (2026-08-28): Real IV Rank was considered specifically for
# iron-condor entry gating (per the team's 3-strategy spec doc: "IV Rank >
# 30 -> enter IC"). It is NOT implementable on this account — confirmed
# live: no OPRA/Greeks access (403 on feed=opra), and the free indicative
# feed returns no implied-vol data at all. This project deliberately reuses
# the existing realized-volatility-percentile filter below as-is for all
# strategies, rather than fabricating a fake IV Rank number from the
# realized-vol proxy. The realized-vol percentile IS a meaningful signal
# (see the tastytrade-cited study in the docstring below), but it is NOT
# the same thing as IV Rank and should not be relabeled as such.


@dataclass(frozen=True)
class VolatilityFilter:
    """A realized-volatility-percentile proxy for true IV rank — research
    (tastytrade, 595-symbol study) shows entering credit spreads only when
    IV rank/percentile is elevated lifts win rate materially (48.2% ->
    56.8% in that study), but true IV rank needs a 52-week implied-vol
    history this project doesn't have wired up. `_apply_trend_filter` in
    bot.py already pulls ~400 days of daily bars for the EMA/ADX trend
    check — this reuses that same data to rank current realized volatility
    (20-day ATR%) against its own trailing year, no new API calls. This is
    a REALIZED-vol proxy, not implied-vol rank, and is labeled as such
    everywhere it's surfaced (dashboard reasoning, ONE_PAGER.md) rather than
    overclaiming (2026-08-26 research pass).
    """
    enabled: bool = field(default_factory=lambda: _env("VOL_FILTER_ENABLED", "true").lower() == "true")
    lookback_window: int = field(default_factory=lambda: _env_int("VOL_LOOKBACK_ATR_WINDOW", 20))
    min_percentile: float = field(
        # Require current 20-day ATR% to be at/above this percentile of its
        # own trailing-year range — "elevated realized vol" as a cheap stand-
        # in for "elevated IV rank." 0.40 is the value the tastytrade study
        # above actually used (48.2% -> 56.8% win rate at that threshold) —
        # kept at 0.40, not the 0.25 briefly tried 2026-08-27 as a same-day
        # reaction to a high rejection rate with zero external evidence for
        # that specific number (see relaxed_min_percentile below for the
        # honest way to handle a genuinely low-vol stretch).
        default_factory=lambda: _evolved_or_env_float("min_percentile", "VOL_MIN_PERCENTILE", 0.40)
    )
    relaxed_min_percentile: float = field(
        # Adaptive fallback (2026-08-27): if the baseline threshold above
        # would reject more than max_rejection_rate_before_relax of a
        # cycle's candidates, fall back to this lower percentile for that
        # cycle instead of trading zero names — a documented, logged rule
        # applied only when triggered, not a permanently-lowered bar.
        default_factory=lambda: _env_float("VOL_RELAXED_MIN_PERCENTILE", 0.25)
    )
    max_rejection_rate_before_relax: float = field(
        default_factory=lambda: _env_float("VOL_MAX_REJECTION_RATE_BEFORE_RELAX", 0.80)
    )


@dataclass(frozen=True)
class SupabaseConfig:
    db_host: str = field(default_factory=lambda: _env("SUPABASE_DB_HOST"))
    db_port: int = field(default_factory=lambda: _env_int("SUPABASE_DB_PORT", 5432))
    db_name: str = field(default_factory=lambda: _env("SUPABASE_DB_NAME", "postgres"))
    db_user: str = field(default_factory=lambda: _env("SUPABASE_DB_USER"))
    db_password: str = field(default_factory=lambda: _env("SUPABASE_DB_PASSWORD"))
    schema: str = field(default_factory=lambda: _env("SUPABASE_SCHEMA", "alpaca_hackathon"))


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))


@dataclass(frozen=True)
class AppConfig:
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    screening: ScreeningFilters = field(default_factory=ScreeningFilters)
    risk: OptionsRiskLimits = field(default_factory=OptionsRiskLimits)
    volatility: VolatilityFilter = field(default_factory=VolatilityFilter)
    supabase: SupabaseConfig = field(default_factory=SupabaseConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


config = AppConfig()
