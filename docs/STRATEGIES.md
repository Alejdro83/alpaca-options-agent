# Three implementations, one backbone

This hackathon entry is really an experiment with **one goal** and **three
independent agents** chasing it:

> Trade **defined-risk options structures** (credit vertical spreads, iron
> condors, directional debit spreads) on liquid US equities, autonomously,
> on a brand-new $100k paper account — winning small and often rather than
> big and rarely.

All three share the same *underlying-selection* and *risk* backbone. What
differs is the **decision layer** — the part that turns "here are the
risk-approved candidates this cycle" into "open this / open nothing". That
is the variable we wanted to isolate:

| | **Ours — judged** | **Paco — research** | **rookieriot — independent** |
|---|---|---|---|
| Repo | this one (`alpaca-options-agent`) | vendored under [`paco/`](../paco/) (runs on the zeroclaw agent framework, not from this repo) | [github.com/massemolle/Alpaca-Trading-rookieriot](https://github.com/massemolle/Alpaca-Trading-rookieriot) |
| Decision layer | **deterministic `risk_gate.py` first**, then an LLM picks among the survivors and can never override the gate | **LLM runs the whole cycle end to end** — screen, classify regime, choose structure, size, place — with *no* deterministic decision layer in the agent itself | independent build by a teammate; its own reasoner (incl. a headless-Claude-Code mode) over the same signal modules |
| LLM | `mimo-v2.5-pro` (OpenAI-compatible endpoint) — but swappable; the model only *selects* from a pre-vetted menu, so results are relatively model-robust | `mimo-v2.5-pro` — but swappable; the model does *all* the judgement, so this arm is the one whose results should track model capability most | teammate's choice (incl. headless Claude Code) |
| Risk backstop | in-process, in `risk_gate.check_new_spread`, before any candidate reaches the model | **external** — an MCP proxy (`mcp_risk_proxy/`, not in this repo) intercepts every `place_option_order` and runs the **same** `risk_gate.check_new_spread` | its own gate, plus infra/robustness fixes shared both ways (limit orders, reconciliation, fail-closed checks) |
| Account (paper) | `PA36EFWLOWRF` (the submission-form account) | `PA34KZNBKA4L` | `PA34CFYP0MIZ` |
| Schedule | Hermes cron, adaptive 2–30 min, market hours | zeroclaw cron, adaptive (~10 min base), market hours | teammate's own scheduling |
| State / dashboard | Supabase `alpaca_hackathon` → [live dashboard](https://alpaca-agent-dashboard.vercel.app) | Supabase `zeroclaw_trading` → same dashboard | teammate's own instrumentation |

**Live 3-way comparison:** the dashboard's
[`/compare`](https://alpaca-agent-dashboard.vercel.app/compare) page
overlays all three equity curves against a SPY buy-and-hold benchmark, with
each agent's per-cycle reasoning.

---

## 1. Ours — the judged agent

The submission. A cycle is: manage open spreads → screen the S&P 500 ∪
Nasdaq-100 → **deterministic risk gate** → LLM decision → execute via
Alpaca's MCP server.

The gate (`risk_gate.py`) is plain Python that runs *before* the model
sees anything: daily-loss circuit breaker, max-concurrent-spreads cap,
per-spread max-loss as % of equity, DTE window, per-underlying and
correlation-cluster concentration caps, a minimum credit-to-width floor
per structure, and a post-stop re-entry cooldown. A candidate that fails
any check is never shown to the LLM. The LLM can decline a gate-approved
candidate; it can never open one the gate rejected. That asymmetry is the
whole point.

Structures currently live: bull put / bear call verticals, iron condors
(width scaled to the underlying, VIXY-percentile vol overlay), and a
directional debit-spread overlay.

See the top-level [`README.md`](../README.md) for the full design rationale
and honest-scope notes (no broker Greeks on this account, `open_interest`
frequently null, realized-vol used as an IV proxy, etc.).

## 2. Paco — the pure-reasoning research arm

Named `Paco`; runs entirely inside **zeroclaw** (a Rust agent framework),
scheduled by its own cron. Vendored here **read-only** under
[`paco/`](../paco/) so the implementation is inspectable — it is *not* run
from this repo.

Paco has **no deterministic decision layer of its own**. Each cycle, the
LLM reads its instruction files (`paco/AGENTS.md` is the single source of
strategy truth), pulls this cycle's rotation of watchlist tickers,
classifies each one's regime (ADX + vol-ratio), chooses a structure,
sizes it, and places the order — reasoning through every step itself.

The risk discipline is enforced **from outside the agent**: an MCP proxy
(`mcp_risk_proxy/`, on the trading host, not in this repo) sits between
Paco and Alpaca's real MCP server. It passes most tool calls straight
through, hard-blocks stock/crypto/close-all/exercise calls entirely, and
intercepts every `place_option_order` to run the **same**
`risk_gate.check_new_spread` this repo uses — same OCC-symbol parsing,
same credit/max-loss math, same cluster-exposure and options-level
inputs. A read-only `assess_spread_risk` tool returns the raw numbers
behind each check so Paco can see *why* something would be rejected before
it tries.

So Paco tests the opposite hypothesis to ours: **how far does an LLM get
running the full loop autonomously, when the only hard rules live in a
proxy it cannot see or edit?**

Paco currently runs on **`mimo-v2.5-pro`** over an OpenAI-compatible
endpoint, but nothing about the design is tied to that model — any
capable model can be dropped in. Because Paco has no deterministic
scaffold catching a weak judgement call (the proxy blocks *unsafe*
trades, never *unwise* ones), we'd expect its performance to move with
model capability more than either other arm's — the judged bot's LLM only
ranks a menu the code already built, so a weaker model there degrades
selection but not the structure, strikes, or risk. That expectation is
part of what the comparison is set up to probe; it isn't something we've
benchmarked across models yet.

`paco/` contents:

| Path | Role |
|---|---|
| `AGENTS.md` | Single source of strategy + per-cycle checklist + risk rules |
| `SOUL.md` | Identity / tone / values (incl. an "explain options concepts" brief for its Telegram side) |
| `TOOLS.md` | The tool surface it's allowed to use, via the proxy |
| `HEARTBEAT.md` | Liveness / status conventions |
| `scripts/run_cycle_paco.py` | Wrapper that guarantees one `cycles` row per cron tick + a lock so two agent processes never act on the account at once |
| `scripts/reconcile_paco.py` | Independent broker↔DB reconcile; engages a kill-all e-stop on an unexplained mismatch, self-heals a benign phantom close |
| `scripts/classify_regime_batch.py` | Batch regime classification (ADX / vol-ratio) for a cycle's tickers in one pass |
| `scripts/quiet_market_report_paco.py` | End-of-day "did the machinery do anything" diagnostic |
| `scripts/account_snapshot_paco.py`, `scripts/portfolio_greeks_paco.py` | Equity + held-position Greeks snapshots for the dashboard |
| `scripts/cancel_stale_orders_paco.py`, `scripts/adjust_cron.py`, `scripts/check_paco_tools.py` | Housekeeping: stale-order cleanup, adaptive cron cadence, tool-name validation against the proxy |

## 3. rookieriot — the independent build

A separate, independently-built agent by a teammate — its own repo, its
own account, its own deployment:
**[github.com/massemolle/Alpaca-Trading-rookieriot](https://github.com/massemolle/Alpaca-Trading-rookieriot)**.

It is verticals-only and reuses this project's `screening/` and `signals/`
modules (forked), with its own decision layer — including a mode that runs
headless Claude Code as the reasoner. We deliberately **do not copy
strategy** between the two builds; we do share *infrastructure and
robustness* fixes in both directions (marketable-limit orders,
broker/book reconciliation, fail-closed quote-age checks, the per-symbol
net-contracts reconcile fix, a "wrong account" guard).

---

## What is shared vs. what is not

**Shared** (so the comparison is about the decision layer, not the inputs):

- Underlying selection — the `screening/` + `signals/` modules (day/swing
  multi-horizon signals, EMA50/200 + ADX trend filter, liquid-universe
  filters), vendored from a real equities system with 400+ prior live
  paper cycles.
- The risk math — `risk_gate.check_new_spread`, the credit/max-loss
  formulas, the Black-Scholes delta with a realized-vol IV proxy.
- Broad parameter intent — ~0.13 target short-leg delta, a short DTE
  window, defined-risk structures only, "win small often".

**Not shared:**

- The decision layer (the point of the experiment).
- Runtime and orchestration — our Hermes cron vs. Paco's zeroclaw daemon
  vs. rookieriot's own deployment.
- Where the risk gate sits — in-process (ours) vs. an external proxy the
  agent can't see (Paco) vs. rookieriot's own.
- Each agent has its **own** $100k paper account; there is no shared
  capital and no cross-account netting.

## Reading the comparison honestly

- It is **days** of live paper data on **one** account each — directional,
  not statistically significant, and we say so on the dashboard.
- The judged bot's nightly parameter-evolution job runs **report-only**
  for the judged week — it never self-modifies while being scored; a real
  promotion is manual.
- The "a stronger model should help the autonomous arm most" claim is an
  expectation from the architecture, not a cross-model benchmark.
- Only `PA36EFWLOWRF` is the judged entry. Paco and rookieriot are
  comparison arms that make the "why this decision-layer design" argument
  concrete; they are not separate contest submissions.
