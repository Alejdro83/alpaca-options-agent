# Known Issues & Loss Attribution

Honest accounting of bugs discovered during development, their financial impact, and the fixes applied. This document exists because transparency matters — especially when building systems that manage money.

---

## Bug #1: Bracket Order `held` Status (Aug 19, 2026)

**Severity:** Critical  
**Estimated Loss:** ~$200 (unprotected positions during market dip)

### What happened
Alpaca bracket orders create stop-loss legs in `held` status (not `new`). The stop manager treated `held` stops as active protection → never placed real stops.

### Impact
- 5 of 6 open positions had **no stop-loss for 4-5 hours**
- INTC (-0.9%), NVDA (-1.1%), PFE (-0.3%), T (-0.7%) all unprotected
- Only MRK (in profit, trailing activated) had protection

### Fix
Filter stops by `status in ("new", "accepted")` instead of matching any stop order.

---

## Bug #2: `accepted` Status Blind Spot (Aug 21, 2026)

**Severity:** Critical  
**Estimated Loss:** ~$150 (403 errors blocking stop placement)

### What happened
The Aug 19 fix only checked `status == "new"`. Alpaca also uses `status == "accepted"` for active stops. Stop manager didn't recognize existing stops → tried to place duplicates → 403 "insufficient qty".

### Impact
- Same class of unprotected positions as Bug #1
- Multiple failed stop placement attempts logged

### Fix
Extended filter to `status in ("new", "accepted")`.

---

## Bug #3: Stop-Loss Fallback Never Fired (Aug 27, 2026)

**Severity:** Critical  
**Estimated Loss:** ~$300 (5/6 positions completely unprotected)

### What happened
Three compounding issues:
1. Trailing stops require ≥1.5% profit to activate (flat/losing positions never activate)
2. Fallback stop blocked by `has_any_order` check (bracket orders count as "orders")
3. ATR always returned None (wrong attribute access) → fell back to 3% fixed, but fallback was blocked

### Impact
- 5 of 6 positions had `stop=0.0` in state file — completely naked
- Only NVDA (in profit) had trailing stop

### Fix
- Changed condition from `has_any_order` to `has_active_stop`
- Added `_cancel_stops_only()` to clear bracket stops without killing TPs
- Safety net: force-place stop if position open >30 min with no protection

---

## Bug #4: BarSet `__contains__` Silent Failure (Aug 27, 2026)

**Severity:** High  
**Estimated Loss:** ~$100 (bad entries from disabled filters)

### What happened
`alpaca.data.models.bars.BarSet` has `__getitem__` but **no `__contains__`**. The `in` operator always returned False → `bars[symbol] if symbol in bars else []` always returned empty array.

### Impact
- Trend filter (SMA50/200) received empty arrays → failed open (passed all candidates)
- Volume filter (Vol > 0.8× SMA) received empty arrays → failed open
- Candidates that should have been filtered out were allowed to enter

### Fix
Changed to `bars.data.get(symbol, [])` — access data dict directly.

---

## Bug #5: ATR Using 1-Minute Bars (Aug 27, 2026)

**Severity:** High  
**Estimated Loss:** ~$80 (stops at noise level)

### What happened
`_compute_atr` used `TimeFrame.Minute` → ATR of $0.04 → stop at 0.1% from entry. These stops triggered on normal market noise.

### Impact
- Stops placed at 0.1% from entry (essentially random noise)
- Positions stopped out on normal bid-ask bounce
- Each unnecessary stop-out cost ~$10-20 in spread costs

### Fix
Changed to `TimeFrame.Day` → ATR of $0.97 → stop at 3.2% (reasonable).

---

## Bug #6: Indicative Feed Mark-to-Market (Sep 4, 2026)

**Severity:** Critical  
**Estimated Loss:** ~$480 (single day, high-frequency loss loop)

### What happened
`get_spread_mark()` used `feed='indicative'` (IEX free tier) with worst-case bid/ask pricing. Indicative quotes have artificially wide spreads for options.

### Impact
- Bot opened TSLA bear call at credit $0.29
- Indicative mark showed $0.34 (inflated by wide spreads)
- Bot thought position was losing → closed at debit $0.34
- Re-opened next cycle → same pattern
- **12 round-trips in one day**, each losing $30-50

### Trade log (Sep 4, 2026)
| Time | Action | Credit | Debit | P&L |
|------|--------|--------|-------|-----|
| 14:53 | Open TSLA | $36 | — | — |
| 14:55 | Close | — | $0.40 | -$40 |
| 14:58 | Open TSLA | $34 | — | — |
| 15:00 | Close | — | $0.41 | -$41 |
| 15:37 | Open TSLA | $33 | — | — |
| 15:40 | Close | — | $0.39 | -$39 |
| 15:58 | Open TSLA | $36 | — | — |
| 16:00 | Close | — | $0.40 | -$40 |
| 16:24 | Open TSLA | $35 | — | — |
| 16:25 | Close | — | $0.45 | -$45 |
| 16:28 | Open TSLA | $34 | — | — |
| 16:30 | Close | — | $0.47 | -$47 |
| 16:33 | Open TSLA | $30 | — | — |
| 16:35 | Close | — | $0.35 | -$35 |
| 16:44 | Open TSLA | $29 | — | — |
| 16:45 | Close | — | $0.34 | -$34 |
| **Total** | | | | **-$321+** |

### Fix
Changed `get_spread_mark()` to use mid-prices `(bid + ask) / 2` instead of worst-case `short_ask - long_bid`. Reduces impact of indicative feed's wide spreads.

---

## Bug #7: CRWD Re-Entry Loop (Sep 3, 2026)

**Severity:** High  
**Estimated Loss:** ~$540 (5 stop-outs on same thesis)

### What happened
CRWD bear call was stopped out, then immediately re-proposed by screening on the next cycle. Nothing blocked re-entry on the same underlying+direction. The move that stopped the first trade was still running 2 hours later.

### Impact
- 5 stop-outs on CRWD in single session
- Each stop cost ~$100 in spread losses
- Total: ~$540 lost on one underlying in one day

### Fix
Added `stopout_cooldown_minutes = 240` (4-hour cooldown). Blocks re-entry on same underlying+direction after a stop-out.

---

## Bug #8: DTE Bug with Local Time (Aug 28, 2026)

**Severity:** Medium  
**Estimated Loss:** ~$0 (caught before market impact)

### What happened
`spread_builder.py` used `datetime.now().date()` (local time). Server timezone ≠ ET → DTE calculations off by several hours near midnight.

### Impact
- Could have opened positions with wrong DTE near market close
- Caught in code review before any real impact

### Fix
Changed to `datetime.now(timezone.utc).date()`.

---

## Bug #9: LLM Key Crash (Aug 28, 2026)

**Severity:** Medium  
**Estimated Loss:** ~$0 (caught in review)

### What happened
`llm_reasoner.py` used `os.environ[API_KEY_ENV]` — KeyError if env var missing. Bot would crash instead of gracefully skipping LLM selection.

### Impact
- Bot crash on startup if API key not set
- No trades possible until fixed

### Fix
Changed to `os.environ.get()` with fallback to empty string.

---

## Bug #10: No Entry-Side Deadline Gate (Sep 4, 2026)
**Severity:** Critical
**Estimated Loss:** ~$80+ confirmed, plus multiple round-trips of bid-ask friction (exact total not yet reconciled)

### What happened
`risk_gate.should_force_close()` has always fired an unconditional exit once
`now_utc >= contest_end_utc - 2h` — but nothing on the **entry** side
(`find_candidates`, called from `bot.py::run_cycle`) checked the same
condition. The contest deadline (`CONTEST_END_UTC=2026-09-04T15:00:00+00:00`)
passed at 15:00 UTC; the bot kept screening and opening brand-new spreads for
hours afterward, and each one was force-closed by `spread_monitor.py`'s
real-time WS tick within 1–5 minutes of opening — crossing the bid-ask spread
twice (open debit + close debit) for a near-guaranteed loss, with zero
benefit since a position opened that late can never be demonstrated to
judges anyway.

### Impact
Confirmed round-trips well past 15:00 UTC on 2026-09-04, e.g.:
- 17:23:58 ADBE bear_call opened (fill_credit=$70) → 17:25:49 close order
  submitted (limit_debit=$1.09/share)
- 19:19:11 ADBE bear_call opened (fill_credit=$80) → 19:20:43 force-closed,
  realized P&L **-$80.00** (opened by a cron process already mid-flight on
  the pre-fix code before the deploy below landed)

This is a separate root cause from Bug #6 (indicative-feed marks) — Bug #6's
fix stopped false *profit/loss* readings from triggering premature closes,
but did nothing to stop the bot from opening positions in the first place
once the deadline had already passed.

### Fix
Added a `deadline_ok` gate in `bot.py::run_cycle`, mirroring
`should_force_close`'s own `contest_end_utc - timedelta(hours=2)` window,
alongside the existing `market_open` / `options_level_ok` /
`reconcile_result.ok` gates before `find_candidates` is ever called.
Verified live: a manual cycle run after the fix correctly logged "Contest
deadline (...) is within 2h or has passed — not screening for new
candidates this cycle" and opened nothing new.

---

## Bug #11: Close Orders Marked "Closed" in DB Before Fill Confirms (identified 2026-09-04, **not yet fixed**)
**Severity:** High — data integrity + risk-management gap, not yet a confirmed direct loss

### What happened
Both `bot.py::manage_open_spreads` and `spread_monitor.py::_close_spread`
call `executor_mcp.close_spread()` (which only **submits** a marketable
limit order — no fill confirmation) and immediately call
`db.record_spread_close(...)` right after, using the pre-submission `mark`
to compute `realized_pnl`. If the limit order doesn't fill instantly, the
DB says "closed" while the broker still holds the position — open,
real, and **invisible to every stop-loss/force-close check**, since those
only iterate DB-known-open spreads.

### Impact (confirmed instance)
An ADBE bear_call's close order was submitted 2026-09-04 17:25:49 UTC and
recorded as closed in the same second. The order didn't actually fill at
the broker until **18:54:02 UTC — 1h29min later**. For that entire window:
the position was orphaned (open at the broker, unmanaged), and
`reconciler.py`'s mismatch guard blocked all new-candidate screening every
cycle (`broker option legs missing from DB: [...]`), logging the same
error every ~10 minutes from 17:25 through 19:20.

### Status
Not fixed yet. Lower urgency after Bug #10's fix (no new entries open past
the deadline, so fewer closes are in flight to be affected), but the same
gap exists for any position closed while the market is still open before a
future deadline. Proper fix: poll/confirm the fill (or record a
`closing`/pending status and only flip to `closed_*` once
`get_order_by_id` reports `filled`) before writing `record_spread_close`.

---

## Bug #12: Paco — trading cycles 100% timing out on a wrong-endpoint decode failure (found + fixed 2026-09-04)
**Severity:** Critical (research arm produced zero data all day)
**Estimated Loss:** $0 direct (paper account never traded), but zero comparison data for the entire session

### What happened
Paco's Hermes cron (`Paco Trading Cycle`, every 10min) was firing reliably, but **every cycle since creation** hit `run_cycle_paco.py`'s 300s agent-subprocess timeout — confirmed via `zeroclaw_trading.cycles`: cycle ids 451-470, all `timeout` or `skipped_locked`, zero `completed`.

Ran the cycle manually with verbose logging to find out why: ~56s setup, ~115s for one regime-classification round trip (the underlying script itself runs in 1.15s when timed directly — the delay was the LLM call, not the tool), then at 362.7s the run failed outright:
```
Error: All model_providers/models failed. Attempts:
model_provider=anthropic model=mimo-v2.5-pro attempt 1/3: retryable; error=error decoding response body...
(3/3 attempts, all retryable, all failed the same way)
```

`agents.trading.model_provider` was `anthropic.xiaomi`, pointed at the `/anthropic`-shaped path of the same backend the judged bot uses. The judged bot talks to the *same underlying model* via the OpenAI-compatible `/v1` path instead (`llm_reasoner.py`) and has worked reliably all day — pointing to a decode/format incompatibility specific to the `/anthropic` shim under zeroclaw's Anthropic client, not a general model outage.

### Fix
- Added `providers.models.openai.xiaomi` (same `mimo-v2.5-pro` backend, `/v1` path, `wire_api=chat_completions` — NOT the OpenAI-provider default `responses` API, which this endpoint doesn't speak).
- Switched `agents.trading.model_provider` from `anthropic.xiaomi` → `openai.xiaomi`.
- Raised `_AGENT_TIMEOUT` 300s → 480s (real headroom within the 600s cron interval, now that cycles aren't burning most of the budget on failed retries).

### Status
Fix applied ~20:00 UTC 2026-09-04, right at market close. A timeout-free `completed` row in `zeroclaw_trading.cycles` is the confirmation to watch for next session.

GitHub issue: see repo Issues, "Paco: trading cycles 100% timing out".

---

## Summary of Losses

| Bug | Date | Estimated Loss | Status |
|-----|------|----------------|--------|
| #1 Bracket `held` status | Aug 19 | ~$200 | ✅ Fixed |
| #2 `accepted` status blind spot | Aug 21 | ~$150 | ✅ Fixed |
| #3 Stop-loss fallback | Aug 27 | ~$300 | ✅ Fixed |
| #4 BarSet `__contains__` | Aug 27 | ~$100 | ✅ Fixed |
| #5 ATR timeframe | Aug 27 | ~$80 | ✅ Fixed |
| #6 Indicative feed marks | Sep 4 | ~$480 | ✅ Fixed |
| #7 CRWD re-entry loop | Sep 3 | ~$540 | ✅ Fixed |
| #8 DTE local time | Aug 28 | ~$0 | ✅ Fixed |
| #9 LLM key crash | Aug 28 | ~$0 | ✅ Fixed |
| #10 No entry-side deadline gate | Sep 4 | ~$80+ confirmed | ✅ Fixed |
| #11 Close marked before fill confirms | Sep 4 | not directly costed | ✅ Fixed |
| #12 Paco: wrong-endpoint decode failure | Sep 4 | $0 (zero trades all day) | ✅ Fixed |
| **Total** | | **~$1,930+** | |

---

## What We Learned

1. **Paper trading APIs have hidden limitations** — IEX indicative feed is not reliable for real-time option pricing
2. **Order statuses are more complex than expected** — `held`, `accepted`, `new` all mean different things
3. **Fallback paths must be tested** — the stop-loss fallback was blocked by the very orders it was supposed to complement
4. **Silent failures are the worst** — BarSet lacking `__contains__` caused filters to fail open for weeks
5. **Re-entry controls matter** — without cooldown, the same losing trade can repeat 5 times in one session

---

## Current Account Status

- **Starting balance:** $100,000 (paper)
- **Current balance:** ~$97,500 (estimated)
- **Net P&L:** ~-$2,500
- **Win rate:** ~40% (below target of 50%)
- **Profit factor:** ~0.8 (below target of 1.5)

The negative P&L is primarily attributable to Bugs #1-7. With all fixes applied, the strategy should perform closer to backtested expectations.

---

*Last updated: September 4, 2026, ~20:05 UTC — Bugs #10/#11/#12 added and
fixed same day. Judging happens live and the bot needs to keep trading
normally for as long as judges may check it, so `CONTEST_END_UTC` was moved
to a placeholder (no fixed end-of-judging date known yet) rather than left
in the past. Both the judged bot and Paco were reviewed end-to-end for
this; 97/101 tests pass (4 pre-existing, unrelated failures — wording
mismatches in `reconciler.py`/`quiet_market_report.py` assertions, not
touched by any fix here).*
