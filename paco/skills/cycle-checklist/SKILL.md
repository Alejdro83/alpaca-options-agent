---
name: cycle-checklist
description: The exact ordered step sequence for one trading cycle, as a direct action list — skip straight to executing instead of re-deriving the plan from AGENTS.md's prose every single cycle.
version: 1.0.0
author: paco
tags: [trading, cycle, checklist]
always: true
---
# Cycle Checklist

Every cycle is a fresh process — you never remember doing this before,
so you re-derive "what do I do now" from AGENTS.md's prose each time
before your first real tool call. This skill is that plan already made,
in the exact same order AGENTS.md's "Every session" and "Cycle" sections
specify — nothing here changes what happens or when, only how fast you
get from "cycle starts" to your first tool call. Follow it in order, and
save your actual reasoning for the judgment calls it can't make for you
(which candidate is best, whether a close is warranted, whether a
headline is a real catalyst). AGENTS.md/TOOLS.md remain the full
reference for the "why" and the edge cases (debit-spread math, risk
rules, correlation clusters, the regime table).

## Every session (before anything else)
1. Read SOUL.md, then USER.md.
2. `~/kill_switch` exists → stop, nothing else.
3. `get_clock` → closed → stop, nothing else. Never assume fixed hours.

## Cycle
1. `get_account_info` + `get_all_positions`. Unsure your own count is
   right? `check_positions.py` — ground truth, no side effects.
2. Daily loss > -3% → `zeroclaw cron pause`, best-effort `notify.sh`, stop.
3. 7 positions already open → skip straight to step 12 (manage existing
   only, no screening).
4. Run `next_watchlist_batch.py`, no arguments → this cycle's 3 tickers
   (your default). Want to look beyond the fixed Watchlist?
   `find_candidates_preview.py [N]` instead/alongside — the real
   screening pipeline over the full universe, ~2 min, sorted strongest
   first. Screen ONLY the tickers either one gives you.
5. Use the regime-classification skill on all 3 in ONE batched call →
   regime per ticker → the AGENTS.md regime table picks the strategy, or
   skip that ticker.
6. For each ticker with a strategy: `preview_spread_build.py TICKER
   STRATEGY [DIRECTION]` → the concrete plan (real strikes/expiration/
   credit) or an explicit "no plan, why". Do NOT eyeball
   `get_option_contracts`/`get_option_snapshot` yourself to guess the
   strike closest to ~0.13 delta.
7. For each candidate that reached a real buildable plan (not the whole
   batch): `get_news` → skip it if a real recent catalyst exists.
8. For each surviving candidate: `assess_spread_risk` → with 2+
   candidates, compare their real numbers and pick the strongest.
9. Check the picked candidate against Risk rules (AGENTS.md) yourself —
   not just the proxy's flag.
10. `place_option_order` if it passes — this always re-validates for
    real, never trust step 8's preview alone.
11. Poll `get_order_by_id` to confirm the fill → best-effort `notify.sh`.
12. Manage EACH open position, every cycle (not just when opening
    something new): `preview_close.py STRATEGY STRUCTURE CREDIT_RECEIVED
    EXPIRATION SHORT_SYMBOL LONG_SYMBOL [...]` → tells you profit
    target / stop / force-close-by-deadline with the real live mark. Do
    NOT compute cost_to_close/proceeds by hand and compare to the
    credit/debit formulas in AGENTS.md step 12 yourself — that's exactly
    what the tool already does. If it says close: `place_option_order`
    to close → poll `get_order_by_id` to confirm → `write_cycle.py
    close-spread` → best-effort `notify.sh`.
13. `write_cycle.py cycle` — every cycle, even a no-trade one. Include
    `--contracts`/`--structure debit`/negative `--credit` exactly as
    AGENTS.md step 13 specifies when they apply.
14. Run `adjust_cron.py`, no arguments, last step, always.

Report what you did in plain language after step 14, same as always —
this checklist changes how fast you get moving, not what you tell the
user.
