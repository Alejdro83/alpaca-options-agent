"""WebSocket-based spread monitor — real-time profit target / stop loss
management for open credit spreads.

Replaces the 30-minute-cron gap in spread management with a persistent
WebSocket connection to Alpaca's indicative options quote feed.  On every
quote update it recomputes the spread mark and fires risk_gate.should_close
/ should_force_close instantly, closing via executor_mcp if triggered.

Designed to run as a systemd service or background process alongside the
existing cron-based bot.py.

Usage:
    python spread_monitor.py
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import signal
import sys
import time
from datetime import datetime, date, timezone
from pathlib import Path

import websockets

import db
import executor_mcp
import risk_gate
from config import config
from mcp_client import AlpacaMCP

LOG_DIR = Path(__file__).resolve().parent / "state"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filename=str(LOG_DIR / "spread_monitor.log"),
)
logger = logging.getLogger(__name__)

WS_URL = "wss://stream.data.alpaca.markets/v1beta3/indicative"
REFRESH_INTERVAL = 300
MAX_RECONNECT_DELAY = 60

# Same lock file run_options_cron.sh's flock uses. bot.py's cron and this
# long-running process both close spreads independently — without a shared
# lock, both could race to close the same spread at the same moment (e.g.
# a WS tick and a cron tick landing together), sending two closing orders
# or writing an inconsistent db.record_spread_close twice. Non-blocking:
# if bot.py currently holds the lock, this cycle's close is skipped and
# retried on the next quote update / cron tick rather than blocking the
# event loop waiting for it.
LOCK_PATH = LOG_DIR / "bot.lock"


class _NonBlockingLockHeld(Exception):
    pass


def _try_acquire_shared_lock():
    fh = open(LOCK_PATH, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise _NonBlockingLockHeld
    return fh


class SpreadMonitor:
    def __init__(self) -> None:
        self._spreads: list[dict] = []
        self._quotes: dict[str, dict[str, float]] = {}
        self._subscribed: set[str] = set()
        self._running = True
        self._mcp: AlpacaMCP | None = None
        self._ws: websockets.ClientConnection | None = None

    def _all_leg_symbols(self) -> set[str]:
        symbols: set[str] = set()
        for s in self._spreads:
            symbols.add(s["short_symbol"])
            symbols.add(s["long_symbol"])
        return symbols

    async def _refresh_spreads(self) -> None:
        """Real bug fixed 2026-08-27: this used to mark new symbols as
        'subscribed' in local state without ever sending the actual
        WebSocket subscribe message — a spread opened mid-session (after
        the initial connection) would silently never get live quotes here,
        relying entirely on bot.py's cron to eventually catch it instead.
        Now sends the subscribe message for real when a connection exists;
        if there isn't one yet (still connecting/reconnecting), the symbols
        stay unsubscribed and get picked up by _ws_session's own initial
        subscribe once it connects.
        """
        try:
            self._spreads = db.get_open_spreads()
        except Exception:
            logger.exception("Failed to refresh open spreads from DB")
            return

        new_symbols = self._all_leg_symbols() - self._subscribed
        if not new_symbols:
            return
        if self._ws is None:
            logger.info("New symbols pending subscription (no active WS yet): %s", new_symbols)
            return
        try:
            await self._ws.send(json.dumps({"action": "subscribe", "quotes": list(new_symbols)}))
            self._subscribed.update(new_symbols)
            logger.info("Subscribed to %d new symbol(s): %s", len(new_symbols), new_symbols)
        except Exception:
            logger.exception("Failed to subscribe to new symbols, will retry next refresh")

    def _print_summary(self) -> None:
        if not self._spreads:
            print("spread_monitor: no open spreads to monitor")
            return
        print(f"spread_monitor: monitoring {len(self._spreads)} spread(s)")
        for s in self._spreads:
            exp = s["expiration"]
            print(
                f"  {s['underlying']} {s['direction']} "
                f"exp={exp} credit=${s['credit_received']:.2f} "
                f"({s['short_symbol']} / {s['long_symbol']})"
            )

    def _compute_mark(self, short_symbol: str, long_symbol: str) -> float | None:
        sq = self._quotes.get(short_symbol)
        lq = self._quotes.get(long_symbol)
        if not sq or not lq:
            return None
        short_mid = (sq["bid"] + sq["ask"]) / 2
        long_mid = (lq["bid"] + lq["ask"]) / 2
        return short_mid - long_mid

    async def _handle_message(self, raw: str) -> None:
        msgs = json.loads(raw)
        if not isinstance(msgs, list):
            msgs = [msgs]
        for msg in msgs:
            t = msg.get("T")
            if t == "success" and msg.get("msg") == "authenticated":
                print("spread_monitor: authenticated to Alpaca indicative feed")
                logger.info("Authenticated")
            elif t == "error":
                logger.error("WS error: %s", msg)
                print(f"spread_monitor: WS error: {msg}", file=sys.stderr)
            elif t == "q":
                sym = msg.get("S")
                if not sym:
                    continue
                bp = msg.get("bp")
                ap = msg.get("ap")
                if bp is not None and ap is not None:
                    self._quotes[sym] = {"bid": float(bp), "ask": float(ap)}
                    await self._check_spread_for_symbol(sym)

    async def _check_spread_for_symbol(self, symbol: str) -> None:
        for spread in self._spreads:
            if symbol not in (spread["short_symbol"], spread["long_symbol"]):
                continue
            await self._evaluate_spread(spread)

    async def _evaluate_spread(self, spread: dict) -> None:
        expiration = datetime.strptime(str(spread["expiration"]), "%Y-%m-%d").date()
        force_close, force_reason = risk_gate.should_force_close(expiration=expiration)

        mark = self._compute_mark(spread["short_symbol"], spread["long_symbol"])

        if force_close:
            should_close, reason = True, force_reason
        elif mark is None:
            return
        else:
            should_close, reason = risk_gate.should_close(
                credit_received=float(spread["credit_received"]),
                current_mark=mark,
            )

        if not should_close:
            return

        await self._close_spread(spread, mark, reason)

    async def _close_spread(self, spread: dict, mark: float | None, reason: str | None) -> None:
        assert self._mcp is not None
        underlying = spread["underlying"]
        direction = spread["direction"]
        spread_id = spread["id"]

        # bot.py's cron closes spreads too (manage_open_spreads, every
        # 15 min) — without this shared lock, a WS tick here and a cron
        # tick could race to close the same spread at the same moment.
        # Non-blocking: if bot.py currently holds it, skip this attempt
        # and let the next quote update (or the cron itself) retry —
        # never block the event loop waiting for a lock that a one-shot
        # cron process will release again within seconds.
        try:
            lock_fh = _try_acquire_shared_lock()
        except _NonBlockingLockHeld:
            logger.info("bot.py cron holds the lock, deferring close of spread %s", spread_id)
            return

        try:
            try:
                await executor_mcp.close_spread(
                    self._mcp,
                    short_symbol=spread["short_symbol"],
                    long_symbol=spread["long_symbol"],
                    contracts=spread["contracts"],
                )
            except Exception:
                logger.exception("Failed to close spread %s", spread_id)
                print(f"spread_monitor: ERROR closing {underlying} {direction}", file=sys.stderr)
                return

            contracts_held = int(spread.get("contracts") or 1)
            if mark is None:
                realized_pnl = None
                status = "closed_expiry"
                note = f"Force-closed {underlying} {direction}: {reason} (P&L unknown)"
            else:
                # credit_received/mark are per-contract — same fix as bot.py's
                # manage_open_spreads, applied here too.
                realized_pnl = (float(spread["credit_received"]) - mark) * contracts_held
                status = "closed_expiry" if "force" in (reason or "") else (
                    "closed_profit" if realized_pnl > 0 else "closed_stop"
                )
                note = f"Closed {underlying} {direction}: {reason} (P&L ${realized_pnl:+.2f})"

            try:
                db.record_spread_close(spread_id, status, realized_pnl)
            except Exception:
                logger.exception("Failed to record close for spread %s", spread_id)

            print(note)

            self._spreads = [s for s in self._spreads if s["id"] != spread_id]
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
            lock_fh.close()

    async def _ws_session(self) -> None:
        delay = 1
        while self._running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    auth_msg = json.dumps({
                        "action": "auth",
                        "key": config.alpaca.api_key,
                        "secret": config.alpaca.secret_key,
                    })
                    await ws.send(auth_msg)

                    if self._subscribed:
                        sub_msg = json.dumps({
                            "action": "subscribe",
                            "quotes": list(self._subscribed),
                        })
                        await ws.send(sub_msg)

                    delay = 1

                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle_message(raw)

            except (websockets.ConnectionClosed, OSError) as exc:
                if not self._running:
                    break
                logger.warning("WebSocket disconnected: %s — reconnecting in %ds", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
            except Exception:
                if not self._running:
                    break
                logger.exception("Unexpected WS error — reconnecting in %ds", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
            finally:
                self._ws = None

    async def _refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(REFRESH_INTERVAL)
            if not self._running:
                break
            await self._refresh_spreads()

    async def _wait_for_spreads(self) -> None:
        """Poll DB every REFRESH_INTERVAL until spreads appear."""
        while self._running:
            await self._refresh_spreads()
            if self._spreads:
                return
            logger.debug("No open spreads, checking again in %ds", REFRESH_INTERVAL)
            await asyncio.sleep(REFRESH_INTERVAL)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._shutdown)

        print("spread_monitor: started, waiting for spreads...")
        logger.info("Spread monitor started")

        while self._running:
            await self._wait_for_spreads()
            if not self._running:
                break

            self._print_summary()
            self._mcp = AlpacaMCP()
            await self._mcp.__aenter__()

            try:
                ws_task = asyncio.create_task(self._ws_session())
                refresh_task = asyncio.create_task(self._refresh_loop())
                await asyncio.gather(ws_task, refresh_task)
            finally:
                if self._mcp is not None:
                    await self._mcp.__aexit__(None, None, None)
                    self._mcp = None

            # All spreads closed — go back to waiting
            if self._running:
                print("spread_monitor: all spreads closed, waiting for new ones...")
                logger.info("All spreads closed, returning to poll mode")

        print("spread_monitor: stopped")

    def _shutdown(self) -> None:
        print("\nspread_monitor: shutting down...")
        self._running = False


async def main() -> None:
    monitor = SpreadMonitor()
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
