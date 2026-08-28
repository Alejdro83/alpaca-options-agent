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
from signals.indicators import compute_rsi, compute_macd, compute_bollinger, compute_atr, compute_sma
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


def generate_swing_signals(symbols: list[str], client: AlpacaClient | None = None) -> list[Signal]:
    if client is None:
        client = AlpacaClient()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)  # 90 days of daily data for swing signals

    signals: list[Signal] = []
    adaptive = AdaptiveIndicators()

    for symbol in symbols:
        try:
            # Use daily bars with IEX feed (available on paper trading)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame(amount=1, unit=TimeFrameUnit.Day),
                start=start,
                end=end,
                limit=120,
                feed=DataFeed.IEX
            )
            raw = client._data.get_stock_bars(req)
            if symbol not in raw.data or len(raw.data[symbol]) < 60:
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
        atr = result.atr
        bb_upper = result.bb_upper
        bb_middle = result.bb_middle
        bb_lower = result.bb_lower

        if pd.isna(rsi.iloc[-1]):
            continue

        reasoning: list[str] = [f"Regime: {regime.regime.value} (ADX={regime.adx}, vol_ratio={regime.vol_ratio}x)"]
        score = 0.0

        last_close = close.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_macd_hist = macd_hist.iloc[-1] if not pd.isna(macd_hist.iloc[-1]) else 0
        last_bb_upper = bb_upper.iloc[-1] if not pd.isna(bb_upper.iloc[-1]) else None
        last_bb_lower = bb_lower.iloc[-1] if not pd.isna(bb_lower.iloc[-1]) else None
        last_atr = atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 0

        sma50 = compute_sma(close, 50)
        sma200 = compute_sma(close, 200)
        last_sma50 = sma50.iloc[-1] if not pd.isna(sma50.iloc[-1]) else None
        last_sma200 = sma200.iloc[-1] if not pd.isna(sma200.iloc[-1]) else None

        # --- Z-score normalized scoring with adaptive thresholds ---
        rsi_mean = rsi.rolling(20).mean().iloc[-1]
        rsi_std = rsi.rolling(20).std().iloc[-1]
        rsi_z = (last_rsi - rsi_mean) / rsi_std if rsi_std and rsi_std > 0 else 0
        score += rsi_z * 0.4
        if last_rsi < params.rsi_oversold + 10:
            reasoning.append(f"RSI approaching oversold ({last_rsi:.1f}, z={rsi_z:.2f}, threshold={params.rsi_oversold})")
        elif last_rsi > params.rsi_overbought - 10:
            reasoning.append(f"RSI approaching overbought ({last_rsi:.1f}, z={rsi_z:.2f}, threshold={params.rsi_overbought})")

        macd_mean = macd_hist.rolling(20).mean().iloc[-1]
        macd_std = macd_hist.rolling(20).std().iloc[-1]
        macd_z = (last_macd_hist - macd_mean) / macd_std if macd_std and not pd.isna(macd_std) and macd_std > 0 else 0
        score += macd_z * 0.3
        if abs(macd_z) > 0.5:
            reasoning.append(f"MACD z-score {'bullish' if macd_z > 0 else 'bearish'} ({macd_z:.2f})")

        if last_bb_lower and last_close <= last_bb_lower:
            score += 0.5
            reasoning.append(f"Price at lower Bollinger Band (bb_std={params.bb_std})")
        elif last_bb_upper and last_close >= last_bb_upper:
            score -= 0.5
            reasoning.append(f"Price at upper Bollinger Band (bb_std={params.bb_std})")

        if last_sma50 and last_sma200:
            if last_sma50 > last_sma200:
                score += 0.4
                reasoning.append("Golden cross (SMA50 > SMA200)")
            else:
                score -= 0.4
                reasoning.append("Death cross (SMA50 < SMA200)")

        if last_sma50:
            if last_close > last_sma50:
                score += 0.2
                reasoning.append("Price above SMA50")
            else:
                score -= 0.2
                reasoning.append("Price below SMA50")

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
                "bb_std": params.bb_std,
                "bb_upper": round(last_bb_upper, 4) if last_bb_upper else None,
                "bb_lower": round(last_bb_lower, 4) if last_bb_lower else None,
                "sma50": round(last_sma50, 4) if last_sma50 else None,
                "sma200": round(last_sma200, 4) if last_sma200 else None,
                "atr": round(last_atr, 4),
                "stop_distance": result.stop_distance,
                "price": round(last_close, 4),
            },
            reasoning=reasoning,
        ))

    logger.info("Swing signals: %d generated from %d symbols", len(signals), len(symbols))
    return signals
