"""The actual "autonomous AI trading agent" decision step — separate from
risk_gate.py on purpose: the gate is a hard, code-level backstop the model
cannot argue its way past (a candidate that fails the gate is never even
shown to the model); this module is where genuine judgment happens among
whatever survives the gate — which candidate(s) to act on this cycle, sized
within the remaining concurrent-spread budget, and why.

Provider is configurable via env (`REASONER_API_BASE`/`REASONER_API_KEY`/
`REASONER_MODEL`), any OpenAI-compatible chat-completions endpoint. Default
is the user's own flat-rate mimo-v2.5-pro plan (Xiaomi's direct API,
`https://token-plan-ams.xiaomimimo.com/v1`) — already used reliably
elsewhere in their own production infra (Gaussly), zero marginal cost since
it's a monthly plan, and confirmed 2026-08-26 via 3/3 live test calls
returning clean, schema-matching JSON (a genuinely free `:free` model on
OpenRouter was tried first — nvidia/nemotron-3-ultra-550b-a55b:free — and
rejected: 1 of 2 test calls returned a malformed response missing the
"choices" key, plausible free-tier capacity flakiness. This model drives
every trade decision, unattended, for the full judged week; reliability
matters far more here than the trivial cost difference from a paid
alternative like Sonnet).
"""
from __future__ import annotations

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

API_BASE = os.environ.get("REASONER_API_BASE", "https://token-plan-ams.xiaomimimo.com/v1")
API_KEY_ENV = os.environ.get("REASONER_API_KEY_ENV", "REASONER_API_KEY")
MODEL = os.environ.get("REASONER_MODEL", "mimo-v2.5-pro")

SYSTEM_PROMPT = """You are the decision layer of an autonomous options-trading agent \
competing in a hackathon (lablab.ai x Alpaca, "AI Trading Agents"). You choose which \
already-risk-approved credit spread candidate(s), if any, to open this cycle.

Hard rules, already enforced in code before you see these candidates — do not \
second-guess them, only work within them:
- Every candidate here already passed the risk gate (max loss %, DTE window, daily \
  loss circuit breaker, concurrent-spread cap).
- You may select zero, one, or multiple candidates, up to `remaining_budget` more \
  concurrent spreads.
- Prefer higher conviction (stronger underlying signal `strength`, cleaner \
  `reasoning` from the screening layer) and better risk/reward (credit relative to \
  max loss) over simply taking every candidate available.
- Skipping a mediocre setup is a valid, often correct, decision.

Respond with ONLY a JSON object: {"selected": ["TICKER", ...], "reasoning": "..."}. \
`reasoning` must be a few real sentences explaining the specific choice — this text \
is shown verbatim on the project's public dashboard as the agent's own explanation, \
so make it genuinely informative, not generic filler."""


def decide(candidates: list[dict], remaining_budget: int) -> dict:
    """`candidates` items: {ticker, direction, strength, signal_reasoning,
    credit_estimate, max_loss, expiration}. Returns {"selected": [...],
    "reasoning": str}. Falls back to "select nothing" (never a guess) if the
    API call fails or returns something unparseable — a skipped cycle is
    always safe, an unparsed/misread response acted upon blindly is not.
    """
    if not candidates:
        return {"selected": [], "reasoning": "No candidates survived the risk gate this cycle."}

    user_prompt = json.dumps(
        {"remaining_budget": remaining_budget, "candidates": candidates},
        default=str,
    )

    try:
        resp = requests.post(
            f"{API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ[API_KEY_ENV]}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        assert isinstance(parsed.get("selected"), list)
        assert isinstance(parsed.get("reasoning"), str)
        return parsed
    except Exception as exc:
        logger.exception("LLM reasoning step failed, defaulting to no trade")
        return {"selected": [], "reasoning": f"LLM reasoning step failed ({exc}); no trade taken this cycle."}
