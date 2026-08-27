from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val is not None else default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    return int(val) if val is not None else default


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
        default_factory=lambda: _env_float("MAX_LOSS_PER_SPREAD_PCT", 0.02)
    )
    max_daily_loss_pct: float = field(
        # Same 3% circuit breaker as trading_bot/config.py's RiskLimits —
        # once daily realized+unrealized P&L breaches -3%, no new spreads
        # open for the rest of that session.
        default_factory=lambda: _env_float("MAX_DAILY_LOSS_PCT", 0.03)
    )
    max_concurrent_spreads: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENT_SPREADS", 5)
    )
    min_dte: int = field(
        # 10-21 days, not the textbook 30-45 — the contest window itself is
        # only ~5 trading days, so full alignment with "enter at 30-45,
        # manage out by 21" isn't available to us. This range is deliberately
        # pulled *out* of the highest-gamma stretch (widened from an earlier
        # 7-14, which sat entirely inside the zone convention says to have
        # already exited by), while still resolving close enough to the
        # judging window for `account_snapshots`' daily mark-to-market to
        # show real, meaningful movement even on positions that never fully
        # close (2026-08-26 research pass, see ONE_PAGER.md).
        default_factory=lambda: _env_int("MIN_DTE", 10)
    )
    max_dte: int = field(
        default_factory=lambda: _env_int("MAX_DTE", 21)
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
        default_factory=lambda: _env_float("SHORT_LEG_TARGET_DELTA", 0.17)
    )
    spread_width_dollars: float = field(
        # Distance between short and long strikes. $5 wide is a clean,
        # common increment for the liquid large/mid-caps this screening
        # universe selects (see ScreeningFilters.min_price/max_price).
        default_factory=lambda: _env_float("SPREAD_WIDTH_DOLLARS", 5.0)
    )
    profit_target_pct: float = field(
        # Close early once 50% of max credit is captured — standard credit-
        # spread management, reduces tail-risk exposure to gamma near expiry.
        default_factory=lambda: _env_float("PROFIT_TARGET_PCT", 0.50)
    )
    stop_loss_multiple: float = field(
        # Close if the spread's mark-to-market loss reaches this multiple of
        # credit received (e.g. 2x credit received = stop out). Within the
        # commonly-cited 1.5-2x professional range — kept as-is, no evidence
        # this needs to move for our situation (2026-08-26 research pass).
        default_factory=lambda: _env_float("STOP_LOSS_MULTIPLE", 2.0)
    )
    min_open_interest: int = field(
        # Per-contract liquidity gate, applied to BOTH legs — equity-level
        # liquidity (ScreeningFilters.min_avg_volume) is a poor proxy for
        # options liquidity specifically; a heavily-traded stock can still
        # have a thin market on a given strike/expiration. Rejects rather
        # than silently widening the spread search (2026-08-26 research pass).
        default_factory=lambda: _env_int("MIN_OPEN_INTEREST", 100)
    )
    max_bid_ask_spread_pct: float = field(
        # Max (ask - bid) / mid on a single leg's quote. 12% sits in the
        # commonly-cited 10-15% "tradeable" band for single-name equity
        # options (index/ETF options are usually much tighter, but this
        # screening universe is single names).
        default_factory=lambda: _env_float("MAX_BID_ASK_SPREAD_PCT", 0.12)
    )
    max_concentration_pct: float = field(
        # No single underlying should represent more than this fraction of
        # equity — prevents one position from dominating the portfolio.
        default_factory=lambda: _env_float("MAX_CONCENTRATION_PCT", 0.20)
    )
    contest_end_utc: str = field(
        # Hard close-out deadline, independent of profit/loss — added
        # specifically because should_close() previously only fired on
        # profit-target/stop-loss, so a spread opened late in the week could
        # still be open and undemonstrated at judging time. See
        # risk_gate.should_force_close().
        default_factory=lambda: _env("CONTEST_END_UTC", "2026-09-04T15:00:00+00:00")
    )


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
        default_factory=lambda: _env_float("VOL_MIN_PERCENTILE", 0.40)
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
