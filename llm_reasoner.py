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
already-risk-approved candidate(s), if any, to open this cycle.

Each candidate carries a `strategy` field, one of two complementary structures chosen \
by a regime classifier (ADX trend strength + a 20d/60d realized-vol ratio, four \
regimes: TRENDING, VOLATILE_TRENDING, RANGING, VOLATILE_RANGING):
- 'vertical': a directional credit spread (bull put or bear call) — the underlying is \
  in a TRENDING or VOLATILE_TRENDING regime (real trend strength, ADX above \
  threshold), so this needs real directional conviction behind it (check `direction`, \
  `strength`, `signal_reasoning`).
- 'iron_condor': a neutral, range-bound structure (short put spread + short call \
  spread at the same expiration) — offered specifically because the underlying is in \
  a RANGING regime (ADX below threshold, no elevated vol either): no real trend to \
  lean on, so no directional bet is being made. It needs NO directional view: it \
  profits if the underlying just stays inside a range through expiration. Don't \
  penalize it for lacking a `direction`/`strength` signal — that absence is exactly \
  why it's an iron condor instead of a vertical.

Every candidate also carries a `structure` field, 'credit' or 'debit' (2026-09-02): \
- 'credit' (the default, always true for 'iron_condor'): the existing structure — \
  sells the near-the-money leg, buys a further-OTM leg for protection, collects a \
  net premium up front (`credit_estimate` is POSITIVE), profits from time decay and \
  the underlying staying away from the short strike.
- 'debit' (only possible for 'vertical', and only offered when the underlying's \
  trend is unusually strong — ADX well above the regime threshold — with real signal \
  strength behind it): BUYS the near-the-money leg (the actual directional bet) and \
  sells a further-OTM leg to reduce cost. `credit_estimate` is NEGATIVE — this is \
  CORRECT, not an error: it means premium was PAID, not received. `max_loss` is that \
  same debit, capped, exactly as for a credit spread. This structure profits from \
  real price movement, the OPPOSITE exposure of a credit spread — do not treat a \
  negative `credit_estimate` as a red flag or a reason to reject a 'debit' candidate; \
  judge it on the strength of the directional case (`direction`, `strength`, \
  `signal_reasoning`) exactly like a 'vertical'/'credit' candidate, just with the \
  opposite premium sign.

Each candidate also carries a `fact_ids` dict: short, deterministic, UPPERCASE \
identifiers (pattern: TICKER_FIELD, e.g. AAPL_CREDIT_EST, AAPL_MAX_LOSS, AAPL_DTE, \
AAPL_SIGNAL_STRENGTH) mapped to their numeric values. These are the ground-truth \
numbers this candidate was built from.

CITATION RULE (mandatory): Every number you mention in `reasoning` MUST cite its \
source fact ID inline using square brackets, exactly like this: \
"...$66.52 credit [AAPL_CREDIT_EST] against $433.48 max loss [AAPL_MAX_LOSS]...". \
WHY: this reasoning text is shown verbatim on the project's public dashboard as the \
agent's own explanation. A judge, a debugging session, or a future audit must be able \
to check every number you claimed against where it actually came from — uncited \
numbers are unverifiable and undermine the agent's credibility. Cite the fact IDs \
from the candidate(s) you are discussing, not invented ones.

Hard rules, already enforced in code before you see these candidates — do not \
second-guess them, only work within them:
- Every candidate here already passed the risk gate (max loss %, DTE window, daily \
  loss circuit breaker, concurrent-spread cap), regardless of strategy.
- You may select zero, one, or multiple candidates, up to `remaining_budget` more \
  concurrent spreads, mixing strategies freely.
- For 'vertical' candidates, prefer higher conviction (stronger underlying signal \
  `strength`, cleaner `reasoning` from the screening layer) and better risk/reward \
  (credit relative to max loss). For 'iron_condor' candidates, judge purely on \
  risk/reward (credit relative to max loss) since there is no directional signal to \
  weigh.
- Skipping a mediocre setup is a valid, often correct, decision.

Respond with ONLY a JSON object: {"selected": ["TICKER", ...], "reasoning": "..."}. \
`reasoning` must be a few real sentences explaining the specific choice — this text \
is shown verbatim on the project's public dashboard as the agent's own explanation, \
so make it genuinely informative, not generic filler."""


def decide(candidates: list[dict], remaining_budget: int) -> dict:
    """`candidates` items: {ticker, strategy, structure, direction, strength,
    signal_reasoning, credit_estimate, max_loss, expiration, fact_ids} —
    `direction`/`strength`/`signal_reasoning` are only meaningful for
    strategy='vertical' candidates (an 'iron_condor' candidate has no
    directional signal by construction). `fact_ids` maps short UPPERCASE
    identifiers (e.g. AAPL_CREDIT_EST) to their numeric values, for the
    LLM to cite in its reasoning.

    Returns {"selected": [...], "reasoning": str, "cited_fact_ids": [...],
    "uncited_ratio": float}. Falls back to "select nothing" (never a
    guess) if the API call fails or returns something unparseable — a
    skipped cycle is always safe, an unparsed/misread response acted upon
    blindly is not.
    """
    if not candidates:
        return {"selected": [], "reasoning": "No candidates survived the risk gate this cycle.", "cited_fact_ids": [], "uncited_ratio": 0.0}

    user_prompt = json.dumps(
        {"remaining_budget": remaining_budget, "candidates": candidates},
        default=str,
    )

    try:
        resp = requests.post(
            f"{API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ.get(API_KEY_ENV, '')}",
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

        # --- citation validation (best-effort, never blocking) ---
        reasoning = parsed["reasoning"]
        import re as _re
        cited = _re.findall(r"\[([A-Z0-9_]+)\]", reasoning)
        all_fact_keys: set[str] = set()
        for c in candidates:
            all_fact_keys.update(c.get("fact_ids", {}).keys())

        unknown = [fid for fid in cited if fid not in all_fact_keys]
        if unknown:
            logger.warning("LLM reasoning cited unknown fact IDs: %s", unknown)
        if not cited and candidates:
            logger.warning("LLM reasoning cited ZERO fact IDs despite %d candidates", len(candidates))

        valid_cited = [fid for fid in cited if fid in all_fact_keys]
        uncited_ratio = 1.0 - (len(valid_cited) / len(all_fact_keys)) if all_fact_keys else 0.0

        parsed["cited_fact_ids"] = valid_cited
        parsed["uncited_ratio"] = round(uncited_ratio, 3)
        return parsed
    except Exception as exc:
        logger.exception("LLM reasoning step failed, defaulting to no trade")
        return {"selected": [], "reasoning": f"LLM reasoning step failed ({exc}); no trade taken this cycle.", "cited_fact_ids": [], "uncited_ratio": 0.0}
