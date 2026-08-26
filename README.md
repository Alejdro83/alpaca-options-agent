# Alpaca Options Credit-Spread Agent

Submission for lablab.ai's **Alpaca AI Trading Agents Hackathon**
(28 Aug – 4 Sep 2026).

An autonomous agent that trades **credit vertical spreads** on US equities:
a bull put spread when its screening/signal layer sees a bullish setup, a
bear call spread on a bearish one — both defined-risk from the moment they
open, sized and gated by explicit code-level rules the LLM decision layer
cannot override.

## Why this design

- **Screening/signals**: vendored, unmodified, from a real trading system
  (`trading_bot/` on the author's own infrastructure — 400+ live paper
  trading cycles before this hackathon existed) — day/swing multi-horizon
  signal generation, EMA50/200 + ADX trend filtering, liquid-universe
  filters. This project reuses its *underlying selection* logic and
  translates the resulting direction into an options structure instead of
  an equity order.
- **Options execution — 100% via [Alpaca's official MCP
  server](https://github.com/alpacahq/alpaca-mcp-server)** (`mcp_client.py`,
  `spread_builder.py`, `executor_mcp.py`) — every chain lookup, quote, and
  order placement goes through MCP tool calls, never the raw SDK.
- **Delta computed in-process, not broker-supplied** (`black_scholes.py`)
  — verified live against the real account that real-time OPRA options
  data (needed for Alpaca's own Greeks) requires a paid Algo Trader Plus
  subscription; the free/paper `indicative` feed returns quotes with no
  Greeks at all. Rather than degrade to picking strikes by a fixed dollar
  distance, delta is computed with a standard closed-form Black-Scholes
  formula, using the same realized-volatility estimate the entry filter
  already computes as the implied-vol proxy — labeled as a proxy
  throughout, not overclaiming real IV.
- **Risk gates** (`risk_gate.py`) — hard, deterministic, code-level checks
  applied *before* any candidate reaches the LLM: a daily-loss circuit
  breaker, a max-concurrent-spreads cap, a max-loss-per-spread cap as % of
  equity, and a DTE window. A candidate that fails any gate is never shown
  to the model.
- **Autonomous decision layer** (`llm_reasoner.py`) — among whatever
  survives the gate, an LLM call picks which spread(s), if any, to actually
  open this cycle, and produces the plain-language reasoning shown on the
  dashboard.
- **Deployment**: scheduled every ~30min during market hours by
  [Hermes](https://github.com/) (the author's own agent-orchestration
  system), the same mechanism already running the underlying equities bot
  for months — chosen over a fresh cloud deployment specifically to reuse
  proven scheduling/monitoring rather than rebuild it under a hackathon
  deadline.
- **Dashboard**: a small Next.js app, also usable as a Telegram Mini App,
  reading the agent's live state (equity curve, open spreads, every cycle's
  reasoning) from Supabase — see `../alpaca-agent-dashboard/`.

## Running it

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the dedicated hackathon Alpaca account's keys
python smoke_test.py SPY   # confirm MCP + options data + $100k account, before anything else
python bot.py               # one cycle, manually
```

Scheduled execution: `run_options_cron.sh`, registered as a Hermes cron job
(mirrors the schedule/lock/silent-delivery pattern of the author's existing
equities bot).

## Files

| File | Role |
|---|---|
| `bot.py` | Main cycle: manage open spreads → screen → risk-gate → LLM decide → execute |
| `spread_builder.py` | Signal → concrete strikes/expiration via MCP option contracts + quotes |
| `black_scholes.py` | Self-computed delta (no broker Greeks available — see below) |
| `risk_gate.py` | Hard, non-negotiable risk checks, incl. force-close-by-contest-end |
| `llm_reasoner.py` | The autonomous decision step among risk-approved candidates |
| `executor_mcp.py` | Opens/closes spreads, exclusively via Alpaca's MCP server |
| `mcp_client.py` | Thin async wrapper spawning `alpaca-mcp-server` over stdio |
| `db.py` | Writes agent state to Supabase for the dashboard |
| `config.py` | All tunables, env-overridable, defaults explained inline |
| `screening/`, `signals/` | Vendored from `trading_bot/` — unmodified |

## Honest scope notes

- **No broker-supplied Greeks.** Verified live against the real hackathon
  account: `feed=opra` 403s with "OPRA agreement is not signed" (real-time
  OPRA data needs Alpaca's paid Algo Trader Plus plan), and the free
  `indicative` feed's snapshot has no `greeks` key at all. Delta is
  computed via `black_scholes.py` using a realized-volatility proxy for
  implied vol — a standard, well-understood substitution, not hidden
  anywhere in the code or this document.
- **`open_interest` is frequently `null`** on this account/feed, even for
  genuinely liquid near-the-money SPY strikes (verified directly) — the
  liquidity gate enforces it only when a real value comes back, leaning on
  the bid-ask-spread check (which does return real, usable data) as the
  effective liquidity signal.
- Every field-name and parameter-shape assumption in this codebase (MCP
  response nesting, `qty`/`ratio_qty` as strings, `position_intent`
  requirements) has been verified directly against the real account's
  actual responses, not just Alpaca's docs — several initial guesses were
  wrong and are visible in git history alongside their fixes.
- The LLM reasoning step can choose *not* to trade a risk-approved
  candidate; it can never trade one that failed the gate. That asymmetry is
  intentional.
