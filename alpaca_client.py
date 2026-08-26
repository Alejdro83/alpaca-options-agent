from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetPortfolioHistoryRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

from config import config

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
RETRY_BACKOFF = 2.0


def _retry(fn, *args, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            if "rate" in msg or "429" in msg:
                wait = RETRY_BACKOFF ** attempt
                logger.warning("Rate limited, retry %d/%d in %.1fs", attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Max retries exceeded")


class AlpacaClient:
    def __init__(self) -> None:
        cfg = config.alpaca
        self._trading = TradingClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
            paper=True,
        )
        self._data = StockHistoricalDataClient(
            api_key=cfg.api_key,
            secret_key=cfg.secret_key,
        )

    def get_account(self) -> dict[str, Any]:
        acct = _retry(self._trading.get_account)
        return {
            "equity": float(acct.equity),
            "last_equity": float(acct.last_equity),
            "cash": float(acct.cash),
            "buying_power": float(acct.buying_power),
            "portfolio_value": float(acct.portfolio_value),
            "status": acct.status.value if hasattr(acct.status, "value") else str(acct.status),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        positions = _retry(self._trading.get_all_positions)
        return [
            {
                "symbol": p.symbol,
                "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "market_value": float(p.market_value),
                "created_at": str(p.created_at) if getattr(p, "created_at", None) else None,
            }
            for p in positions
        ]

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Force-close an entire position by symbol."""
        order = _retry(self._trading.close_position, symbol)
        return {
            "symbol": symbol,
            "order_id": str(order.id) if order else None,
            "status": "close_submitted",
        }

    def get_orders(self, status: str = "open") -> list[dict[str, Any]]:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        req = GetOrdersRequest(
            status=QueryOrderStatus(status) if status != "all" else None
        )
        orders = _retry(self._trading.get_orders, filter=req)
        return [
            {
                "id": str(o.id),
                # Multi-leg (order_class=mleg) orders — this project's ONLY
                # order type — carry no top-level symbol/side; each leg has
                # its own, under `o.legs`. Vendored `trading_bot/` never hit
                # this (equities-only, always single-leg), so this diverges
                # from that original on purpose rather than crashing.
                "symbol": o.symbol,
                "side": o.side.value if o.side else None,
                "legs": [
                    {"symbol": leg.symbol, "side": leg.side.value if leg.side else None}
                    for leg in (o.legs or [])
                ] if getattr(o, "legs", None) else None,
                "qty": str(o.qty),
                "type": o.type.value if hasattr(o.type, "value") else str(o.type),
                "stop_price": float(o.stop_price) if getattr(o, "stop_price", None) else None,
                "status": o.status.value if hasattr(o.status, "value") else str(o.status),
                "submitted_at": str(o.submitted_at),
            }
            for o in orders
        ]

    def place_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        time_in_force: str = "day",
    ) -> dict[str, Any]:
        """Submit an order.

        - `stop_loss_price` only: entry placed as OTO (one-triggers-other) --
          Alpaca submits the protective stop once the entry fills.
        - `stop_loss_price` + `take_profit_price`: entry placed as a full
          BRACKET order -- both legs rest once the entry fills, linked OCO
          (filling/canceling one cancels the other). stop_manager.py's
          trailing-stop replacement must account for both legs existing per
          symbol, not just the stop.
        """
        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce(time_in_force.lower())

        order_class = None
        stop_loss = None
        take_profit = None
        if stop_loss_price is not None and take_profit_price is not None:
            order_class = OrderClass.BRACKET
            stop_loss = StopLossRequest(stop_price=stop_loss_price)
            take_profit = TakeProfitRequest(limit_price=take_profit_price)
        elif stop_loss_price is not None:
            order_class = OrderClass.OTO
            stop_loss = StopLossRequest(stop_price=stop_loss_price)

        if order_type == "market":
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=tif,
                order_class=order_class,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        elif order_type == "stop":
            req = StopOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=tif,
                stop_price=stop_price,
            )
        else:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=tif,
                limit_price=limit_price,
                order_class=order_class,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

        order = _retry(self._trading.submit_order, order_data=req)
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "side": order.side.value,
            "qty": str(order.qty),
            "type": order.type.value if hasattr(order.type, "value") else str(order.type),
            "status": order.status.value if hasattr(order.status, "value") else str(order.status),
        }

    def cancel_order(self, order_id: str) -> None:
        _retry(self._trading.cancel_order_by_id, order_id)

    def get_bars(
        self,
        symbol: str,
        timeframe: TimeFrame = TimeFrame.Minute,
        start: str | None = None,
        end: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            feed=DataFeed.IEX,  # paper accounts get 403 on SIP for recent bars
        )
        bars = _retry(self._data.get_stock_bars, req)
        # `symbol in bars` is always False -- BarSet doesn't proxy `in` to
        # its `.data` dict the way `bars[symbol]` proxies `__getitem__` to
        # it, so the original (trading_bot-inherited) check silently
        # returned [] on every call, real data or not. Confirmed live
        # 2026-08-26: `raw.data['CMCSA']` had 162 real bars while
        # `'CMCSA' in raw` was False. Fixed to check `.data` directly.
        rows = bars.data.get(symbol, [])
        return [
            {
                "timestamp": str(b.timestamp),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": int(b.volume),
                "vwap": float(b.vwap) if hasattr(b, "vwap") and b.vwap else None,
            }
            for b in rows
        ]

    def get_latest_quote(self, symbol: str) -> dict[str, Any]:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = _retry(self._data.get_stock_latest_quote, req)
        q = quotes[symbol]
        return {
            "ask_price": float(q.ask_price),
            "bid_price": float(q.bid_price),
            "ask_size": int(q.ask_size),
            "bid_size": int(q.bid_size),
            "spread_pct": (
                (float(q.ask_price) - float(q.bid_price))
                / float(q.bid_price)
                * 100
                if float(q.bid_price) > 0
                else 0.0
            ),
        }

    def get_snapshots(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        req = StockSnapshotRequest(symbol_or_symbols=symbols)
        snapshots = _retry(self._data.get_stock_snapshot, req)
        result: dict[str, dict[str, Any]] = {}
        for sym, snap in snapshots.items():
            latest_trade = snap.latest_trade
            latest_quote = snap.latest_quote
            daily_bar = snap.daily_bar
            result[sym] = {
                "latest_trade_price": float(latest_trade.price) if latest_trade else None,
                "latest_ask": float(latest_quote.ask_price) if latest_quote else None,
                "latest_bid": float(latest_quote.bid_price) if latest_quote else None,
                "daily_open": float(daily_bar.open) if daily_bar else None,
                "daily_high": float(daily_bar.high) if daily_bar else None,
                "daily_low": float(daily_bar.low) if daily_bar else None,
                "daily_close": float(daily_bar.close) if daily_bar else None,
                "daily_volume": int(daily_bar.volume) if daily_bar else None,
            }
        return result

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> list[dict[str, Any]]:
        """Daily equity curve straight from the broker -- the authoritative
        source for evaluating live performance (not reconstructed from local
        execution logs, which only capture orders this bot placed itself)."""
        req = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        hist = _retry(self._trading.get_portfolio_history, req)
        rows = []
        for ts, equity, pl, pl_pct in zip(
            hist.timestamp, hist.equity, hist.profit_loss, hist.profit_loss_pct
        ):
            if equity is None:
                continue
            rows.append({
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
                "equity": float(equity),
                "profit_loss": float(pl) if pl is not None else 0.0,
                "profit_loss_pct": float(pl_pct) if pl_pct is not None else 0.0,
            })
        return rows

    def get_clock(self) -> dict[str, Any]:
        clock = _retry(self._trading.get_clock)
        return {
            "is_open": clock.is_open,
            "next_open": str(clock.next_open),
            "next_close": str(clock.next_close),
            "timestamp": str(clock.timestamp),
        }
