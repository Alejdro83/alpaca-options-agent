#!/usr/bin/env python3
"""check_paco_tools.py — cross-checks tool names in AGENTS.md/TOOLS.md
against the REAL MCP tool list (2026-08-30).

Not the zeroclaw eval harness (investigated and rejected -- Phase 0's
`default_tools()` is a single EchoTool with no MCP bridging at all, so it
cannot catch anything about Paco's real tools). This is a much smaller,
targeted, deterministic check aimed at the exact bug class that's already
bitten this project for real: an invented or misremembered tool name in
Paco's own instructions (e.g. `get_positions` instead of the real
`get_all_positions`, found and fixed 2026-08-29). A wrong name in the docs
means Paco fails with "Unknown tool" the moment it actually tries it --
this catches that BEFORE deployment, by asking the real proxy what tools
actually exist instead of trusting the docs' own claims.

Run this by hand after editing AGENTS.md or TOOLS.md, before considering
the change done:
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/check_paco_tools.py

Not part of Paco's own cycle -- this is a pre-deployment sanity check for
the human operator, not something Paco runs on itself.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

_WORKSPACE = Path(__file__).resolve().parent.parent
_AGENTS_MD = _WORKSPACE / "AGENTS.md"
_TOOLS_MD = _WORKSPACE / "TOOLS.md"

# Real Alpaca MCP tool names all use these verb prefixes (confirmed
# against the live list of 72 tools, 2026-08-30) -- used to pull likely
# tool-name mentions out of AGENTS.md's prose without also flagging
# unrelated backtick text (file paths, script names, "python3", "curl",
# "zeroclaw", schema names like "zeroclaw_trading", etc.).
_TOOL_PREFIXES = (
    "get_", "place_", "cancel_", "close_", "create_", "update_", "delete_",
    "add_", "remove_", "replace_", "search_", "fetch_", "list_", "exercise_",
    "do_not_exercise_",
)
_IDENT_RE = re.compile(r"`([a-z][a-z0-9_]*)`")


async def _real_tool_names() -> set[str]:
    import os
    # `uvx` (launches alpaca-mcp-server) lives in ~/.local/bin, which a
    # non-interactive shell (e.g. this script invoked over ssh) doesn't
    # necessarily have on PATH -- same fix mcp_risk_proxy already needed.
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")

    for line in Path("/home/lab-master/mcp_risk_proxy/.env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip().strip("'\"")

    from mcp_client import AlpacaMCP

    async with AlpacaMCP() as mcp:
        tools = await mcp.session.list_tools()
        return {t.name for t in tools.tools}


def _mentioned_tool_names(text: str) -> set[str]:
    return {
        name for name in _IDENT_RE.findall(text)
        if name.startswith(_TOOL_PREFIXES)
    }


def _bulleted_tool_list(text: str, section_header: str) -> set[str]:
    """Tool names explicitly listed as available/blocked in a named
    section -- checked unconditionally (not just prefix-matched), since
    every entry there is claimed as a real tool name by construction."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == section_header)
    except StopIteration:
        return set()
    names: set[str] = set()
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        names.update(_IDENT_RE.findall(line))
    # Filter to plausible tool-name shapes only (drop file paths etc. that
    # happen to be backtick-quoted in the same section, e.g. "TOOLS.md").
    return {n for n in names if "_" in n or n.startswith(_TOOL_PREFIXES)}


# Tools this proxy adds itself -- they never exist on the real upstream
# alpaca-mcp-server (this script queries that directly, not through the
# proxy, to avoid the extra complexity/flakiness of a nested MCP protocol
# connection just for this check), so they'd otherwise be wrongly flagged
# as invented. Keep this in sync with mcp_risk_proxy/server.py's own
# proxy-native tools (2026-08-30: just assess_spread_risk so far).
_PROXY_NATIVE_TOOLS = {"assess_spread_risk"}


def main() -> int:
    real_tools = asyncio.run(_real_tool_names()) | _PROXY_NATIVE_TOOLS

    agents_text = _AGENTS_MD.read_text()
    tools_text = _TOOLS_MD.read_text()

    claimed = set()
    claimed |= _mentioned_tool_names(agents_text)
    claimed |= _mentioned_tool_names(tools_text)
    claimed |= _bulleted_tool_list(tools_text, "## Alpaca MCP (via the risk-validating proxy)")
    claimed |= _bulleted_tool_list(tools_text, "## Blocked (the proxy rejects these unconditionally — don't try them)")

    unknown = sorted(claimed - real_tools)
    if unknown:
        print(f"FAIL: {len(unknown)} tool name(s) in AGENTS.md/TOOLS.md do not exist on the real proxy:")
        for name in unknown:
            print(f"  - `{name}`")
        print(f"\n({len(real_tools)} real tools confirmed live from the proxy.)")
        return 1

    print(f"OK: every tool name mentioned in AGENTS.md/TOOLS.md ({len(claimed)} checked) "
          f"is a real tool ({len(real_tools)} confirmed live from the proxy).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
