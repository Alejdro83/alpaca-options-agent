from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from alpaca_client import AlpacaClient
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest
from alpaca.data.enums import DataFeed
from signals.indicators import compute_rsi, compute_macd, compute_sma
from signals.adaptive import AdaptiveIndicators
from signals.regime import Regime

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    ticker: str
    direction: str
    strength: float
    indicators: dict[str, Any]
    reasoning: list[str] = field(default_factory=list)


def generate_position_signals(symbols: list[str], client: AlpacaClient | None = None) -> list[Signal]:
    if client is None:
        client = AlpacaClient()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365)  # 1 year of weekly data for position signals

    signals: list[Signal] = []
    adaptive = AdaptiveIndicators()

    for symbol in symbols:
        try:
            # Use weekly bars with IEX feed (available on paper trading)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Week,
                start=start,
                end=end,
                limit=60,
                feed=DataFeed.IEX
            )
            raw = client._data.get_stock_bars(req)
            if symbol not in raw.data or len(raw.data[symbol]) < 30:
                continue
            bars = [bar.__dict__ for bar in raw.data[symbol]]
        except Exception as exc:
            logger.debug("Failed to get bars for %s: %s", symbol, exc)
            continue

        df = pd.DataFrame(bars)
        df = df.sort_values("timestamp").reset_index(drop=True)

        # --- Regime detection + adaptive indicators ---
        try:
            result = adaptive.compute(df)
        except Exception as exc:
            logger.debug("Adaptive compute failed for %s: %s", symbol, exc)
            continue

        regime = result.regime
        params = result.params

        close = df["close"]
        rsi = result.rsi
        macd_hist = result.macd_hist

        if pd.isna(rsi.iloc[-1]):
            continue

        reasoning: list[str] = [f"Regime: {regime.regime.value} (ADX={regime.adx}, vol_ratio={regime.vol_ratio}x)"]
        score = 0.0

        last_close = close.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_macd_hist = macd_hist.iloc[-1] if not pd.isna(macd_hist.iloc[-1]) else 0

        sma10 = compute_sma(close, 10)
        sma30 = compute_sma(close, 30)
        last_sma10 = sma10.iloc[-1] if not pd.isna(sma10.iloc[-1]) else None
        last_sma30 = sma30.iloc[-1] if not pd.isna(sma30.iloc[-1]) else None

        # --- Z-score normalized scoring with adaptive thresholds ---
        rsi_mean = rsi.rolling(10).mean().iloc[-1]
        rsi_std = rsi.rolling(10).std().iloc[-1]
        rsi_z = (last_rsi - rsi_mean) / rsi_std if rsi_std and rsi_std > 0 else 0
        score += rsi_z * 0.3
        if last_rsi < params.rsi_oversold + 15:
            reasoning.append(f"RSI relatively low ({last_rsi:.1f}, z={rsi_z:.2f}, threshold={params.rsi_oversold})")
        elif last_rsi > params.rsi_overbought - 15:
            reasoning.append(f"RSI relatively high ({last_rsi:.1f}, z={rsi_z:.2f}, threshold={params.rsi_overbought})")

        macd_mean = macd_hist.rolling(10).mean().iloc[-1]
        macd_std = macd_hist.rolling(10).std().iloc[-1]
        macd_z = (last_macd_hist - macd_mean) / macd_std if macd_std and not pd.isna(macd_std) and macd_std > 0 else 0
        score += macd_z * 0.3
        if abs(macd_z) > 0.5:
            reasoning.append(f"MACD z-score {'bullish' if macd_z > 0 else 'bearish'} ({macd_z:.2f})")

        if last_sma10 and last_sma30:
            if last_sma10 > last_sma30:
                score += 0.4
                reasoning.append("SMA10 > SMA30 weekly uptrend")
            else:
                score -= 0.4
                reasoning.append("SMA10 < SMA30 weekly downtrend")

        if last_sma10:
            if last_close > last_sma10:
                score += 0.2
                reasoning.append("Price above SMA10")
            else:
                score -= 0.2
                reasoning.append("Price below SMA10")

        direction = "long" if score > 0 else "short" if score < 0 else "neutral"
        strength = min(abs(score) / 2.0, 1.0)

        signals.append(Signal(
            ticker=symbol,
            direction=direction,
            strength=round(strength, 3),
            indicators={
                "regime": regime.regime.value,
                "adx": regime.adx,
                "vol_ratio": regime.vol_ratio,
                "rsi_period": params.rsi_period,
                "rsi_overbought": params.rsi_overbought,
                "rsi_oversold": params.rsi_oversold,
                "rsi": round(last_rsi, 2),
                "macd_fast": params.macd_fast,
                "macd_slow": params.macd_slow,
                "macd_hist": round(last_macd_hist, 4),
                "stop_distance": result.stop_distance,
                "atr_multiplier": params.atr_multiplier,
                "sma10": round(last_sma10, 4) if last_sma10 else None,
                "sma30": round(last_sma30, 4) if last_sma30 else None,
                "price": round(last_close, 4),
            },
            reasoning=reasoning,
        ))

    logger.info("Position signals: %d generated from %d symbols", len(signals), len(symbols))
    return signals
