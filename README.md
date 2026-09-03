# Alpaca Options Credit-Spread Agent

Submission for lablab.ai's **Alpaca AI Trading Agents Hackathon**
(28 Aug – 4 Sep 2026).

An autonomous agent that trades **credit vertical spreads** on US equities:
a bull put spread when its screening/signal layer sees a bullish setup, a
bear call spread on a bearish one — both defined-risk from the moment they
open, sized and gated by explicit code-level rules the LLM decision layer
cannot override.

**Live dashboard**: https://alpaca-agent-dashboard.vercel.app (also opens
as a Telegram Mini App via [@Alpaca_alejdro_bot](https://t.me/Alpaca_alejdro_bot) — same page, same code, either way).

## Three implementations, one backbone

This entry runs **three** autonomous agents at the same goal — defined-risk
options income on liquid US equities — sharing the same underlying-selection
and risk math, differing only in the **decision layer**:

- **Ours (judged)** — this repo. A deterministic `risk_gate.py` clears
  candidates first; an LLM then picks among the survivors and can never
  override the gate. Account `PA36EFWLOWRF` (the submission-form account).
- **Paco (research)** — vendored read-only under [`paco/`](paco/); runs on
  the zeroclaw agent framework, not from this repo. The LLM runs the whole
  cycle end to end with **no deterministic decision layer** — its only hard
  rules live in an external MCP proxy it cannot see or edit.
- **rookieriot (independent)** — a teammate's separate build:
  [github.com/massemolle/Alpaca-Trading-rookieriot](https://github.com/massemolle/Alpaca-Trading-rookieriot).

The dashboard's
[`/compare`](https://alpaca-agent-dashboard.vercel.app/compare) page
overlays all three equity curves against SPY buy-and-hold. Full write-up:
[`docs/STRATEGIES.md`](docs/STRATEGIES.md).

> Note: the sections below describe the judged agent. It now also opens
> iron condors and a directional debit-spread overlay (not only the
> verticals the older text mentions), and `short_leg_target_delta` is
> currently 0.13 — see `config.py` and `docs/STRATEGIES.md` for the
> current state.

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
- **Capital allocation (count-based approximation)** — the team's
  3-strategy spec calls for a 35%/35%/30% capital split (directional
  verticals bull / verticals bear / iron condors). This project
  approximates that via a COUNT-based cap: `max_concurrent_iron_condors`
  (default 2) out of `max_concurrent_spreads` (default 5) = 40% by count.
  Because `_optimal_contracts` sizes every position to ~2% of equity
  regardless of strategy, a count ratio approximates a capital ratio
  reasonably when several positions are open — but they are NOT the same
  mechanism and can diverge sharply at low position counts (e.g., 1 open
  IC out of 1 total = 100% by count, possibly a small fraction of equity).
  A real equity-percentage cap for iron condors is enforced separately via
  `max_iron_condor_equity_pct` (see `risk_gate.py`).
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
- **Two-strategy schema, dashboard ready ahead of the bot**: iron condor is
  being added as a second structure alongside the existing directional
  vertical spread (bull put / bear call) — `alpaca_hackathon.spreads` already
  has the `strategy` column and the four `call_*` leg columns in production
  (`supabase/alpaca_hackathon_schema_iron_condor.sql`), and the dashboard
  already renders an iron condor row (badge + all 4 legs) once one appears.
  The entry logic that actually opens iron condors is still in progress on a
  separate branch, not merged here yet — this branch only ever opens
  vertical spreads.

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

## Parameter optimization pass (`backtest_optimize.py`)

Run once, 2026-08-27, before the first live trading day. Reuses the REAL
signal-generation logic (a documented frozen port of `signals.swing`'s
per-symbol scoring, since that function fetches live data internally and
can't be pointed at an arbitrary past date), the REAL `TrendFilter`, and
the REAL volatility-percentile filter against REAL historical daily bars
(`trading_bot/backtest/data.py`'s `HistoricalDataLoader`, unmodified) — but
simulates spread economics with Black-Scholes theoretical pricing on the
same realized-vol proxy the live bot uses, since real historical option
chain prices aren't available on this account (same limitation as the live
bot's own delta calculation — see below). This compares our own parameter
choices against each other on one consistent, honest proxy; it is **not**
a market-realistic options backtest, and it doesn't simulate the
concurrent-spread portfolio cap or the LLM selection step.

Result on a 12-symbol liquid basket over ~2 years (104 real entry events):
the 10-21 DTE window (the 2026-08-26 research pass) held up well against a
7-14 alternative — a large, sign-flipping difference (7-14 went net
negative in several combinations), not a marginal one, so this conclusion
survives the caveat below. `short_leg_target_delta` initially looked like
it should move 0.17 → 0.20 (win rate 80.8% vs 78.8%), but a follow-up
sanity pass against synthetic cases (see `verify_backtest.py`-equivalent
checks run inline, 2026-08-27) found the strike-selection step (round the
Black-Scholes-implied strike to the nearest $1, an already-disclosed proxy
for a real listed-strike ladder) carries real, quantified error — up to a
34-47% relative delta miss at the production 15-day DTE midpoint for the
basket's lower-priced names (e.g. CMCSA, ~$35-45). That's the same order
of magnitude as the 0.17-vs-0.20 difference itself, so that specific
result is **not trustworthy** and was reverted — `short_leg_target_delta`
stayed at **0.17**. Recorded here rather than quietly fixed, since the
first version of this note (now corrected) stated the 0.20 bump with more
confidence than the underlying check actually supported.

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

## VIXY regime overlay (percentile-based vol-of-vol proxy)

Real VIX (^VIX) is **not available** via Alpaca's data API (confirmed live:
"invalid symbol: ^VIX"). VIXY (a VIX-futures ETF) IS available with real
historical daily bars. However, VIXY's absolute price has **no stable
relationship** to the real VIX level — it has decayed from hundreds of
dollars to ~$17-18 due to structural contango roll costs in the futures it
holds. Using literal price thresholds like "VIX < 15" would silently
misclassify the regime essentially always.

Instead, this project computes **VIXY's own percentile rank against its
trailing-year daily range** — the same rolling-window + `.rank(pct=True)`
technique used for the realized-volatility-percentile filter. This 0-1
percentile is then mapped to three buckets by tertile:

| VIXY percentile | Interpretation | Effect on iron condors |
|---|---|---|
| < 0.33 (low) | Vol-of-vol is low for its own year | Strikes closer to ATM: target delta = `min(base_delta * 1.5, 0.30)` |
| 0.33 – 0.67 (mid) | Standard vol environment | No change |
| > 0.67 (high) | Elevated vol-of-vol proxy | **No new iron condors** this cycle (verticals still allowed) |

This is explicitly a **percentile-based VIXY proxy**, NOT the real VIX
index or its literal threshold levels — labeled honestly, the same way
this codebase labels its realized-volatility-percentile filter as a proxy
for IV rank rather than claiming it's the real thing.
