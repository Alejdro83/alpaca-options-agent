#!/usr/bin/env python3
"""Emergency flatten — close every open spread NOW.

Usage:
    python3 emergency_flatten.py          # interactive: lists spreads, asks for confirmation
    python3 emergency_flatten.py --yes    # skip confirmation (for scripted/cron use)

A break-glass tool for when something looks wrong mid-contest and a human
needs to intervene fast. Closes every open spread via the same MCP order
path bot.py uses (executor_mcp.close_spread / close_iron_condor), records
each close as "closed_emergency" in the DB so it's distinguishable from
normal rule-based exits.

Complementary to the kill switch (state/PAUSE): the kill switch stops NEW
cycles but does nothing about already-open positions — this tool is for
the latter. In a real emergency, use both: kill switch first (stop new
opens), then this (close what's already open).
"""
from __future__ import annotations

import asyncio
import sys

import db
import executor_mcp
from mcp_client import AlpacaMCP


def _print_spread(spread: dict) -> None:
    strategy = spread.get("strategy", "vertical")
    print(
        f"  id={spread['id']}  underlying={spread['underlying']}  "
        f"strategy={strategy}  direction={spread['direction']}  "
        f"contracts={spread.get('contracts', 1)}"
    )


async def _flatten_all() -> None:
    spreads = db.get_open_spreads()
    if not spreads:
        print("No open spreads — nothing to flatten.")
        return

    print(f"Open spreads ({len(spreads)}):")
    for s in spreads:
        _print_spread(s)
    print()

    if "--yes" not in sys.argv:
        answer = input("Type 'flatten' to close ALL open spreads: ").strip()
        if answer != "flatten":
            print("Aborted.")
            return

    print()
    closed = 0
    failed = 0
    async with AlpacaMCP() as mcp:
        for spread in spreads:
            spread_id = spread["id"]
            underlying = spread["underlying"]
            contracts = int(spread.get("contracts") or 1)
            is_iron_condor = spread.get("strategy") == "iron_condor"
            try:
                if is_iron_condor:
                    await executor_mcp.close_iron_condor(
                        mcp,
                        short_put_symbol=spread["short_symbol"],
                        long_put_symbol=spread["long_symbol"],
                        short_call_symbol=spread["call_short_symbol"],
                        long_call_symbol=spread["call_long_symbol"],
                        contracts=contracts,
                    )
                else:
                    await executor_mcp.close_spread(
                        mcp,
                        short_symbol=spread["short_symbol"],
                        long_symbol=spread["long_symbol"],
                        contracts=contracts,
                    )
                db.record_spread_close(spread_id, "closed_emergency", None)
                print(f"  CLOSED  id={spread_id}  {underlying}")
                closed += 1
            except Exception as exc:
                print(f"  FAILED  id={spread_id}  {underlying}: {exc}")
                print(f"    -> close it manually in the Alpaca UI")
                failed += 1

    print()
    print(f"Done: {closed} closed, {failed} failed.")
    if failed:
        print("WARNING: some spreads could not be closed — check the Alpaca UI and close them manually.")


def main() -> None:
    asyncio.run(_flatten_all())


if __name__ == "__main__":
    main()
