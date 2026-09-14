---
name: consult-tools
description: Four read-only tools that do the numeric/data-heavy work for you (find candidates, build a plan, check a close, verify your own positions) so your reasoning goes into deciding, not computing.
version: 1.0.0
author: paco
tags: [trading, tools, risk, candidates]
always: true
---
# Consult Tools

You have real tools for the parts of a cycle that are pure arithmetic or
data-lookup, not judgment. Use them. The pattern across all four is the
same one `regime-classification` and `assess_spread_risk` already
established: something that looks like reasoning (which strike is
closest to a target delta, whether this cost-to-close hit a threshold,
which of 500 tickers cleared a filter) is actually a numeric calculation
an LLM tends to approximate rather than get exactly right — so it has a
real tool, imported from the judged bot's own tested code, never
reimplemented. None of these are gates. None of them can stop you from
doing something else instead. They exist so you spend your reasoning on
"is this actually a good trade" and "should I act on this number," not
on "let me recompute this by hand."

**Full docs, exact invocation syntax, and full detail on why each exists
are in TOOLS.md — this skill is the reminder that they exist and when to
reach for them, not a replacement for reading TOOLS.md before your first
real use of one.**

## The four tools, in the order a cycle would use them

1. **`find_candidates_preview.py`** — look beyond the fixed 19-name
   Watchlist. Runs the judged bot's own real screening pipeline (full
   S&P 500 + Nasdaq-100, liquidity + trend + volatility filters, real
   signal strength) and hands you a ranked list of who cleared the bar.
   Use it when you want to search rather than just rotate through
   `next_watchlist_batch.py`.
2. **`preview_spread_build.py`** — once you have a ticker + strategy in
   mind (from either screening path, or the regime-classification
   skill's output), this builds the real concrete plan: expiration,
   strikes, credit. Never guess which strike is closest to your target
   delta by reading a raw chain yourself.
3. **`assess_spread_risk`** (existing MCP tool, TOOLS.md's first
   section) — once you have a plan, this previews the full risk-gate
   read on it (liquidity, concentration, cluster, DTE, credit floors)
   before you commit.
4. **`preview_close.py`** — for every open position, every cycle: is it
   at profit target, at stop, or force-close-by-deadline, right now,
   with the real live mark. Not your own mental cost-to-close math.

Plus, any time (not tied to a specific step):

- **`check_positions.py`** — you're not sure how many spreads you
  actually hold, or whether your own count matches the broker. Ask this
  instead of trusting your memory of the cycle so far. Real incident
  that this exists because of (2026-09-04): a belief of "7 spreads at
  cap" with nothing in `zeroclaw_trading.spreads` to back it, caught
  much later than it should have been.

## Why this matters more now than it used to

Before 2026-09-04 every LLM turn was slow (the model tier was saturated
— up to 198s for a single word with zero tools involved, see
KNOWN_ISSUES.md Bug #12 in the shared repo). That's fixed now
(`openai.xiaomi_ultraspeed`), which means the actual bottleneck on how
good a cycle turns out is no longer "did it finish in time" — it's
whether the reasoning inside it is any good. Hallucinated arithmetic
(a wrong strike, a wrong profit-target read, a position count with no
data behind it) is a much bigger risk to a *good* decision than a slow
one ever was. These tools are the direct answer to that: less of your
turn spent computing, more of it spent actually deciding well.
