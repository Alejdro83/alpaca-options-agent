# Alpaca Options Credit-Spread Agent — One-Pager

*Draft skeleton — fill in real numbers/screenshots once the account has a
few days of live activity (see plan Day 6-7). Every section below states
what evidence it needs; don't submit until each placeholder is replaced
with something real and verifiable.*

## AI logic

- Underlying selection: [screening/signals summary — liquid universe size,
  which horizon(s) used, trend-filter pass rate this week]
- Options structure: credit vertical spreads (bull put / bear call),
  ~16-18 delta short strike, $5-wide, 10-21 DTE. Delta and DTE were both
  tuned down from more common textbook values (25-30 delta, 30-45 DTE)
  specifically for a ~5-trading-day judged window: research shows 25-30
  delta's higher expected value only plays out over hundreds of trades,
  while a handful of trades in one week is dominated by variance, and 30-45
  DTE resolves well past the contest entirely.
- Entry additionally requires a realized-volatility-percentile filter (a
  proxy for true IV rank — see `config.py`'s `VolatilityFilter` docstring
  for why it's a proxy, not the real thing): only sells premium when the
  underlying's current 20-day ATR% sits at/above the 40th percentile of its
  own trailing year.
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
- Force-close-by-contest-end: any spread still open once expiration is ≤1
  day away, or the contest deadline is within 2 hours, closes unconditionally
  regardless of profit/loss — added specifically so a late-week entry can't
  end the contest open and undemonstrated.
- Per-contract liquidity gate: both legs require a bid-ask spread ≤12% of
  mid, and open interest ≥100 whenever the API actually reports a value
  (confirmed live: Alpaca's free/paper tier returns `open_interest: null`
  even for genuinely liquid, near-the-money SPY contracts — enforced only
  when present rather than silently rejecting almost everything).
- **Known, accepted limitation**: the shared equity screening universe skews
  toward large-cap tech, so several concurrent spreads could end up
  correlated in a broad market move rather than truly diversified — not
  addressed with a hard code change given the timeline, named here instead.

## Alpaca infrastructure

- 100% of options reads/orders via [Alpaca's official MCP
  server](https://github.com/alpacahq/alpaca-mcp-server) —
  `get_option_contracts`, `get_option_snapshot`, `place_option_order`.
  Never the raw SDK for anything options-related.
- No broker-supplied Greeks are available on this account without a paid
  Algo Trader Plus subscription (confirmed live: `feed=opra` 403s with
  "OPRA agreement is not signed"; the free `indicative` feed has no
  `greeks` field at all) — delta is computed in-process via closed-form
  Black-Scholes, using realized volatility as the implied-vol proxy.
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
