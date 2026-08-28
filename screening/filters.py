from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from alpaca_client import AlpacaClient
from config import config

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    symbol: str
    price: float
    volume: int
    spread_pct: float
    snapshot: dict[str, Any]


def _passes_filters(
    symbol: str,
    snapshot: dict[str, Any],
    quote: dict[str, Any],
    sf: Any,
) -> Candidate | None:
    price = snapshot.get("latest_trade_price") or snapshot.get("daily_close")
    if price is None:
        return None
    price = float(price)

    if price < sf.min_price or price > sf.max_price:
        return None

    volume = snapshot.get("daily_volume")
    if volume is None or volume < sf.min_avg_volume:
        return None

    spread_pct = quote.get("spread_pct", 0.0)
    if spread_pct > sf.max_spread_pct:
        return None

    # Market-cap range filter
    market_cap = snapshot.get("market_cap")
    if market_cap is not None:
        market_cap = float(market_cap)
        if market_cap < sf.min_market_cap or market_cap > sf.max_market_cap:
            logger.debug(
                "%s rejected: market_cap %.0f outside [%.0f, %.0f]",
                symbol, market_cap, sf.min_market_cap, sf.max_market_cap,
            )
            return None
    else:
        logger.debug("%s: market_cap unavailable in snapshot, skipping filter", symbol)

    # Min ATR % filter (atr field expected in snapshot if available)
    atr_value = snapshot.get("atr") or snapshot.get("daily_atr")
    if atr_value is not None and price > 0:
        atr_pct = float(atr_value) / price * 100
        if atr_pct < sf.min_atr_pct:
            logger.debug(
                "%s rejected: ATR%% %.2f < min %.2f",
                symbol, atr_pct, sf.min_atr_pct,
            )
            return None
    else:
        logger.debug("%s: ATR data unavailable in snapshot, skipping filter", symbol)

    return Candidate(
        symbol=symbol,
        price=price,
        volume=volume,
        spread_pct=spread_pct,
        snapshot=snapshot,
    )


def filter_universe(tickers: list[str], client: AlpacaClient | None = None) -> list[Candidate]:
    if client is None:
        client = AlpacaClient()

    sf = config.screening
    candidates: list[Candidate] = []

    batch_size = 100
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        try:
            snapshots = client.get_snapshots(batch)
        except Exception as exc:
            logger.error("Snapshot batch failed: %s", exc)
            continue

        for symbol, snap in snapshots.items():
            try:
                quote = client.get_latest_quote(symbol)
            except Exception:
                continue

            c = _passes_filters(symbol, snap, quote, sf)
            if c is not None:
                candidates.append(c)

    logger.info("Screening: %d / %d tickers passed filters", len(candidates), len(tickers))
    return candidates
