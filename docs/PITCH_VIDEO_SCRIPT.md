# Pitch video — visual shooting script

**Master timeline for the submission video. Target 4:30 (hard cap 5:00).**
This is the shot list + voice-over; the word-for-word longer versions live
in [`PITCH_TALKING_POINTS.md`](PITCH_TALKING_POINTS.md) (general) and
[`PITCH_DEEPDIVE.md`](PITCH_DEEPDIVE.md) (our two agents). Will records his
verticals-only segment separately; it stitches in at beat 3.

VO can be split between presenters. Keep every claim inside the
"don't-say" rules at the bottom.

| # | ~time | ON SCREEN | VOICE-OVER |
|---|---|---|---|
| **1. Hook** | 0:00–0:18 | Title card → hard cut to the `/compare` page, three live equity curves moving | "Every retail trader asks the same question: *am I making or losing money, and is my capital safe?* We asked a different one — **how much of the decision should the model actually make?** So we didn't build one agent. We built three, gave them one shared risk brain, and we're measuring the difference live." |
| **2. Shared backbone** | 0:18–0:52 | `risk_gate.py` scrolling; then the `docs/STRATEGIES.md` comparison table | "All three trade the same defined-risk options structures on the same universe, and all three import the **same** `risk_gate.py` — never reimplemented. A dozen-plus hard checks — daily-loss breaker, concentration and correlation caps, credit-to-width floors, a post-stop cooldown. So any difference in behaviour is the *decision layer*, nothing else. That's what makes this an experiment, not three bots." |
| **3. The three arms** | 0:52–2:05 | Split-screen or three cards. Card 1: `bot.py` cycle + RiskGatesPanel. Card 2: `paco/AGENTS.md` + the proxy diagram. Card 3: rookieriot repo / Will's clip | "**Ours — the judged agent — gates, then decides.** Code screens, detects the regime, builds the strikes; the LLM only picks the final trade from a menu that already passed every check. It can decline; it can never open something the gate rejected.<br><br>**Paco — the research arm — decides, then gates.** A fully autonomous agent on the zeroclaw framework, no candidate menu at all — it reasons the whole cycle itself. The same risk code sits in a proxy it can't see or edit, as a hard veto.<br><br>*(→ Will's verticals-only segment stitches here.)*<br><br>Every option order, all three, goes through **Alpaca's official MCP server** — not the raw SDK." |
| **4. What the comparison shows** | 2:05–2:48 | The delta-band numbers on screen; then the two fix commits side by side | "And we can already say concrete things. Our strike selection never leaves a 0.02-to-0.32 delta band; Paco's proxy allows out to 0.45 — real, measured room to be more aggressive, while still provably unable to breach the portfolio caps. Running them side by side surfaced bugs a single bot wouldn't have — a reconciliation edge case on our side, a reasoning-budget timeout on Paco's." |
| **5. Rigor** | 2:48–3:28 | MCP calls in `state/bot.log`; the README parameter table; `evolution_report_dryrun.md`; a reasoning card with `[FACT_ID]` citations | "This data tier gives us no Greeks, no IV history, no VIX. We compute delta, an IV-rank proxy and a VIX proxy in-process — and label every one a proxy, in the code and on the dashboard. Every threshold is tagged: researched, or an unvalidated starting point a nightly job watches — and that job is **report-only** while we're being judged. The LLM has to cite a fact ID for every number it reasons with." |
| **6. Why this is real** | 3:28–3:48 | Fast montage: repo file tree, the test suite running green, the dashboard, the `/compare` curves, a terminal `git log` | "To be blunt about the depth: **51 Python modules. MCP-native. A dozen-plus hard risk checks. A live dashboard, on the web and in Telegram. A hundred-case test suite. A shadow book against a mechanical rule and a coin flip. Three agents, one shared risk brain, compared in public.** This is a system, and it's all in the repo." |
| **7. The honest ledger** | 3:48–4:12 | The Results section of the ONE_PAGER; the real equity curve (down week) | "And we'll show you the real number: this week is **red — about two-thirds of a percent down** on a freshly reset account, a handful of days, variance-dominated. Whether the model's freedom *pays* is the open question — that's the experiment. We're not claiming alpha. We're claiming a rig that can measure it honestly." |
| **8. Close** | 4:12–4:32 | `/compare` full-frame → the dashboard URL held on screen | "Three decision architectures. One risk brain. Compared live, in public. It's a web page and a Telegram Mini App — link's below." |

---

## Shot checklist (capture before editing)

- [ ] `/compare` page with all three curves visible and moving (record ~30s, trim)
- [ ] Screen-capture scroll of `risk_gate.py` and `docs/STRATEGIES.md` table
- [ ] `bot.py` cycle running in a terminal (one real cycle, or a replay) + the dashboard's RiskGatesPanel
- [ ] `paco/AGENTS.md` scroll + a simple diagram: `Paco → mcp_risk_proxy → alpaca-mcp-server` with `place_option_order` flagged
- [ ] `state/bot.log` showing `MCP call: ...` lines
- [ ] A reasoning card on the dashboard with `[FACT_ID]` citations visible
- [ ] `evolution_report_dryrun.md` open (shows "DRY RUN — nothing applied")
- [ ] Test suite running to green (`python tests/test_chaos.py` or the whole set)
- [ ] `git log --oneline` scroll (shows the real, dense history)
- [ ] Repo file tree (VS Code sidebar or `tree`) — the montage needs the "this is big" shot
- [ ] The ONE_PAGER Results section, and the real equity curve for the week
- [ ] Will's verticals-only clip (from him)

## Assets needed

- Title card (project name + "Alpaca AI Trading Agents Hackathon 2026")
- Lower-thirds for each arm: **Credit spreads / iron condors / debit overlay**,
  **LLM end-to-end**, **Verticals only**
- End card: `alpaca-agent-dashboard.vercel.app` + the repo URL
- One number graphic for beat 6 (the 51 / 14 / ~100 / 3 figures)

## Don't-say list (same as the ONE_PAGER / talking points)

- No "produces alpha" / "beats the market" — profitability is the open question.
- Realized vol is a proxy for IV; VIXY is a proxy for VIX — always "a proxy".
- The evolution job is report-only during judging — don't imply it self-tunes.
- "A handful of paper-trading days", not "a week of results".
- Only `PA36EFWLOWRF` is judged; Paco and rookieriot are comparison arms, not
  separate entries.
- "A stronger model should help the autonomous arm most" is an expectation
  from the design, not a benchmark.
