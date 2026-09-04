# Pitch video — TTS narration, card list, and cut list

Companion to [`PITCH_VIDEO_SCRIPT.md`](PITCH_VIDEO_SCRIPT.md). Everything
here is ready to feed straight into a TTS engine (Azure Neural or MiMo) and
a Canva deck.

- **Render one audio file per beat** (`vo-1.wav` … `vo-8.wav`) so each lines
  up against its screen clip on the timeline.
- **Voice:** neutral, measured, unhurried — *not* an ad read. Azure
  `en-US-AndrewNeural` or `en-US-EmmaNeural` at rate `-4%`; MiMo: closest
  calm narrator preset.
- Text below is already TTS-safe (numbers as words, no code tokens, no
  symbols the engine will misread). `[pause]` = ~0.4 s hold; keep it.
- Total target ≈ **4 min 15 s** of narration, video hard-capped at 5:00.

---

## VO-1 · Hook · target 0:18

> Every retail trader asks the same question. Am I making money, or losing
> it — and is my capital safe? [pause] We asked a different one. How much
> of the decision should the model actually make? [pause] So we didn't
> build one trading agent. We built three, gave them one shared risk
> brain, and we're measuring the difference live.

## VO-2 · Shared backbone · target 0:34

> All three trade the same defined-risk options structures, on the same
> universe of large and mid-cap US stocks. And all three import the exact
> same risk gate — plain Python, never rewritten per agent. [pause] A
> dozen-plus hard checks: a daily-loss breaker, concentration and
> correlation limits, a minimum-credit floor, a cooldown that stops it
> re-entering a name it just got stopped on. [pause] So any difference in
> how the three behave is the decision layer. Nothing else. That's what
> makes this an experiment, and not just three bots.

## VO-3 · The three arms · target 1:13

> Our judged agent gates first, then decides. Code does the screening,
> the regime detection, and builds the exact strikes. The model only picks
> the final trade, from a menu that already passed every check. It can
> decline. It can never open something the gate rejected. [pause]
> Paco is the same idea, reversed. It's a fully autonomous agent on the
> zeroclaw framework, with no candidate menu at all. It reasons through the
> whole cycle itself — what to look at, which strikes to build, when to
> act. The same risk code sits in a proxy between Paco and the broker, as a
> veto it cannot see or edit. [pause]
> And a third build, rookieriot, from a teammate — its own codebase, its
> own account, verticals only. An outside check that the design holds up
> when someone else implements it. [pause]
> Every option order, all three agents, goes through Alpaca's official M C P
> server. Never the raw S D K.

## VO-4 · What the comparison shows · target 0:43

> And we can already say concrete things. [pause] Our strike selection
> never leaves a narrow delta band — roughly point oh two to point three
> two. Paco's proxy allows it out to point four five. That's real,
> measured room to be more aggressive, while it's still provably unable to
> breach the portfolio limits. [pause] Running them side by side surfaced
> bugs a single bot wouldn't have — a reconciliation edge case on our
> side, and on Paco's, cycles timing out because the model was doing too
> much math inline.

## VO-5 · Rigor · target 0:40

> This data tier gives us no option Greeks, no implied-volatility history,
> no V I X feed. So we compute delta, an I-V-rank proxy, and a V I X proxy
> in-process — and we label every one as a proxy, in the code and on the
> dashboard. [pause] Every threshold is tagged: backed by research, or
> flagged as an unvalidated starting point that a nightly job watches. And
> that job is report-only while we're being judged. Nothing tunes itself.
> [pause] The model has to cite a source for every number it reasons with.

## VO-6 · Why this is real · target 0:20

> To be blunt about the depth. Fifty-one Python modules. M-C-P native. A
> dozen-plus hard risk checks. A live dashboard, on the web and inside
> Telegram. A hundred-case test suite. A shadow book against a mechanical
> rule and a coin flip. Three agents, one shared risk brain, compared in
> public. [pause] This is a system. And it's all in the repo.

## VO-7 · The honest ledger · target 0:24

> And here's the real number. This week is red — about two-thirds of a
> percent down, on a freshly reset account, over a handful of days where
> variance dominates. [pause] Whether the model's freedom actually pays is
> the open question. That's the experiment. We're not claiming alpha —
> we're claiming a rig that can measure it honestly.

## VO-8 · Close · target 0:20

> Three decision architectures. One shared risk brain. Compared live, in
> public. [pause] It's a web page and a Telegram Mini App — the link is
> below.

---

## Canva cards — 1920 × 1080, dark background to match the dashboard

| Card | On it | Used at |
|---|---|---|
| **Title** | "Alpaca Options Agent" · "Autonomous options-spread trading — three decision architectures, one risk brain" · "lablab.ai × Alpaca — AI Trading Agents Hackathon 2026" | VO-1 open |
| **Lower-third A** | "Credit spreads / iron condors / debit overlay — *ours, judged*" | VO-3, "Our judged agent…" |
| **Lower-third B** | "LLM end-to-end — *Paco, research*" | VO-3, "Paco is the same idea reversed…" |
| **Lower-third C** | "Verticals only — *rookieriot, independent build*" | VO-3, "And a third build…" |
| **Number card** | Four big figures, stacked: "51 Python modules" · "100% options I/O via Alpaca MCP" · "~100-case test suite" · "3 agents · 1 shared risk gate" | VO-6 |
| **Ledger card** | "Week to date: −0.68% ·  $99,319 equity ·  −$518 realized" *(update with the pre-submission number)* · small: "Profitability is the open question, not our claim" | VO-7 |
| **End card** | "alpaca-agent-dashboard.vercel.app" · "github.com/Alejdro83/alpaca-options-agent" · "Also a Telegram Mini App" | VO-8 |

Keep type big — this gets watched at small sizes. One accent colour
(the dashboard's amber or a green), everything else white/grey on near-black.

---

## Cut list — VO segment → what's on screen

| Seg | Screen (record these) | Card overlay |
|---|---|---|
| VO-1 | Title card, then hard-cut to `/compare` with all three curves moving | Title |
| VO-2 | Slow scroll of the risk-gate section of the README, then the `docs/STRATEGIES.md` comparison table | — |
| VO-3 | Split or sequential: (a) `bot.py` running one cycle in a terminal + the dashboard RiskGatesPanel; (b) `paco/AGENTS.md` scroll + a simple Paco→proxy→MCP arrow; (c) the rookieriot repo page / Will's clip; (d) `state/bot.log` showing `MCP call:` lines | Lower-thirds A → B → C on the matching sentences |
| VO-4 | The delta-band figures (screen or card), then the two fix commits open side by side | — |
| VO-5 | `black_scholes.py`; the README parameter table; `evolution_report_dryrun.md` (shows "DRY RUN — nothing applied"); a dashboard reasoning card with the cited sources highlighted | — |
| VO-6 | Fast montage: repo file tree in the editor, the test suite running to green, the dashboard, `/compare`, a `git log --oneline` scroll | Number card held over the montage |
| VO-7 | The dashboard main view (current) and the equity curve for the week | Ledger card |
| VO-8 | `/compare` full-frame, then hold on the dashboard URL | End card |

**Will's verticals-only segment** stitches in right after VO-3's
lower-third C, before VO-4. Keep it to ~30–40 s so the whole piece stays
under 5:00.

## If it runs long

Drop VO-4 to its first two sentences (through "point four five"), and cut
VO-5's last line. That buys ~20 s without losing a pillar.
