# Alpaca Options Credit-Spread & Iron-Condor Agent — One-Pager

*lablab.ai × Alpaca "AI Trading Agents" Hackathon — submission write-up.
Results section is a placeholder: the judged account was reset to a fresh
$100,000 paper account on 2026-08-30 (see "Honest scope notes") and needs
real trading days to accumulate before it can be filled with real numbers.*

## AI logic

- **Screening → regime → structure.** A liquid ~500-ticker universe (reused,
  tested code from an independently-run equities system) feeds a trend
  filter (EMA/ADX), then a 4-regime classifier (`signals/regime.py`, ADX vs
  25 and 20d/60d realized-vol ratio vs 1.5x):
  - TRENDING / VOLATILE_TRENDING → directional **credit vertical** (bull put
    on a long signal, bear call on a short signal).
  - RANGING → non-directional **iron condor** (put spread + call spread,
    same expiration/width) — candidates with no real trend backing them are
    routed here instead of forced into a directional bet or discarded.
  - VOLATILE_RANGING → skip (elevated vol with no trend — the conservative
    cell of the matrix, deliberately not traded).
- **Entry filter.** A realized-volatility-percentile check (20-day ATR% at
  or above the 40th percentile of its own trailing year) — an explicit
  *proxy* for IV rank, labeled as such everywhere it's surfaced, since this
  account has no broker-supplied implied-vol data (see infra section). A
  VIXY-percentile overlay does the same job at the market-wide level (real
  `^VIX` isn't available via Alpaca's data API). Scheduled macro-event
  blackout windows (JOLTS, NFP) block new entries around known high-impact
  releases without touching exits.
- **LLM decision layer** (mimo-v2.5-pro, OpenAI-compatible endpoint) sits
  *on top of* the deterministic risk gate below, not instead of it — it only
  ever sees candidates that already passed every hard check, and chooses
  which (if any) to act on and how to size within the remaining
  concurrent-spread budget. Every candidate carries short `fact_ids` (e.g.
  `AAPL_CREDIT_EST`) and the model is required to cite them (`[FACT_ID]`)
  for every number in its reasoning — uncited or unknown citations are
  logged, making the reasoning auditable rather than trusted at face value.
- **Shadow book.** Every real cycle also runs a mechanical rule-based
  policy and a matched-rate random policy against the *same* gate-approved
  candidates, tracked as virtual positions with real mark-to-market P&L —
  so the LLM's actual value-add is measurable against a naive baseline and
  a coin flip, not just asserted.

## Risk gates (hard backstop in code — the LLM cannot override any of this)

- Per-spread max loss ≤ 2% of equity; daily loss circuit breaker at -3%;
  max 5 concurrent spreads overall, max 2 concurrent iron condors with a
  separate 30%-of-equity aggregate cap (4-leg structures consume more
  liquidity/concentration budget per position than a vertical).
- DTE window enforced on every entry (currently 7-14, see scope note below
  on an unresolved internal disagreement with our own backtest).
- Per-underlying concentration cap (20% of equity) **and** a
  correlation-cluster cap (40%, mega-cap tech / big banks / energy majors)
  — the cluster cap exists specifically because several "different" names
  can still be one correlated bet in a stress move.
- Per-leg liquidity gate (bid-ask ≤ 12% of mid, open interest ≥ 100 when the
  feed reports a value) and a fresh-quote re-check immediately before every
  order — a missing or stale quote timestamp blocks the trade rather than
  being treated as "fine" (fail-closed, not fail-open).
- Entries and exits use **marketable limit orders**, never unbounded market
  orders — accepts no worse than 10% slippage off the just-checked
  credit/debit, with real fill confirmation (poll up to 45s; an order that
  never fills is cancelled and the cycle fails rather than leaving a
  position open at an unvalidated price).
- `should_force_close`: an unconditional exit once DTE ≤ 1 or the contest
  deadline is within 2 hours, independent of profit/loss — so a late-week
  entry can't end the contest open and undemonstrated.
- Independent broker-vs-database **reconciliation** every cycle — compares
  real Alpaca option legs against the local position ledger by symbol,
  quantity, *and* side (not just "does this symbol exist somewhere"), and
  blocks new entries on any mismatch until a human resolves it.
- Manual (`emergency_flatten.py`) and automatic (`kill_switch.py`) circuit
  breakers, independent of the per-cycle gate above.

## Alpaca infrastructure

- **100% of options reads and writes go through Alpaca's official MCP
  server** (`get_option_contracts`, `get_option_snapshot`,
  `place_option_order`) — the raw SDK is never used for anything
  options-related.
- No broker-supplied Greeks or IV rank are available on this account
  without a paid Algo Trader Plus subscription (confirmed live: OPRA feed
  403s, the free indicative feed has no `greeks` field) — delta is computed
  in-process via closed-form Black-Scholes using realized volatility as the
  IV proxy; portfolio-level Greeks (incl. beta-weighted delta, using a real
  beta computed from daily returns, not a hardcoded table) are aggregated
  the same way for open positions and shown on the dashboard.
- Orchestration via a cron job on an adaptive schedule (2-30 minutes,
  tightening automatically near a stop-loss and backing off when idle and
  quiet, rather than a fixed interval) plus a real-time WebSocket monitor
  for open verticals (iron condors are deliberately excluded from that
  always-on monitor and handled by the cron instead, to avoid a partial-fill/
  partial-close failure mode on a 4-leg structure).
- Supabase/Postgres backing store in its own isolated schema.
- Public dashboard (Next.js, dual web + Telegram Mini App) reading that
  store live: equity curve, open positions, per-cycle reasoning with cited
  facts, the shadow-book comparison, and portfolio Greeks.

## Results

*Placeholder — real numbers go here before the 2026-09-04 15:00 UTC
deadline, once the reset account (see below) has accumulated live trading
days.*

- Starting equity: $100,000 (2026-08-30, reset — see scope note)
- Ending equity: $[X]
- Spreads/iron condors opened: [N] — [N] profitable, [N] loss, [N] still
  open at submission time
- Alpaca paper account ID: `PA36EFWLOWRF`

## Honest scope notes

- **Account reset 2026-08-30**: the account originally used since kickoff
  (28-ago) was replaced with a brand-new $100,000 paper account after
  several risk-gate/parameter changes landed mid-week (limit orders,
  fill confirmation, cluster concentration cap, qty/side-aware
  reconciliation) — we wanted the judged track record measured against the
  final rule set, not a mix of old and new rules. Verified compliant with
  the hackathon's own account rule (a "brand-new, dedicated" account, no
  kickoff-timing requirement in the official text).
- No real IV Rank on this account tier — the realized-volatility-percentile
  filter is a proxy, explicitly labeled as such in code and here, not
  relabeled as the real thing.
- Several thresholds are starting values, not independently backtested
  (correlation-cluster cap, the wider iron-condor width in high-vol-trending
  regimes, the 10% entry-slippage tolerance) — flagged as such in code and
  watched via a nightly dry-run parameter-evolution report (logs what it
  *would* have changed and why, never applies anything automatically) before
  any of them are allowed to move for real.
- The DTE window (7-14) follows an external 3-strategy research spec; our
  own walk-forward backtest found 10-21 outperforms with a large,
  sign-changing difference. Deliberately left as a live, tagged comparison
  (real P&L is recorded per parameter "generation") rather than resolved by
  re-running the backtest a second time.
- Screening universe skews toward large/mid-cap liquid names; the
  correlation-cluster gate mitigates but doesn't eliminate the resulting
  concentration risk.
