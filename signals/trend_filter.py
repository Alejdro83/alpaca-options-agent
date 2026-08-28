"""
Trend Filter — Solo permite trades en dirección de la tendencia dominante.

Filtra señales que van contra la tendencia de mayor timeframe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TrendFilterResult:
    """Resultado del filtro de tendencia."""
    allowed: bool
    trend_direction: str  # "bullish", "bearish", "neutral"
    strength: float       # 0-1, fuerza de la tendencia
    reasoning: list[str]
    # Raw ADX(14) value, added 2026-08-28 for the iron condor regime split.
    # Real bug this fixed: `trend_direction` comes from EMA50 vs EMA200 --
    # "neutral" only fires on exact EMA equality, which essentially never
    # happens on real data (confirmed live: 0/20 real signals came back
    # neutral in one check, while 10/20 had ADX<25 "weak trend"). ADX is a
    # genuinely different axis (trend STRENGTH) from the EMA crossover
    # (trend DIRECTION) -- a ticker can be "bullish" by EMA with an ADX of
    # 15 (no real conviction behind it). Exposed raw here instead of only
    # folded into `strength`'s fixed 0.3-or-scaled encoding, so callers can
    # threshold on it directly against `adx_threshold`.
    adx: float = 0.0


class TrendFilter:
    """
    Filtra señales basándose en la tendencia de mayor timeframe.

    Reglas:
    1. Si tendencia es bullish → solo permitir LONG
    2. Si tendencia es bearish → solo permitir SHORT
    3. Si tendencia es neutral → permitir ambas direcciones

    Parameters
    ----------
    ema_fast : int
        EMA rápida para detectar tendencia (default 50)
    ema_slow : int
        EMA lenta para detectar tendencia (default 200)
    adx_threshold : float
        ADX mínimo para considerar tendencia fuerte (default 25)
    """

    def __init__(
        self,
        ema_fast: int = 50,
        ema_slow: int = 200,
        adx_threshold: float = 25.0,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.adx_threshold = adx_threshold

    def check(
        self,
        df: pd.DataFrame,
        direction: str,
    ) -> TrendFilterResult:
        """
        Verifica si una señal en la dirección dada está alineada con la tendencia.

        Parameters
        ----------
        df : DataFrame con columnas [close, high, low]
        direction : "long" o "short"

        Returns
        -------
        TrendFilterResult
        """
        if len(df) < self.ema_slow + 20:
            # Datos insuficientes, permitir trade. adx=0.0 here is
            # deliberate, not just "unknown": with no real trend read
            # possible, routing this candidate to an iron condor (no
            # directional conviction required) rather than a directional
            # vertical is the conservative choice, same "don't guess a
            # direction you can't confirm" spirit as elsewhere in this
            # project.
            return TrendFilterResult(
                allowed=True,
                trend_direction="neutral",
                strength=0.0,
                reasoning=["Insufficient data for trend filter"],
                adx=0.0,
            )

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Calculate EMAs
        ema_fast = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.ema_slow, adjust=False).mean()

        # Calculate ADX
        adx = self._compute_adx(high, low, close, period=14)
        last_adx = adx.iloc[-1] if not pd.isna(adx.iloc[-1]) else 0.0

        # Determine trend
        last_ema_fast = ema_fast.iloc[-1]
        last_ema_slow = ema_slow.iloc[-1]
        last_close = close.iloc[-1]

        reasoning = []

        if last_ema_fast > last_ema_slow:
            trend_direction = "bullish"
            reasoning.append(f"EMA{self.ema_fast} > EMA{self.ema_slow} (bullish)")
        elif last_ema_fast < last_ema_slow:
            trend_direction = "bearish"
            reasoning.append(f"EMA{self.ema_fast} < EMA{self.ema_slow} (bearish)")
        else:
            trend_direction = "neutral"
            reasoning.append("EMAs crossed (neutral)")

        # ADX strength
        if last_adx > self.adx_threshold:
            strength = min(last_adx / 50.0, 1.0)
            reasoning.append(f"ADX={last_adx:.1f} (strong trend)")
        else:
            strength = 0.3
            reasoning.append(f"ADX={last_adx:.1f} (weak trend)")

        # Check if direction matches trend
        allowed = True
        if trend_direction == "bullish" and direction == "short":
            allowed = False
            reasoning.append("BLOCKED: Short against bullish trend")
        elif trend_direction == "bearish" and direction == "long":
            allowed = False
            reasoning.append("BLOCKED: Long against bearish trend")

        return TrendFilterResult(
            allowed=allowed,
            trend_direction=trend_direction,
            strength=strength,
            reasoning=reasoning,
            adx=float(last_adx),
        )

    @staticmethod
    def _compute_adx(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> pd.Series:
        """Compute Average Directional Index."""
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        atr = tr.ewm(alpha=1.0 / period, min_periods=period).mean()
        plus_di = 100 * (
            plus_dm.ewm(alpha=1.0 / period, min_periods=period).mean() / atr
        )
        minus_di = 100 * (
            minus_dm.ewm(alpha=1.0 / period, min_periods=period).mean() / atr
        )

        di_sum = plus_di + minus_di
        dx = 100 * ((plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
        adx = dx.ewm(alpha=1.0 / period, min_periods=period).mean()
        return adx


def apply_trend_filter(
    signals: list[dict],
    daily_data: dict[str, pd.DataFrame],
    day: datetime,
    ema_fast: int = 50,
    ema_slow: int = 200,
) -> list[dict]:
    """
    Aplica trend filter a una lista de señales.

    Parameters
    ----------
    signals : lista de señales
    daily_data : datos diarios por símbolo
    day : día actual
    ema_fast, ema_slow : parámetros de EMA

    Returns
    -------
    Lista de señales filtradas
    """
    from datetime import datetime
    import pandas as pd

    tf = TrendFilter(ema_fast=ema_fast, ema_slow=ema_slow)
    filtered = []

    for sig in signals:
        sym = sig["ticker"]
        direction = sig.get("direction", "neutral")

        if direction == "neutral":
            filtered.append(sig)
            continue

        df = daily_data.get(sym)
        if df is None or len(df) < ema_slow + 20:
            filtered.append(sig)
            continue

        # Get data up to current day
        ts = pd.Timestamp(day)
        if df.index.tz is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        hist = df[df.index <= ts]

        result = tf.check(hist, direction)

        if result.allowed:
            sig["trend_filter"] = {
                "direction": result.trend_direction,
                "strength": result.strength,
                "reasoning": result.reasoning,
            }
            filtered.append(sig)
        else:
            logger.debug(
                "Trend filter blocked %s %s: %s",
                direction, sym, "; ".join(result.reasoning),
            )

    return filtered
