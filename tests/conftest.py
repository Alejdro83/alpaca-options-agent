"""Fakes, mocks, and factory helpers for the chaos regression test suite.

Nothing here touches a real network endpoint. FakeMCP records every call
made against it so tests can assert "no order was placed" etc.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any


# ---------------------------------------------------------------------------
# FakeMCP — stands in for mcp_client.AlpacaMCP
# ---------------------------------------------------------------------------
class FakeMCP:
    """Async mock for AlpacaMCP.  Program per-test via ``responses`` (tool
    name -> return value or exception) and inspect ``call_log`` after.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.call_log: list[tuple[str, dict]] = []

    def set_response(self, tool: str, value: Any) -> None:
        """Register a canned response (or an exception instance to raise)."""
        self.responses[tool] = value

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.call_log.append((tool, arguments))
        resp = self.responses.get(tool)
        if isinstance(resp, Exception):
            raise resp
        if resp is not None:
            return resp
        return {}

    def calls_for(self, tool: str) -> list[dict]:
        return [args for t, args in self.call_log if t == tool]


# ---------------------------------------------------------------------------
# FakeClient — stands in for alpaca_client.AlpacaClient
# ---------------------------------------------------------------------------
@dataclass
class FakeClient:
    """Minimal stand-in for AlpacaClient with canned return values."""
    account: dict[str, Any] = field(default_factory=lambda: {
        "equity": 100_000.0,
        "last_equity": 100_000.0,
        "cash": 100_000.0,
        "buying_power": 200_000.0,
        "portfolio_value": 100_000.0,
        "status": "ACTIVE",
        "options_trading_level": 3,
    })
    clock: dict[str, Any] = field(default_factory=lambda: {
        "is_open": True,
        "next_open": "2026-08-30T13:30:00+00:00",
        "next_close": "2026-08-30T20:00:00+00:00",
        "timestamp": "2026-08-29T18:00:00+00:00",
    })
    positions: list[dict] = field(default_factory=list)
    # order_id -> order dict, for get_order() -- tests configure what a
    # poll should see (e.g. {"status": "filled", "filled_avg_price": 1.35}).
    orders: dict[str, dict] = field(default_factory=dict)
    canceled_order_ids: list[str] = field(default_factory=list)

    def get_account(self) -> dict[str, Any]:
        return self.account

    def get_clock(self) -> dict[str, Any]:
        return self.clock

    def get_positions(self) -> list[dict[str, Any]]:
        return self.positions

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.orders.get(order_id, {"status": "accepted"})

    def cancel_order(self, order_id: str) -> None:
        self.canceled_order_ids.append(order_id)


# ---------------------------------------------------------------------------
# Snapshot / quote helpers
# ---------------------------------------------------------------------------
def make_quote(
    bid: float = 1.00,
    ask: float = 1.10,
    age_minutes: int = 0,
) -> dict:
    """Build a single option-snapshot dict shaped like
    ``get_option_snapshot``'s ``data.snapshots.<symbol>`` value.
    """
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "latestQuote": {
            "bp": str(bid),
            "ap": str(ask),
            "t": ts.isoformat().replace("+00:00", "Z"),
        }
    }


def make_snapshot_response(snapshots: dict[str, dict]) -> dict:
    """Wrap per-symbol snapshots into the shape ``get_option_snapshot``
    actually returns: ``{"data": {"snapshots": {symbol: ...}}}``."""
    return {"data": {"snapshots": snapshots}}


def make_contracts_response(contracts: list[dict]) -> dict:
    """Wrap a list of option contract dicts into the shape
    ``get_option_contracts`` returns."""
    return {"data": {"option_contracts": contracts}}


# ---------------------------------------------------------------------------
# SpreadPlan / IronCondorPlan factories
# ---------------------------------------------------------------------------
def make_plan(
    underlying: str = "SPY",
    direction: str = "bear_call",
    expiration: date | None = None,
    short_strike: float = 450.0,
    long_strike: float = 455.0,
    short_symbol: str = "SPY260905C00450000",
    long_symbol: str = "SPY260905C00455000",
    credit_estimate: float = 1.50,
    max_loss: float = 3.50,
):
    from spread_builder import SpreadPlan
    return SpreadPlan(
        underlying=underlying,
        direction=direction,
        expiration=expiration or (date.today() + timedelta(days=10)),
        short_strike=short_strike,
        long_strike=long_strike,
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        credit_estimate=credit_estimate,
        max_loss=max_loss,
    )


def make_iron_condor_plan(
    underlying: str = "SPY",
    expiration: date | None = None,
    short_put_strike: float = 440.0,
    long_put_strike: float = 435.0,
    short_call_strike: float = 460.0,
    long_call_strike: float = 465.0,
    credit_estimate: float = 2.50,
    max_loss: float = 2.50,
):
    from spread_builder import IronCondorPlan
    return IronCondorPlan(
        underlying=underlying,
        direction="iron_condor",
        expiration=expiration or (date.today() + timedelta(days=10)),
        short_put_strike=short_put_strike,
        long_put_strike=long_put_strike,
        short_call_strike=short_call_strike,
        long_call_strike=long_call_strike,
        short_put_symbol=f"{underlying}260905P00{int(short_put_strike*1000):08d}",
        long_put_symbol=f"{underlying}260905P00{int(long_put_strike*1000):08d}",
        short_call_symbol=f"{underlying}260905C00{int(short_call_strike*1000):08d}",
        long_call_symbol=f"{underlying}260905C00{int(long_call_strike*1000):08d}",
        credit_estimate=credit_estimate,
        max_loss=max_loss,
    )


def make_option_contract(
    symbol: str,
    strike_price: float,
    option_type: str = "put",
    expiration_date: str | None = None,
    open_interest: str | int | None = "500",
) -> dict:
    return {
        "symbol": symbol,
        "strike_price": str(strike_price),
        "type": option_type,
        "expiration_date": expiration_date or (date.today() + timedelta(days=10)).isoformat(),
        "open_interest": str(open_interest) if open_interest is not None else None,
    }
