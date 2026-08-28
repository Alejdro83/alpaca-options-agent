"""Volatility regime detection for adaptive indicator tuning.

Classifies market state into four regimes based on volatility level
and trend strength (ADX):
  - TRENDING:           low vol  + strong trend
  - RANGING:            low vol  + weak trend
  - VOLATILE_TRENDING:  high vol + strong trend
  - VOLATILE_RANGING:   high vol + weak trend
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class Regime(Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE_TRENDING = "volatile_trending"
    VOLATILE_RANGING = "volatile_ranging"


@dataclass
class RegimeResult:
    regime: Regime
    adx: float
    vol_20d: float
    vol_60d_avg: float
    vol_ratio: float  # 20d / 60d avg
    is_high_vol: bool
    is_strong_trend: bool

    def __str__(self) -> str:
        return (
            f"Regime={self.regime.value} | ADX={self.adx:.1f} | "
            f"Vol20d={self.vol_20d:.4f} Vol60dAvg={self.vol_60d_avg:.4f} "
            f"Ratio={self.vol_ratio:.2f}x | "
            f"HighVol={self.is_high_vol} StrongTrend={self.is_strong_trend}"
        )


class RegimeDetector:
    """Detect market regime from OHLCV data.

    Parameters
    ----------
    vol_window : int
        Short rolling window for current volatility (default 20).
    vol_hist_window : int
        Long window for historical volatility baseline (default 60).
    vol_threshold : float
        Ratio above which volatility is considered high (default 1.5).
    adx_period : int
        Period for ADX calculation (default 14).
    adx_threshold : float
        ADX above which trend is considered strong (default 25).
    """

    def __init__(
        self,
        vol_window: int = 20,
        vol_hist_window: int = 60,
        vol_threshold: float = 1.5,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
    ) -> None:
        self.vol_window = vol_window
        self.vol_hist_window = vol_hist_window
        self.vol_threshold = vol_threshold
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def detect(self, df: pd.DataFrame) -> RegimeResult:
        """Classify the current market regime from an OHLCV DataFrame.

        Expects columns: high, low, close.  Needs at least
        ``vol_hist_window + adx_period`` rows.
        """
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # --- Volatility ---
        returns = close.pct_change()
        vol_20d = returns.rolling(self.vol_window).std().iloc[-1]
        vol_60d = returns.rolling(self.vol_hist_window).std()
        vol_60d_avg = vol_60d.iloc[-self.vol_hist_window :].mean()
        vol_ratio = vol_20d / vol_60d_avg if vol_60d_avg > 0 else 1.0
        is_high_vol = vol_ratio > self.vol_threshold

        # --- ADX (Average Directional Index) ---
        adx = self._compute_adx(high, low, close, self.adx_period)
        last_adx = adx.iloc[-1] if not pd.isna(adx.iloc[-1]) else 0.0
        is_strong_trend = last_adx > self.adx_threshold

        # --- Classify ---
        if is_high_vol and is_strong_trend:
            regime = Regime.VOLATILE_TRENDING
        elif is_high_vol and not is_strong_trend:
            regime = Regime.VOLATILE_RANGING
        elif not is_high_vol and is_strong_trend:
            regime = Regime.TRENDING
        else:
            regime = Regime.RANGING

        return RegimeResult(
            regime=regime,
            adx=round(last_adx, 2),
            vol_20d=round(vol_20d, 6),
            vol_60d_avg=round(vol_60d_avg, 6),
            vol_ratio=round(vol_ratio, 3),
            is_high_vol=is_high_vol,
            is_strong_trend=is_strong_trend,
        )

    # ------------------------------------------------------------------
    # ADX internals (pure pandas/numpy — no ta-lib needed)
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_adx(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> pd.Series:
        """Compute the Average Directional Index."""
        # True Range
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Directional Movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        # Smoothed averages (Wilder's smoothing ≈ EMA with alpha=1/period)
        atr = tr.ewm(alpha=1.0 / period, min_periods=period).mean()
        plus_di = 100 * (
            plus_dm.ewm(alpha=1.0 / period, min_periods=period).mean() / atr
        )
        minus_di = 100 * (
            minus_dm.ewm(alpha=1.0 / period, min_periods=period).mean() / atr
        )

        # DX and ADX
        di_sum = plus_di + minus_di
        dx = 100 * ((plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
        adx = dx.ewm(alpha=1.0 / period, min_periods=period).mean()
        return adx
