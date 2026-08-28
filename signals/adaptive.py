"""Adaptive indicators that adjust parameters based on detected market regime.

Wraps the existing compute_* functions from indicators.py, passing
regime-appropriate parameters instead of hard-coded defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from signals.indicators import (
    compute_atr,
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_sma,
)
from signals.regime import Regime, RegimeDetector, RegimeResult


@dataclass(frozen=True)
class IndicatorParams:
    """Regime-specific indicator parameters."""
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    macd_fast: int
    macd_slow: int
    macd_signal: int
    atr_multiplier: float   # for stop-loss sizing
    bb_std: float           # Bollinger band width


# Pre-defined parameter sets per regime
_REGIME_PARAMS: dict[Regime, IndicatorParams] = {
    Regime.TRENDING: IndicatorParams(
        rsi_period=14,
        rsi_overbought=80,
        rsi_oversold=20,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        atr_multiplier=2.0,
        bb_std=2.0,
    ),
    Regime.RANGING: IndicatorParams(
        rsi_period=10,           # faster RSI to catch turns
        rsi_overbought=75,       # tighter bands → more signals
        rsi_oversold=25,
        macd_fast=8,             # faster MACD
        macd_slow=21,
        macd_signal=5,
        atr_multiplier=1.5,      # tighter stops in range
        bb_std=1.5,              # narrower bands
    ),
    Regime.VOLATILE_TRENDING: IndicatorParams(
        rsi_period=14,           # standard RSI
        rsi_overbought=75,
        rsi_oversold=25,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        atr_multiplier=3.0,      # wider stops in vol
        bb_std=2.5,              # wider bands
    ),
    Regime.VOLATILE_RANGING: IndicatorParams(
        rsi_period=21,           # slower RSI to filter noise
        rsi_overbought=80,
        rsi_oversold=20,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        atr_multiplier=3.0,
        bb_std=2.5,
    ),
}


class AdaptiveIndicators:
    """Compute regime-adapted technical indicators.

    Usage::

        ai = AdaptiveIndicators()
        result = ai.compute(df)   # df needs high/low/close columns
        print(result.regime)
        print(result.rsi.iloc[-1])
    """

    def __init__(self, detector: RegimeDetector | None = None) -> None:
        self.detector = detector or RegimeDetector()

    # ------------------------------------------------------------------
    def get_params(self, regime: Regime) -> IndicatorParams:
        return _REGIME_PARAMS[regime]

    # ------------------------------------------------------------------
    def compute(self, df: pd.DataFrame) -> AdaptiveResult:
        """Detect regime and compute all indicators with adapted params."""
        regime_info = self.detector.detect(df)
        params = self.get_params(regime_info.regime)
        return self._compute_with_params(df, regime_info, params)

    def compute_with_fixed(
        self, df: pd.DataFrame, regime: Regime
    ) -> AdaptiveResult:
        """Force a specific regime (for comparison/testing)."""
        params = self.get_params(regime)
        regime_info = RegimeResult(
            regime=regime,
            adx=0, vol_20d=0, vol_60d_avg=0, vol_ratio=0,
            is_high_vol=False, is_strong_trend=False,
        )
        return self._compute_with_params(df, regime_info, params)

    # ------------------------------------------------------------------
    def _compute_with_params(
        self,
        df: pd.DataFrame,
        regime_info: RegimeResult,
        params: IndicatorParams,
    ) -> AdaptiveResult:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        rsi = compute_rsi(close, period=params.rsi_period)
        macd_line, macd_signal, macd_hist = compute_macd(
            close, fast=params.macd_fast, slow=params.macd_slow,
            signal=params.macd_signal,
        )
        atr = compute_atr(high, low, close, period=14)
        bb_upper, bb_middle, bb_lower = compute_bollinger(
            close, period=20, std_dev=params.bb_std,
        )
        ema9 = compute_ema(close, 9)
        ema21 = compute_ema(close, 21)

        return AdaptiveResult(
            regime=regime_info,
            params=params,
            rsi=rsi,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            atr=atr,
            bb_upper=bb_upper,
            bb_middle=bb_middle,
            bb_lower=bb_lower,
            ema9=ema9,
            ema21=ema21,
        )


@dataclass
class AdaptiveResult:
    """All adapted indicators for a single symbol."""
    regime: RegimeResult
    params: IndicatorParams
    rsi: pd.Series
    macd_line: pd.Series
    macd_signal: pd.Series
    macd_hist: pd.Series
    atr: pd.Series
    bb_upper: pd.Series
    bb_middle: pd.Series
    bb_lower: pd.Series
    ema9: pd.Series
    ema21: pd.Series

    @property
    def stop_distance(self) -> float:
        """ATR-based stop distance for current regime."""
        last_atr = self.atr.iloc[-1]
        if pd.isna(last_atr):
            return 0.0
        return round(last_atr * self.params.atr_multiplier, 4)

    def summary(self) -> dict[str, Any]:
        """Latest values as a dict for logging / JSON."""
        def _v(s: pd.Series) -> float | None:
            v = s.iloc[-1]
            return round(float(v), 4) if not pd.isna(v) else None

        return {
            "regime": self.regime.regime.value,
            "adx": self.regime.adx,
            "vol_ratio": self.regime.vol_ratio,
            "rsi_period": self.params.rsi_period,
            "rsi_overbought": self.params.rsi_overbought,
            "rsi_oversold": self.params.rsi_oversold,
            "macd_fast": self.params.macd_fast,
            "macd_slow": self.params.macd_slow,
            "rsi": _v(self.rsi),
            "macd_hist": _v(self.macd_hist),
            "atr": _v(self.atr),
            "stop_distance": self.stop_distance,
            "bb_upper": _v(self.bb_upper),
            "bb_lower": _v(self.bb_lower),
            "ema9": _v(self.ema9),
            "ema21": _v(self.ema21),
        }
