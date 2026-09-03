# Pitch video — deep dive: the two agents our side built

Runs **after** the general comparison segment
([`PITCH_TALKING_POINTS.md`](PITCH_TALKING_POINTS.md)). This one goes a
level deeper on the two agents the core team (Alex + team) built — the
**deterministic judged bot** and **Paco** — and assumes the viewer has
already seen the three-way, one-shared-risk-brain framing. Will's
verticals-only segment is separate and gets stitched in alongside this.

Target ~3 minutes. Blockquotes = read aloud or paraphrase; durations are
spoken estimates; narration can be split between presenters.

---

## 0. Bridge in (~10s) — screen: the `/compare` table, then zoom to the two left cards

> You've seen the setup — three agents, one shared risk gate. Here's a
> closer look at the two we built on our side, and why we ran them as
> mirror images of each other.

---

## 1. The judged bot — "gate, then decide" (~65s) — screen: `bot.py` cycle, then the risk-gate panel, then a reasoning card with cited facts

> The judged agent is a pipeline, and almost all of it is code.
>
> It screens the S&P 500 and Nasdaq-100 down through liquidity filters and
> an EMA-plus-ADX trend filter, then a four-way regime classifier —
> trend strength against volatility — and that classification *routes the
> structure*: a strong trend gets a directional credit spread, with a
> stricter debit-spread overlay tried first on the highest-conviction
> ones; a range-bound name with no real trend gets an iron condor; an
> elevated-volatility name with no trend gets skipped entirely.
>
> Then the risk gate runs — daily-loss breaker, per-spread loss cap,
> concentration and correlation-cluster caps, a credit-to-width floor, a
> cooldown that stops it re-entering a name it just got stopped on. All
> plain Python. A candidate that fails any of it is dropped *here*.
>
> Only what survives reaches the LLM, and its job is narrow: pick which
> of these, if any, to open. It has to cite a fact ID for every number in
> its reasoning — no uncited claims — and that reasoning goes straight to
> the public dashboard.

Optional add (~10s) if the segment has room:

> The data this account's tier doesn't give us — option Greeks, an IV
> history, a VIX feed — we compute in-process from Black-Scholes and
> realized volatility, and label every one as a proxy. We'd rather show
> our work than fake a number.

---

## 2. Paco — "decide, then gate" (~65s) — screen: `paco/AGENTS.md`, then the `mcp_risk_proxy` sitting between Paco and Alpaca, then Paco's dashboard card

> Paco is the same idea run backwards. It's a fully autonomous agent on
> the zeroclaw framework — no pre-built candidate menu at all. Every
> cycle it decides *itself* what to look at, pulls a rotating batch of
> tickers, classifies each regime, chooses a structure, builds the
> strikes, sizes the position, and places the order.
>
> The risk discipline isn't inside the agent — it's a proxy sitting
> between Paco and Alpaca's real MCP server. Most tool calls pass
> straight through. Dangerous ones — close-all, exercise, stock orders —
> are blocked outright. And every `place_option_order` is intercepted and
> run through the **exact same** `risk_gate.check_new_spread` the judged
> bot uses — imported, not reimplemented. Same floor, opposite order of
> operations.
>
> There's also a read-only preview tool, so before Paco commits to a
> trade it can ask the proxy *why* something would be rejected and adjust.
> Paco's whole strategy and discipline live in a Markdown file it re-reads
> every cycle — not in code it can edit.

---

## 3. What running both actually taught us (~45s) — screen: the delta-band numbers, then the two fix commits / the friction metrics

> Two mirror-image designs, and each one caught things the other
> wouldn't have.
>
> We can measure exactly how much freedom the reasoning buys: our judged
> bot never builds a strike outside a roughly 0.02-to-0.32 delta band;
> Paco's proxy allows up to 0.45 — real room to be more aggressive, while
> it's still provably unable to breach the portfolio caps.
>
> The deterministic side surfaced a reconciliation bug — two identical
> spreads opened minutes apart, netting into one broker position the
> per-row check choked on. Paco's side surfaced a different class of
> problem entirely: the agent was doing so much per-option reasoning that
> cycles were timing out and it wasn't trading at all — which a
> gate-then-decide pipeline would never hit.
>
> And the honest part: this is still a handful of paper-trading days on
> one account each. Whether the extra freedom *pays* is the open
> question. That's the experiment.

---

## 4. Bridge out (~10s) — screen: back to `/compare`

> Same risk brain, two philosophies about the model's leash. Alongside
> them, a third build from the ground up —

(hand to Will's verticals-only segment.)

---

## If time is tight — ~40s combined

> Our judged bot is a code pipeline — screen, regime-detect, route the
> structure, risk-gate — and the LLM only picks the final trade from a
> pre-vetted menu, citing a fact for every number. Paco is the same idea
> reversed: a fully autonomous agent that reasons the whole cycle itself,
> with the identical risk code as an external veto it can't see or edit.
> We can measure the difference — our strike selection stays inside a
> 0.02-0.32 delta band, Paco's is allowed out to 0.45 — and each design
> caught bugs the other never would.

---

## B-roll / screen cue list

| Moment | On screen |
|---|---|
| Bridge in | `/compare` table, zoom to the two left cards |
| Judged bot pipeline | `bot.py` `find_candidates` / `_apply_trend_and_volatility_filters`; the 4-regime routing |
| Judged bot gate | the RiskGatesPanel on the dashboard |
| Judged bot LLM | a reasoning card with `[FACT_ID]` citations highlighted |
| Proxies | `black_scholes.py`; the parameter table's proxy notes |
| Paco autonomy | `paco/AGENTS.md` cycle checklist scrolling |
| Paco proxy | a diagram: Paco → `mcp_risk_proxy` → `alpaca-mcp-server`, with `place_option_order` flagged |
| Paco preview | `assess_spread_risk` output in a log |
| What we learned | the delta-band numbers; the reconcile fix commit; the "0 trades / timeouts" metrics then the batch-regime fix |
| Bridge out | `/compare` |

## Don't-say list (same rules as the ONE_PAGER)

- No "produces alpha" / "beats the market" — profitability is the open
  question.
- Realized vol is a proxy for IV; VIXY is a proxy for VIX — always say
  "proxy".
- The nightly evolution job is dry-run / report-only during judging —
  don't imply it tunes anything live.
- "A handful of paper-trading days", not "a week of results".
- Paco's account and rookieriot's are comparison arms, not separate
  contest entries — only `PA36EFWLOWRF` is judged.
