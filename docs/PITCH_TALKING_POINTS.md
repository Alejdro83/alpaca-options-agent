# Pitch video — talking points

Target: **≤ 5 minutes**. The spine of the whole video is the
**three-decision-architecture comparison**, not a feature tour. Judged on
P&L, technology implementation, creativity/originality, presentation — the
comparison is the creativity/originality centrepiece and doubles as the
technology story.

Read the blockquotes as-is or paraphrase. Durations are spoken estimates.
Narration can be split between presenters (Alex / Will) — nothing here is
tied to one voice.

This is the **general segment** (the comparison + project overview). The
closer look at our two agents — the deterministic bot and Paco — is in
[`PITCH_DEEPDIVE.md`](PITCH_DEEPDIVE.md); Will's verticals-only segment is
separate. All three get stitched together.

---

## 0. Cold open (~15s) — screen: the `/compare` dashboard, three equity curves

> Every AI trading agent has to answer one question: how much does the
> model actually *decide*, versus code around it? We didn't pick an
> answer — we built **three** agents, gave them one shared risk brain,
> and we're measuring the difference live, in public.

---

## 1. The shared backbone (~40s) — screen: repo / `risk_gate.py` + `docs/STRATEGIES.md`

> All three chase the same goal: trade **defined-risk options
> structures** — credit spreads, iron condors, debit spreads — on liquid
> US equities, autonomously, each on its own fresh $100,000 paper
> account.
>
> They share the same underlying-selection signals and the **same
> risk code** — `risk_gate.check_new_spread`, imported directly by every
> agent, never re-implemented. So any difference in how they behave is
> down to **one variable**: the decision layer. That's what makes this an
> experiment instead of three separate bots.

Key on-screen point: the risk gate is plain Python — daily-loss breaker,
per-spread loss cap, concurrency caps, concentration and correlation-
cluster caps, a credit-to-width floor, a post-stop re-entry cooldown.

---

## 2. The three arms (~75s) — screen: the comparison table from STRATEGIES.md, then each dashboard card

**Ours — the judged agent. "Gate, then decide."**

> Deterministic pipeline: screen ~500 names, detect the regime, build the
> exact strikes — all code. The risk gate runs *before* the model sees
> anything. The LLM only picks the final trade from a menu that already
> passed every hard check. It can say "none of these" — it can never open
> one the gate rejected.

**Paco — the research arm. "Decide, then gate."**

> A fully autonomous agent on the zeroclaw framework. **No pre-built
> candidate menu at all** — it reasons through the entire cycle itself:
> what to look at, which strikes to build, when to act. The same risk
> code sits in an MCP proxy *between* Paco and the broker as a hard veto
> it can't see or edit. Opposite order of operations, identical floor.

**rookieriot — the independent build.**

> A teammate's separate codebase, its own account, same gate-then-decide
> pattern as ours but built from scratch — an outside check that the
> design holds up when someone else implements it.

---

## 3. What the comparison already shows (~55s) — screen: `/compare` panel + a risk-gate detail

> And we can already say concrete things.
>
> **Measurable decision freedom.** Our bot never builds a trade outside a
> roughly 0.02–0.32 delta band. Paco's safety check allows up to 0.45 —
> real, measured room to pick a more aggressive strike, while it's still
> provably incapable of breaching the portfolio-wide risk caps, which are
> identical for all three.
>
> **The comparison is already useful.** Two of the three teams
> independently hit the *same* failure — an entry filter tuned too
> strict, throttling trades to near zero — and diagnosed and fixed it a
> day apart. Running them side by side surfaced it faster than one bot
> would have.
>
> And we're honest about the limits: this is a handful of live paper days
> on one account each. Whether the LLM's freedom *pays* is the open
> question — that's the experiment, not a claim we're making.

---

## 4. Technology & rigour (~45s) — screen: MCP calls in the log, the parameter table, the evolution dry-run report

> Everything options-related — every quote, every order — goes through
> **Alpaca's official MCP server**, all three agents.
>
> This account's data tier has no broker Greeks, no IV history, no VIX
> feed. Instead of skipping those checks or faking a number, we compute
> delta, an IV-rank proxy and a VIX proxy in-process — from
> Black-Scholes and realized volatility — and label every one as a proxy,
> in the code, on the dashboard, in the docs.
>
> Every threshold is tagged: backed by research, or flagged as an
> unvalidated starting point that a nightly evolution job watches — and
> that job runs **report-only** during judging. Nothing self-modifies
> while it's being scored.

---

## 5. Close (~20s) — screen: `/compare`, then the dashboard URL

> Three real decision architectures. One shared risk brain. Compared
> live, in public, with real money on the line — paper money, but real
> stakes for the experiment.
>
> It's all on the dashboard, as a web page and a Telegram Mini App.

---

## If time is very tight — 35s core segment (use section 0 + this)

> Our judged bot is a deterministic pipeline — screening, regime
> detection, strike selection, all code — and the LLM only picks the
> final trade among candidates that already passed every check. In
> parallel we built Paco: a fully autonomous agent that reasons through
> the entire cycle itself, with the exact same risk code as a hard veto,
> imported directly, never reimplemented. Plus a third, independently
> built version. Three decision architectures, one shared risk brain,
> compared live on our public dashboard.

## One-liner (for the submission blurb or a hard cut)

> We didn't build one trading agent — we built three, with the same risk
> backbone and different philosophies on how much the LLM gets to decide,
> and we're comparing them live, in public, with real data.

---

## B-roll / screen cue list

| Moment | On screen |
|---|---|
| Cold open | `/compare` — three equity curves vs SPY |
| Shared backbone | `risk_gate.py` scrolling; `docs/STRATEGIES.md` comparison table |
| Three arms | the STRATEGIES.md table, then each dashboard card in turn |
| Decision freedom | a risk-gate detail / the delta-band numbers |
| Comparison is useful | the two teams' fix commits side by side, or the dashboard note |
| Technology | `state/bot.log` MCP calls; the README parameter table; `evolution_report_dryrun.md` |
| Close | `/compare`, then `alpaca-agent-dashboard.vercel.app` full-frame |

## Don't-say list (keeps us honest, matches the ONE_PAGER)

- Don't claim the agents "produce alpha" or "beat the market" — the
  profitability question is explicitly open.
- Don't present realized vol as implied vol, or VIXY as VIX — always "a
  proxy".
- Don't imply the evolution job tunes the bot during judging — it's
  dry-run / report-only.
- Don't overstate the sample — say "a handful of paper-trading days".
