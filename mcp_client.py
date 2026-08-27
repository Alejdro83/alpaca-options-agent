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


class AlpacaMCP:
    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "AlpacaMCP":
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command="uvx",
            args=["alpaca-mcp-server"],
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
        result = await self.session.call_tool(tool, arguments)
        if result.is_error:
            text = "; ".join(getattr(c, "text", str(c)) for c in result.content)
            raise RuntimeError(f"Alpaca MCP tool '{tool}' failed: {text}")
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
