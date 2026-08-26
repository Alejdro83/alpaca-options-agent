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
  `spread_builder.py`, `executor_mcp.py`) — every chain lookup, Greeks
  snapshot, and order placement goes through MCP tool calls, never the raw
  SDK.
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
| `spread_builder.py` | Signal → concrete strikes/expiration via MCP option chain + Greeks |
| `risk_gate.py` | Hard, non-negotiable risk checks |
| `llm_reasoner.py` | The autonomous decision step among risk-approved candidates |
| `executor_mcp.py` | Opens/closes spreads, exclusively via Alpaca's MCP server |
| `mcp_client.py` | Thin async wrapper spawning `alpaca-mcp-server` over stdio |
| `db.py` | Writes agent state to Supabase for the dashboard |
| `config.py` | All tunables, env-overridable, defaults explained inline |
| `screening/`, `signals/` | Vendored from `trading_bot/` — unmodified |

## Honest scope notes

- Field names in `spread_builder.py`'s parsing of MCP responses are written
  against Alpaca's documented schema but were not yet exercised against a
  live response as of this commit — `smoke_test.py` is the first thing to
  run against the real account, and this note will be removed once
  verified.
- The LLM reasoning step can choose *not* to trade a risk-approved
  candidate; it can never trade one that failed the gate. That asymmetry is
  intentional.
