"""Run this FIRST against the real, funded hackathon account, before ever
enabling the cron job — confirms (1) the MCP server actually starts and
authenticates, (2) options data comes back in the shape spread_builder.py
assumes, (3) the account's options trading level is actually enabled (the
one dependency this whole project has on a step only the account owner can
do — see plan Day 0 item 1).

Usage: python smoke_test.py SPY
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp_client import AlpacaMCP


async def main(ticker: str) -> None:
    async with AlpacaMCP() as mcp:
        print(f"--- get_option_chain({ticker}) ---")
        chain = await mcp.call("get_option_chain", {"underlying_symbol": ticker})
        contracts = chain if isinstance(chain, list) else chain.get("contracts", chain.get("option_contracts", chain))
        print(f"Got {len(contracts) if isinstance(contracts, list) else '?'} contracts. First one:")
        print(json.dumps(contracts[0] if isinstance(contracts, list) and contracts else contracts, indent=2)[:1500])

        if isinstance(contracts, list) and contracts:
            symbol = contracts[0].get("symbol")
            print(f"\n--- get_option_snapshot([{symbol}]) ---")
            snap = await mcp.call("get_option_snapshot", {"symbols": [symbol]})
            print(json.dumps(snap, indent=2)[:1500])
            has_greeks = "greeks" in json.dumps(snap)
            print(f"\nGreeks present in snapshot: {has_greeks}")
            if not has_greeks:
                print("!! spread_builder.py's delta_of() will return None for everything — "
                      "check the real field name/path here and fix spread_builder.py before "
                      "wiring the cron job.")

        print("\n--- get_account (via alpaca_client, sanity check) ---")
        from alpaca_client import AlpacaClient
        account = AlpacaClient().get_account()
        print(json.dumps(account, indent=2))
        if abs(account["equity"] - 100_000) > 1:
            print(f"\n!! Account equity is ${account['equity']:,.2f}, not $100,000 — "
                  "confirm this is really the fresh, dedicated hackathon account "
                  "before trading on it.")


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    asyncio.run(main(ticker))
