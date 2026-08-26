"""Self-computed Black-Scholes delta — added after discovering, against the
real account, that Alpaca's free/paper options data does NOT include
broker-supplied Greeks: real-time OPRA data (which `get_option_snapshot`
needs to return a `greeks` field) requires a paid "Algo Trader Plus"
subscription; the free `indicative` feed returns only a bid/ask quote, no
Greeks at all (confirmed directly: `feed=opra` on this account 403s with
"OPRA agreement is not signed" / needs the paid plan; `feed=indicative`
succeeds but its snapshot has no `greeks` key whatsoever).

Rather than degrade the strategy to "pick the strike closest to some fixed
dollar distance," this computes a real Black-Scholes delta using the same
realized-volatility estimate the volatility filter already computes
(bot._passes_volatility_filter's ATR-based approach) as the volatility
input — an honest proxy for implied vol, not the real thing, and labeled
as such everywhere it's surfaced (see ONE_PAGER.md). This is standard
practice when a broker's own IV/Greeks aren't available: closed-form
Black-Scholes delta from spot/strike/DTE/vol is well-established, not a
novel approximation.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    *,
    spot: float,
    strike: float,
    dte_days: float,
    volatility: float,
    option_type: str,
    risk_free_rate: float = 0.045,
) -> float:
    """`volatility` is annualized (e.g. 0.25 for 25%). `dte_days` is
    calendar days to expiration; converted to years internally. Returns
    delta in [-1, 1] (negative for puts) — spread_builder.py takes abs()
    where it needs magnitude for strike selection, matching how it would
    have used a broker-supplied delta.
    """
    if dte_days <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    t = dte_days / 365.0
    d1 = (
        math.log(spot / strike) + (risk_free_rate + 0.5 * volatility**2) * t
    ) / (volatility * math.sqrt(t))

    if option_type == "call":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def annualized_realized_vol(daily_returns_std: float) -> float:
    """Annualizes a daily log-return standard deviation (252 trading days)."""
    return daily_returns_std * math.sqrt(252)


def realized_vol_from_bars(bars_df: pd.DataFrame, window: int = 20) -> float:
    """Annualized realized volatility from the trailing `window` days of
    daily closes — the standard deviation-of-log-returns approach, distinct
    from the ATR%-percentile calculation `bot._passes_volatility_filter`
    uses (that one ranks *whether* vol is elevated; this one estimates the
    actual vol *level* to feed into bs_delta as the IV proxy). Both reuse
    the same underlying bars fetch, no extra API calls.
    """
    closes = bars_df["close"].tail(window + 1)
    log_returns = np.log(closes / closes.shift(1)).dropna()
    if len(log_returns) < 2:
        return 0.20  # a reasonable fallback rather than a crash on thin history
    return annualized_realized_vol(float(log_returns.std()))
