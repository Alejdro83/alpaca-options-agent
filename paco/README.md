# Paco — the pure-reasoning research arm

**Paco is not the judged submission** and **does not run from this repo.**
It is a second autonomous options agent chasing the same goal as the
judged bot (defined-risk options income on liquid US equities, "win small,
often"), on its **own dedicated $100k paper account** (`PA34KZNBKA4L`),
built to isolate one variable: the decision layer.

- **Judged bot** (this repo, top level): a deterministic `risk_gate.py`
  clears candidates *first*, then an LLM picks among the survivors and can
  never override the gate.
- **Paco** (these files): the **LLM runs the entire cycle end to end** —
  screen, classify regime, choose structure, size, place — with **no
  deterministic decision layer inside the agent**. Its only hard rules
  live in an external MCP proxy it cannot see or edit.

See [`../docs/STRATEGIES.md`](../docs/STRATEGIES.md) for the full 3-way
comparison (the third arm is a teammate's independent build).

## How it actually runs (not from here)

Paco runs inside **[zeroclaw](https://github.com/)** — a Rust agent
framework, as a daemon on the trading host — scheduled by its own cron
(~10 min, market hours). Each tick, zeroclaw starts the agent, which reads
these instruction files fresh and works through `AGENTS.md`'s per-cycle
checklist.

The risk discipline is enforced **outside** the agent by `mcp_risk_proxy/`
(also on the trading host, also not in this repo). It sits between Paco and
Alpaca's real MCP server and:

- passes most tool calls straight through;
- **hard-blocks** `place_stock_order`, `place_crypto_order`,
  `close_all_positions`, `cancel_all_orders`, `close_position`,
  `exercise_options_position`, `do_not_exercise_options_position`,
  `get_option_chain`;
- intercepts every `place_option_order` and runs the **same**
  `risk_gate.check_new_spread` the judged bot uses — same OCC-symbol
  parsing, same credit / max-loss math (with `max_loss * contracts` before
  the equity-% comparison), same cluster-exposure and options-level
  inputs, plus liquidity / IC credit-to-width / delta-sanity / vol-regime
  backstops that reuse the bot's own code;
- exposes a read-only `assess_spread_risk` tool that returns the raw
  numbers behind every check so Paco can see *why* a spread would be
  rejected before trying.

So this directory is enough to **read and audit** what Paco is and how it
decides. It is not enough to **run** it — that needs the zeroclaw daemon,
the proxy, and Paco's own account credentials, none of which live here.

## Files

| Path | Role |
|---|---|
| `AGENTS.md` | **Single source of strategy truth** — intent, per-cycle checklist, regime→structure table, risk rules, closing procedure |
| `SOUL.md` | Identity, tone, values; also an "explain options concepts honestly" brief for its Telegram side |
| `TOOLS.md` | The exact tool surface it may use (through the proxy) |
| `HEARTBEAT.md` | Liveness / status-reporting conventions |
| `scripts/run_cycle_paco.py` | Cron entry point: guarantees one `zeroclaw_trading.cycles` row per tick, and a lock so two agent processes never touch the account at once (`decision='skipped_locked'` if held) |
| `scripts/reconcile_paco.py` | Independent broker↔DB reconcile; kill-all e-stop on an unexplained mismatch, self-heals a benign phantom close |
| `scripts/classify_regime_batch.py` | Classifies a whole cycle's tickers (ADX + vol-ratio) in one pass — added because per-option manual computation was timing the cycle out |
| `scripts/quiet_market_report_paco.py` | End-of-day "did the machinery do anything today" diagnostic |
| `scripts/account_snapshot_paco.py` | Equity / buying-power snapshot for the dashboard |
| `scripts/portfolio_greeks_paco.py` | Held-position Greeks snapshot for the dashboard |
| `scripts/cancel_stale_orders_paco.py` | Cancels orders left working too long |
| `scripts/adjust_cron.py` | Adaptive cron cadence (slower when quiet, faster near events) |
| `scripts/check_paco_tools.py` | Validates every tool name in the instruction files exists among the proxy's real tools |

Vendored 2026-09-04 for the hackathon submission; the live copies are on
the trading host.
