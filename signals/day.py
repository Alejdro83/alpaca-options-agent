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
from signals.indicators import compute_rsi, compute_macd, compute_ema, compute_atr
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


def generate_day_signals(symbols: list[str], client: AlpacaClient | None = None) -> list[Signal]:
    if client is None:
        client = AlpacaClient()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)  # 90 days to ensure regime detection has enough data

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
        df["timestamp"] = pd.to_datetime(df["timestamp"])
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
        ema9 = result.ema9
        ema21 = result.ema21

        if rsi.iloc[-1] is None or pd.isna(rsi.iloc[-1]):
            continue

        reasoning: list[str] = [f"Regime: {regime.regime.value} (ADX={regime.adx}, vol_ratio={regime.vol_ratio}x)"]
        score = 0.0

        last_close = close.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_macd_hist = macd_hist.iloc[-1] if not pd.isna(macd_hist.iloc[-1]) else 0
        last_ema9 = ema9.iloc[-1] if not pd.isna(ema9.iloc[-1]) else None
        last_ema21 = ema21.iloc[-1] if not pd.isna(ema21.iloc[-1]) else None
        last_atr = atr.iloc[-1] if not pd.isna(atr.iloc[-1]) else 0

        # --- Combined scoring with adaptive thresholds ---
        # RSI: use regime-adapted overbought/oversold levels
        if last_rsi < params.rsi_oversold:
            score += 1.0
            reasoning.append(f"RSI oversold ({last_rsi:.1f} < {params.rsi_oversold})")
        elif last_rsi > params.rsi_overbought:
            score -= 1.0
            reasoning.append(f"RSI overbought ({last_rsi:.1f} > {params.rsi_overbought})")
        
        # RSI z-score as secondary confirmation
        rsi_mean = rsi.rolling(14).mean().iloc[-1]
        rsi_std = rsi.rolling(14).std().iloc[-1]
        rsi_z = (last_rsi - rsi_mean) / rsi_std if rsi_std and rsi_std > 0 else 0
        score += rsi_z * 0.3
        if abs(rsi_z) > 1.5:
            reasoning.append(f"RSI z-score extreme ({rsi_z:.2f})")

        # MACD: z-score normalized
        macd_mean = macd_hist.rolling(14).mean().iloc[-1]
        macd_std = macd_hist.rolling(14).std().iloc[-1]
        macd_z = (last_macd_hist - macd_mean) / macd_std if macd_std and not pd.isna(macd_std) and macd_std > 0 else 0
        score += macd_z * 0.4
        if abs(macd_z) > 1.0:
            reasoning.append(f"MACD z-score {'bullish' if macd_z > 0 else 'bearish'} ({macd_z:.2f})")
        elif last_macd_hist > 0:
            score += 0.2
            reasoning.append("MACD positive")
        else:
            score -= 0.2
            reasoning.append("MACD negative")

        # EMA crossover: direction only (binary signal)
        if last_ema9 and last_ema21:
            if last_ema9 > last_ema21:
                score += 0.4
                reasoning.append("EMA9 > EMA21 bullish crossover")
            else:
                score -= 0.4
                reasoning.append("EMA9 < EMA21 bearish crossover")

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
                "ema9": round(last_ema9, 4) if last_ema9 else None,
                "ema21": round(last_ema21, 4) if last_ema21 else None,
                "atr": round(last_atr, 4),
                "stop_distance": result.stop_distance,
                "price": round(last_close, 4),
            },
            reasoning=reasoning,
        ))

    logger.info("Day signals: %d generated from %d symbols", len(signals), len(symbols))
    return signals
