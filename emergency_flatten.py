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

Hardening (2026-08-30, from cross-checking a competing team's own break-
glass tool): shares state/bot.lock with run_options_cron.sh so this can
never race a concurrently-running cycle for the same position, and
reconciles against the broker first (informational — never blocks a
flatten) so an operator running this mid-emergency immediately sees any
broker/DB divergence rather than discovering it from a failed close.
"""
from __future__ import annotations

import asyncio
import fcntl
import sys
from pathlib import Path

import db
import executor_mcp
import reconciler
from alpaca_client import AlpacaClient
from mcp_client import AlpacaMCP

LOCK_PATH = Path(__file__).resolve().parent / "state" / "bot.lock"


def _acquire_lock():
    """Same lock file run_options_cron.sh holds for the whole of bot.py --
    a flatten racing a live cycle could both try to act on the same
    position at once. Non-blocking: a stuck lock must fail loud, not hang
    an operator trying to react fast in a real emergency.
    """
    LOCK_PATH.parent.mkdir(exist_ok=True)
    fh = open(LOCK_PATH, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print("ERROR: bot.lock is held by another process (the cron is mid-cycle) — "
              "wait for it to finish, or kill it first, then re-run this.")
        sys.exit(2)
    return fh


def _print_spread(spread: dict) -> None:
    strategy = spread.get("strategy", "vertical")
    print(
        f"  id={spread['id']}  underlying={spread['underlying']}  "
        f"strategy={strategy}  direction={spread['direction']}  "
        f"contracts={spread.get('contracts', 1)}"
    )


async def _flatten_all() -> None:
    client = AlpacaClient()
    recon = reconciler.reconcile(client)
    if not recon.ok:
        print(f"NOTE — broker/DB reconciliation mismatch (informational, not blocking): {recon.reason}")

    spreads = db.get_open_spreads()
    if not spreads:
        if recon.broker_option_symbols:
            print(
                "No open spreads in the DB, but the broker still holds option legs: "
                f"{sorted(recon.broker_option_symbols)} — close these manually in the Alpaca UI, "
                "this tool only acts on rows it has in the local book."
            )
        else:
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
            max_loss = float(spread["max_loss"]) if spread.get("max_loss") is not None else None
            # A failed quote fetch (network hiccup, feed outage) must not
            # abort the whole close attempt in an emergency tool -- fall
            # back to the max_loss bound below instead of giving up.
            mark: float | None = None
            try:
                if is_iron_condor:
                    mark = await executor_mcp.get_iron_condor_mark(
                        mcp,
                        short_put_symbol=spread["short_symbol"],
                        long_put_symbol=spread["long_symbol"],
                        short_call_symbol=spread["call_short_symbol"],
                        long_call_symbol=spread["call_long_symbol"],
                    )
                else:
                    mark = await executor_mcp.get_spread_mark(
                        mcp, spread["short_symbol"], spread["long_symbol"]
                    )
            except Exception:
                print(f"  (no live mark for id={spread_id} — falling back to max_loss bound)")
                mark = None

            try:
                if is_iron_condor:
                    await executor_mcp.close_iron_condor(
                        mcp,
                        short_put_symbol=spread["short_symbol"],
                        long_put_symbol=spread["long_symbol"],
                        short_call_symbol=spread["call_short_symbol"],
                        long_call_symbol=spread["call_long_symbol"],
                        contracts=contracts,
                        current_mark=mark,
                        max_loss=max_loss,
                    )
                else:
                    await executor_mcp.close_spread(
                        mcp,
                        short_symbol=spread["short_symbol"],
                        long_symbol=spread["long_symbol"],
                        contracts=contracts,
                        current_mark=mark,
                        max_loss=max_loss,
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
    lock_fh = _acquire_lock()
    try:
        asyncio.run(_flatten_all())
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


if __name__ == "__main__":
    main()
