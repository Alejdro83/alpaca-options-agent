"""Thin wrapper around Alpaca's official MCP server (alpacahq/alpaca-mcp-server),
spawned as a stdio subprocess via `uvx`.

This is the piece that actually satisfies the hackathon's hard requirement
#2 ("projects must utilize either Alpaca's MCP server or its CLI tools") —
every options read (chain/snapshot/greeks) and every options order in this
project goes through here, never through a raw alpaca-py call. Market-data
reads for the *equity* screening step (vendored from trading_bot/) still use
the plain SDK, since that logic was validated before this hackathon existed
and touching it wasn't part of the plan — but nothing options-related
bypasses this module.

Usage:
    async with AlpacaMCP() as mcp:
        chain = await mcp.call("get_option_chain", {"underlying_symbol": "SPY"})
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import config

logger = logging.getLogger(__name__)

# `stdio_client`'s `errlog` defaults to *our own* stderr — the subprocess's
# FastMCP banner + startup log line ("Starting MCP server...") were bleeding
# straight through to bot.py's stderr, which run_options_cron.sh's `2>&1`
# then delivers as if it were noteworthy (found 2026-08-27 alongside the
# separate logging.basicConfig fix in bot.py — same underlying "silent
# unless something happened" contract, broken by a second, distinct
# stdio-inheritance path this time). Redirecting it to a file closes that
# path too.
_MCP_STDERR_LOG = Path(__file__).resolve().parent / "state" / "mcp_server.log"

# Alpaca's market-data API rate-limits (HTTP 429) under load: one screening
# cycle fans get_option_snapshot out across dozens of candidates and trips
# it (first seen 2026-09-02, ~19:00 UTC, on repeated SMCI snapshot calls).
# The MCP server surfaces the 429 as a tool *error*, not an exception, so
# retry it here with exponential backoff. Bounded — a genuine sustained
# outage still fails the cycle cleanly rather than hanging it.
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_BASE_DELAY = 1.0  # seconds; 1, 2, 4 between the 4 attempts


class AlpacaMCP:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "AlpacaMCP":
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command="uvx",
            # Real outage 2026-08-31: fastmcp 4.0.0 (a breaking major version)
            # published on PyPI at 18:20 UTC that same day -- alpaca-mcp-server
            # has no upper bound on its own fastmcp dependency, so `uvx`
            # (which re-resolves on every invocation, no lockfile) picked up
            # the new major version within minutes and every cycle since
            # failed at session.initialize() with "Connection closed"
            # (confirmed root cause: `ModuleNotFoundError: No module named
            # 'fastmcp.tools.tool'` when running the subprocess directly).
            # Pinned below 4.0.0 until alpaca-mcp-server itself is confirmed
            # compatible with the new major version.
            args=["--with", "fastmcp<4.0.0", "alpaca-mcp-server"],
            env={
                "ALPACA_API_KEY": config.alpaca.api_key,
                "ALPACA_SECRET_KEY": config.alpaca.secret_key,
                "ALPACA_PAPER_TRADE": "true",
            },
        )
        _MCP_STDERR_LOG.parent.mkdir(exist_ok=True)
        errlog = self._stack.enter_context(open(_MCP_STDERR_LOG, "a"))
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Calls one MCP tool and returns its parsed content. Raises on a
        tool-reported error rather than returning a half-parsed result —
        every caller in this project treats a spread entry/exit as
        all-or-nothing, never "partially placed and we're not sure."
        """
        assert self.session is not None, "call() used outside `async with`"
        logger.info("MCP call: %s(%s)", tool, arguments)

        for attempt in range(_RATE_LIMIT_RETRIES):
            result = await self.session.call_tool(tool, arguments)
            if not result.is_error:
                break
            text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
            is_rate_limit = "429" in text or "rate limit" in text.lower()
            if not is_rate_limit or attempt == _RATE_LIMIT_RETRIES - 1:
                suffix = f" after {attempt + 1} attempts" if is_rate_limit else ""
                raise RuntimeError(f"Alpaca MCP tool '{tool}' failed{suffix}: {text}")
            delay = _RATE_LIMIT_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "MCP tool '%s' rate-limited (429), retrying in %.1fs (attempt %d/%d)",
                tool, delay, attempt + 1, _RATE_LIMIT_RETRIES - 1,
            )
            await asyncio.sleep(delay)

        texts = [c.text for c in result.content if getattr(c, "text", None)]
        if not texts:
            return None
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            # Some tools (e.g. plain confirmations) return prose, not JSON —
            # callers that need structured data should already know which
            # tools return which shape.
            return joined
