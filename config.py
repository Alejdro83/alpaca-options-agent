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
        # Minimum days-to-expiration at entry — avoids gamma risk from
        # entering something that expires before the judging window even
        # gives it room to work.
        default_factory=lambda: _env_int("MIN_DTE", 7)
    )
    max_dte: int = field(
        default_factory=lambda: _env_int("MAX_DTE", 14)
    )
    short_leg_target_delta: float = field(
        # ~25-delta short strike is the standard "high-probability" credit
        # spread convention — roughly a 75% mechanical probability of
        # expiring worthless, before considering the underlying signal at all.
        default_factory=lambda: _env_float("SHORT_LEG_TARGET_DELTA", 0.25)
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
        # credit received (e.g. 2x credit received = stop out).
        default_factory=lambda: _env_float("STOP_LOSS_MULTIPLE", 2.0)
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
    supabase: SupabaseConfig = field(default_factory=SupabaseConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


config = AppConfig()
