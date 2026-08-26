"""Run this against the real, funded hackathon account before ever enabling
the cron job — this exact script (an earlier version of it) is what
surfaced every real bug fixed on 2026-08-26: `mcp_client.py`'s `isError` vs
`is_error`, `get_option_chain` vs `get_option_contracts` for structural
chain data, the nested `data.snapshots[symbol].latestQuote.{bp,ap}` response
shape, the total absence of broker-supplied Greeks without a paid Algo
Trader Plus subscription, `open_interest` coming back null even for liquid
SPY strikes, `qty`/`ratio_qty` needing to be strings, and `position_intent`
being required for correct open/close semantics on multi-leg orders. Keep
running this after any change to mcp_client.py/spread_builder.py/
executor_mcp.py — it's cheap insurance against exactly this class of bug.

Usage: python smoke_test.py SPY
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp_client import AlpacaMCP


async def main(ticker: str) -> None:
    from alpaca_client import AlpacaClient
    client = AlpacaClient()

    print("--- get_account ---")
    account = client.get_account()
    print(json.dumps(account, indent=2))
    if abs(account["equity"] - 100_000) > 1:
        print(f"\n!! Account equity is ${account['equity']:,.2f}, not $100,000 — "
              "confirm this is really the fresh, dedicated hackathon account "
              "before trading on it.")

    print("\n--- get_clock ---")
    clock = client.get_clock()
    print(json.dumps(clock, indent=2, default=str))
    if not clock.get("is_open"):
        print("Market is closed right now — any test order will sit unfilled "
              "(status 'pending_new'/'new'); that's expected, not a bug. "
              "Cancel test orders afterward with AlpacaClient().cancel_order(id).")

    async with AlpacaMCP() as mcp:
        print(f"\n--- get_option_contracts({ticker}) ---")
        contracts_result = await mcp.call(
            "get_option_contracts",
            {"underlying_symbols": ticker, "status": "active", "limit": 3},
        )
        contracts = contracts_result.get("data", {}).get("option_contracts", [])
        print(f"Got {len(contracts)} contracts. First one:")
        print(json.dumps(contracts[0] if contracts else contracts_result, indent=2)[:1500])
        if contracts and contracts[0].get("open_interest") is None:
            print("\nNote: open_interest is null (a known gap on this account/feed — "
                  "spread_builder._passes_liquidity() only enforces it when present).")

        if contracts:
            symbol = contracts[0]["symbol"]
            print(f"\n--- get_option_snapshot([{symbol}], feed=indicative) ---")
            snap_result = await mcp.call(
                "get_option_snapshot", {"symbols": symbol, "feed": "indicative"}
            )
            snapshots = snap_result.get("data", {}).get("snapshots", {})
            print(json.dumps(snapshots, indent=2)[:1500])
            has_greeks = "greeks" in json.dumps(snapshots)
            print(f"\nGreeks present: {has_greeks} (expected: False on this account — "
                  "delta is computed via black_scholes.bs_delta instead, see its "
                  "module docstring for why)")


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    asyncio.run(main(ticker))
