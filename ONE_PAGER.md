# Alpaca Options Credit-Spread Agent — One-Pager

*Draft skeleton — fill in real numbers/screenshots once the account has a
few days of live activity (see plan Day 6-7). Every section below states
what evidence it needs; don't submit until each placeholder is replaced
with something real and verifiable.*

## AI logic

- Underlying selection: [screening/signals summary — liquid universe size,
  which horizon(s) used, trend-filter pass rate this week]
- Options structure: credit vertical spreads (bull put / bear call),
  ~20-30 delta short strike, $5-wide, 7-14 DTE.
- Autonomous decision layer: [N] LLM decision calls this week, [N] resulted
  in a trade, [N] in a deliberate skip — include 2-3 real `reasoning`
  strings pulled from the `cycles` table as examples.

## Risk gates

- Max loss per spread: 2% of equity (~$[X] at $100k).
- Daily loss circuit breaker: -3%, [did it trigger this week? Y/N]
- Max concurrent spreads: 5.
- DTE window: 7-14 days, so every position resolves within or just past
  the judging window.
- [Any gate that actually fired this week — a rejected candidate is good
  evidence the gates are real, not decorative]

## Alpaca infrastructure

- 100% of options reads/orders via [Alpaca's official MCP
  server](https://github.com/alpacahq/alpaca-mcp-server) — `get_option_chain`,
  `get_option_snapshot`, `place_option_order`. Never the raw SDK for
  anything options-related.
- Screening/signal generation reuses a real, independently-running
  equities trading system's tested code (400+ prior live paper cycles),
  translated to an options structure for this project.
- Scheduled ~every 30min during market hours; [N] cycles run, [N] errors
  (link the specific error if any, and what happened as a result).

## Results (fill in from the real account before submitting)

- Starting equity: $100,000 ([date])
- Ending equity: $[X] ([date])
- Total spreads opened: [N] — [N] closed profitable, [N] closed at a loss,
  [N] still open at submission time
- Alpaca paper account ID: [account ID, required for judging]

## Honest scope notes

- [Anything that didn't work as intended, any manual intervention, any gap
  between design and what actually ran this week — this section exists
  specifically so it doesn't get skipped under deadline pressure.]
