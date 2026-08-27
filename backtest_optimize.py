"""Express walk-forward parameter check for the credit-spread strategy --
run once, 2026-08-27, before the hackathon's first live trading day.

Scope, stated honestly up front (same convention as README.md's "Honest
scope notes"):
- Reuses the REAL signal-generation logic (a frozen copy of
  signals.swing.generate_swing_signals' per-symbol scoring -- that function
  fetches live data internally from datetime.now(), so it can't produce a
  signal as-of an arbitrary past date; `_signal_from_df` below is a
  byte-for-byte port of its scoring math onto an already-loaded historical
  df. Resync it if generate_swing_signals' scoring ever changes -- same
  "flat copy, no auto-sync" convention as Gaussly's dist/modules/*.R), the
  REAL TrendFilter, and the REAL volatility-percentile filter, against REAL
  historical daily bars from Alpaca (via trading_bot/backtest/data.py's
  HistoricalDataLoader, reused unmodified).
- Spread economics (entry credit, daily mark, exit timing) are SIMULATED
  with Black-Scholes theoretical pricing using the same realized-vol proxy
  the live bot uses for delta -- NOT real historical option chain prices
  (that data is paid/unavailable; same limitation black_scholes.py and
  README.md already document for the live bot). This compares OUR OWN
  parameter choices against each other on one consistent, honest proxy; it
  is not a market-realistic options backtest.
- Fixed 12-symbol liquid basket, not the full ~500-ticker screen replayed
  at every historical date (that many fetches across a full walk-forward
  doesn't fit an "express, before market open" budget). Does not simulate
  the max-concurrent-spreads portfolio cap or the LLM selection step --
  isolates the spread-parameter question (delta / DTE / profit target)
  from the portfolio-sizing and LLM-selection questions, which are treated
  as already decided.
"""
from __future__ import annotations

import itertools
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import pandas as pd

sys.path.insert(0, "/home/lab-master/trading_bot")
sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

from backtest.data import HistoricalDataLoader  # trading_bot's, unmodified
from signals.adaptive import AdaptiveIndicators
from signals.indicators import compute_sma, compute_atr
from signals.swing import Signal
from signals.trend_filter import TrendFilter
import black_scholes

BASKET = ["SPY", "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "JPM", "JNJ", "XOM", "HD", "CMCSA"]
LOOKBACK_DAYS = 760           # ~2 calendar years
WARMUP_TRADING_DAYS = 210     # room for EMA200 (trend filter) + 90-day swing window
VOL_LOOKBACK = 20
VOL_MIN_PERCENTILE = 0.40
RISK_FREE_RATE = 0.045
MAX_LOSS_PER_SPREAD_PCT = 0.02
NOTIONAL_EQUITY = 100_000.0
SPREAD_WIDTH = 5.0
STOP_LOSS_MULTIPLE = 2.0      # held fixed -- already validated in the 2026-08-26 pass, not swept here

PARAM_GRID = {
    "target_delta": [0.15, 0.17, 0.20, 0.25],
    "dte_range": [(7, 14), (10, 21), (14, 30)],
    "profit_target_pct": [0.40, 0.50, 0.60],
}
CURRENT_DEFAULTS = (0.17, (10, 21), 0.50)


def _signal_from_df(symbol: str, df: pd.DataFrame, adaptive: AdaptiveIndicators) -> Signal | None:
    if len(df) < 60:
        return None
    try:
        result = adaptive.compute(df)
    except Exception:
        return None
    close = df["close"]
    rsi = result.rsi
    macd_hist = result.macd_hist
    bb_upper = result.bb_upper
    bb_lower = result.bb_lower
    if pd.isna(rsi.iloc[-1]):
        return None
    score = 0.0
    last_close = close.iloc[-1]
    last_rsi = rsi.iloc[-1]
    last_macd_hist = macd_hist.iloc[-1] if not pd.isna(macd_hist.iloc[-1]) else 0
    last_bb_upper = bb_upper.iloc[-1] if not pd.isna(bb_upper.iloc[-1]) else None
    last_bb_lower = bb_lower.iloc[-1] if not pd.isna(bb_lower.iloc[-1]) else None
    sma50 = compute_sma(close, 50)
    sma200 = compute_sma(close, 200)
    last_sma50 = sma50.iloc[-1] if not pd.isna(sma50.iloc[-1]) else None
    last_sma200 = sma200.iloc[-1] if not pd.isna(sma200.iloc[-1]) else None
    rsi_mean = rsi.rolling(20).mean().iloc[-1]
    rsi_std = rsi.rolling(20).std().iloc[-1]
    rsi_z = (last_rsi - rsi_mean) / rsi_std if rsi_std and rsi_std > 0 else 0
    score += rsi_z * 0.4
    macd_mean = macd_hist.rolling(20).mean().iloc[-1]
    macd_std = macd_hist.rolling(20).std().iloc[-1]
    macd_z = (last_macd_hist - macd_mean) / macd_std if macd_std and not pd.isna(macd_std) and macd_std > 0 else 0
    score += macd_z * 0.3
    if last_bb_lower and last_close <= last_bb_lower:
        score += 0.5
    elif last_bb_upper and last_close >= last_bb_upper:
        score -= 0.5
    if last_sma50 and last_sma200:
        score += 0.4 if last_sma50 > last_sma200 else -0.4
    if last_sma50:
        score += 0.2 if last_close > last_sma50 else -0.2
    direction = "long" if score > 0 else "short" if score < 0 else "neutral"
    strength = min(abs(score) / 2.0, 1.0)
    return Signal(ticker=symbol, direction=direction, strength=round(strength, 3), indicators={}, reasoning=[])


def _passes_volatility_filter(bars_df: pd.DataFrame) -> bool:
    atr = compute_atr(bars_df["high"], bars_df["low"], bars_df["close"], period=VOL_LOOKBACK)
    atr_pct = (atr / bars_df["close"]).dropna()
    if len(atr_pct) < VOL_LOOKBACK * 2:
        return True
    percentile = atr_pct.rank(pct=True).iloc[-1]
    return percentile >= VOL_MIN_PERCENTILE


def bs_price(*, spot, strike, dte_days, volatility, option_type, r=RISK_FREE_RATE) -> float:
    if dte_days <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if option_type == "call" else (strike - spot))
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (r + 0.5 * volatility**2) * t) / (volatility * math.sqrt(t))
    d2 = d1 - volatility * math.sqrt(t)
    N = black_scholes._norm_cdf
    if option_type == "call":
        return spot * N(d1) - strike * math.exp(-r * t) * N(d2)
    return strike * math.exp(-r * t) * N(-d2) - spot * N(-d1)


def strike_for_delta(*, spot, target_delta, dte_days, volatility, option_type, r=RISK_FREE_RATE) -> float:
    """Closed-form inverse of bs_delta. Real trading picks from actually-
    listed strikes (discrete increments); with no historical chain
    available, this rounds to the nearest $1 as an honest proxy."""
    t = dte_days / 365.0
    d1 = NormalDist().inv_cdf(1 - target_delta) if option_type == "put" else NormalDist().inv_cdf(target_delta)
    k = spot / math.exp(d1 * volatility * math.sqrt(t) - (r + 0.5 * volatility**2) * t)
    return round(k)


@dataclass
class SimTrade:
    symbol: str
    direction: str
    entry_date: date
    exit_date: date
    credit: float
    pnl: float
    exit_reason: str


def simulate_trade(df, entry_idx, direction, spot_entry, realized_vol, target_delta, dte_range, profit_target_pct):
    dte0 = (dte_range[0] + dte_range[1]) // 2
    option_type = "put" if direction == "long" else "call"
    short_strike = strike_for_delta(spot=spot_entry, target_delta=target_delta, dte_days=dte0,
                                     volatility=realized_vol, option_type=option_type)
    long_strike = short_strike - SPREAD_WIDTH if direction == "long" else short_strike + SPREAD_WIDTH

    entry_short = bs_price(spot=spot_entry, strike=short_strike, dte_days=dte0, volatility=realized_vol, option_type=option_type)
    entry_long = bs_price(spot=spot_entry, strike=long_strike, dte_days=dte0, volatility=realized_vol, option_type=option_type)
    credit = entry_short - entry_long
    if credit <= 0:
        return None
    max_loss = SPREAD_WIDTH - credit
    if max_loss * 100 > NOTIONAL_EQUITY * MAX_LOSS_PER_SPREAD_PCT:
        return None  # mirrors risk_gate.check_new_spread's per-spread cap

    entry_date = df.index[entry_idx].date()
    last_idx = len(df) - 1
    for days_forward in range(1, dte0 + 1):
        idx = entry_idx + days_forward
        if idx > last_idx:
            idx = last_idx
            forced_end = True
        else:
            forced_end = False
        spot_t = float(df["close"].iloc[idx])
        dte_left = max(dte0 - days_forward, 0.5)
        short_px = bs_price(spot=spot_t, strike=short_strike, dte_days=dte_left, volatility=realized_vol, option_type=option_type)
        long_px = bs_price(spot=spot_t, strike=long_strike, dte_days=dte_left, volatility=realized_vol, option_type=option_type)
        mark = short_px - long_px
        profit_captured = 1 - (mark / credit) if credit else 0
        row_date = df.index[idx].date()
        if forced_end:
            return SimTrade("", direction, entry_date, row_date, credit, (credit - mark) * 100, "data_end")
        if dte_left <= 1:
            return SimTrade("", direction, entry_date, row_date, credit, (credit - mark) * 100, "expiry")
        if profit_captured >= profit_target_pct:
            return SimTrade("", direction, entry_date, row_date, credit, (credit - mark) * 100, "profit_target")
        if mark >= credit * STOP_LOSS_MULTIPLE:
            return SimTrade("", direction, entry_date, row_date, credit, (credit - mark) * 100, "stop")
    return None


def main():
    print("Loading historical data (Alpaca IEX, via trading_bot's HistoricalDataLoader)...")
    loader = HistoricalDataLoader()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_DAYS)
    data = loader.fetch_all(BASKET, start, end, lookback_days=0)
    available = {s: df.sort_index() for s, df in data.items() if len(df) > WARMUP_TRADING_DAYS + 30}
    print(f"Data loaded for {len(available)}/{len(BASKET)} symbols: {sorted(available)}\n")

    adaptive = AdaptiveIndicators()
    trend_filter = TrendFilter()

    events = []
    for symbol, df in available.items():
        cooldown_until = -1
        for i in range(WARMUP_TRADING_DAYS, len(df) - 3):
            if i <= cooldown_until:
                continue
            window = df.iloc[max(0, i - 89): i + 1]
            sig = _signal_from_df(symbol, window, adaptive)
            if sig is None or sig.direction == "neutral":
                continue
            trend_window = df.iloc[: i + 1]
            try:
                trend_result = trend_filter.check(trend_window, sig.direction)
                if not trend_result.allowed:
                    continue
            except Exception:
                pass
            if not _passes_volatility_filter(trend_window):
                continue
            realized_vol = black_scholes.realized_vol_from_bars(trend_window)
            if realized_vol <= 0:
                continue
            spot = float(df["close"].iloc[i])
            events.append((symbol, i, sig.direction, spot, realized_vol, df))
            cooldown_until = i + 25

    print(f"{len(events)} entry events found across the basket\n")
    if not events:
        print("No entry events -- nothing to optimize on. Aborting.")
        return

    combos = list(itertools.product(PARAM_GRID["target_delta"], PARAM_GRID["dte_range"], PARAM_GRID["profit_target_pct"]))
    results = []
    for target_delta, dte_range, profit_target_pct in combos:
        trades = []
        for symbol, i, direction, spot, rvol, df in events:
            t = simulate_trade(df, i, direction, spot, rvol, target_delta, dte_range, profit_target_pct)
            if t is not None:
                trades.append(t)
        if not trades:
            continue
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]
        results.append({
            "target_delta": target_delta,
            "dte_range": dte_range,
            "profit_target_pct": profit_target_pct,
            "n_trades": len(trades),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(statistics.mean(pnls), 2),
            "median_pnl": round(statistics.median(pnls), 2),
            "win_rate": round(len(wins) / len(pnls), 3),
        })

    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    print(f"{'delta':>6} {'dte':>10} {'PT':>5} {'n':>4} {'total_pnl':>12} {'avg_pnl':>10} {'win%':>6}")
    for r in results:
        print(f"{r['target_delta']:>6} {str(r['dte_range']):>10} {r['profit_target_pct']:>5} "
              f"{r['n_trades']:>4} {r['total_pnl']:>12} {r['avg_pnl']:>10} {r['win_rate']*100:>5.1f}%")

    current = next((r for r in results if (r["target_delta"], r["dte_range"], r["profit_target_pct"]) == CURRENT_DEFAULTS), None)
    print(f"\nCurrent production defaults {CURRENT_DEFAULTS}: {current}")
    print(f"Best by total synthetic P&L: {results[0] if results else None}")
    print(f"Best by win rate (min 5 trades): {max((r for r in results if r['n_trades'] >= 5), key=lambda r: r['win_rate'], default=None)}")


if __name__ == "__main__":
    main()
