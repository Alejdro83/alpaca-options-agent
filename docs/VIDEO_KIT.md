# Video & presentation kit

Everything needed to shoot the pitch video and build the deck, in one place.
Deadline: **2026-09-04 15:00 UTC**. Video hard cap **5:00**, target 4:30.

---

## 1. Cards (title, lower-thirds, number, ledger, end)

**Canvas:** https://claude.ai/code/artifact/9236863e-f320-407f-967f-43585de12d7e

7 artboards at 1920×1080, styled to match the dashboard (near-black,
amber→orange title, sky accent, Space Grotesk + JetBrains Mono):

| Artboard | Use |
|---|---|
| Title | cold open |
| Lower-third A / B / C | on the "three arms" beat — *Credit spreads / iron condors / debit overlay* · *LLM end-to-end* · *Verticals only* |
| Number card | the "why this is real" beat (51 · 100% MCP · ~100 tests · 3→1) |
| Ledger card | the honest-P&L beat |
| End card | close — dashboard + repo URLs |

Export per artboard as PNG (toolbar → Export) or all as one PDF. Text is
editable in the canvas — **update the Ledger card numbers with the final
close before exporting.**

---

## 2. Narration (TTS-ready)

**`docs/PITCH_VO.md`** — 8 beat scripts written TTS-safe (numbers as
words, no code tokens), one render per beat with target durations and
pause marks. Also carries the Canva card copy and the segment→screen cut
list.

- TTS: Azure Neural via Loki (`en-US-AndrewNeural` / `en-US-EmmaNeural`,
  rate −4%) or MiMo's TTS. Render `vo-1.wav` … `vo-8.wav`.

---

## 3. Scripts

| File | What |
|---|---|
| **`docs/PITCH_VIDEO_SCRIPT.md`** | Master timeline — shot list, timings, b-roll cues, the "if it runs long" cuts |
| **`docs/PITCH_TALKING_POINTS.md`** | Word-for-word, **general segment** (comparison + project overview) — the part Alex + Will present |
| **`docs/PITCH_DEEPDIVE.md`** | Word-for-word, **our two agents** (deterministic bot + Paco), assumes the general segment ran first |
| Will's segment | verticals-only, recorded by Will separately, stitched in after the "three arms" beat — keep to ~30–40 s |

---

## 4. Reference (numbers + framing)

- **`docs/STRATEGIES.md`** — the full three-implementation write-up (the
  comparison table, what's shared vs not, "reading it honestly").
- **`ONE_PAGER.md` §Results** — current ledger: equity, realized P&L, the
  shadow book, Paco's state. **Refresh right before submission** — market
  is open until 15:00 UTC today.

---

## 5. Screenshots

- `docs/assets/dashboard.jpg` — main view, 2026-09-03 close (down week)
- `docs/assets/compare-midweek.jpg` — `/compare` earlier the same week
  (judged account +$55 over its $100k start)
- **To capture fresh before final export:** the `/compare` 3-way view at
  the last session's close → `docs/assets/compare.png`.

---

## 6. Live surfaces to screen-record

| | URL / target |
|---|---|
| Dashboard (main) | https://alpaca-agent-dashboard.vercel.app |
| 3-way compare | https://alpaca-agent-dashboard.vercel.app/compare |
| Backtest lab | https://alpaca-agent-dashboard.vercel.app/lab |
| MCP calls | `state/bot.log` on the trading host — `MCP call:` lines |
| Evolution dry-run | `state/evolution_report_dryrun.md` ("DRY RUN — nothing applied") |
| Tests green | `python tests/test_chaos.py` (and the suite) |
| Repo depth | file tree in the editor + `git log --oneline` |

---

## 7. Pre-submission checklist (video-relevant)

- [ ] Repo public (`gh repo edit Alejdro83/alpaca-options-agent --visibility public`) — README/ONE_PAGER images 404 until then
- [ ] Fresh `/compare` capture → `docs/assets/compare.png`, swap into README + ONE_PAGER
- [ ] Ledger card + `ONE_PAGER.md` §Results updated with the final close
- [ ] Video ≤ 5:00, Will's segment stitched, uploaded (YouTube/unlisted), link in the submission form
- [ ] Deck PDF (can be the card exports + a couple of dashboard stills)
