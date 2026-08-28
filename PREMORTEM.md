# Pre-mortem — "It's Sep 4 and we failed. What happened?"

Each imagined failure maps to a guard. Status: ☐ open ☑ guarded

| # | Imagined failure | Guard | Status |
|---|---|---|---|
| 1 | LLM picked a spread that the code-level risk gate should have blocked | Two code-level risk gates (eligibility + pre-trade) bracket the LLM — it never argues with either | ☑ |
| 2 | Stale indicative quotes produced bad fill estimates | Pre-trade gate re-fetches quotes before execution; quote-age hard rejection at 15 min | ☑ |
| 3 | Duplicate orders on timeout retry | Idempotent client_order_id per leg (uuid-based) | ☑ |
| 4 | One underlying dominated portfolio losses | Concentration cap: max 20% equity per underlying | ☑ |
| 5 | Bot kept trading after a losing streak blew up the account | Daily loss circuit breaker at -3% halts all new entries | ☑ |
| 6 | Options liquidity was too thin for mid-cap underlyings | Asymmetric liquidity gate: 12% short leg, 25% long leg; OI null passes on indicative feed | ☑ |
| 7 | LLM added no value over a mechanical rule | Shadow ablation baseline runs every cycle; decision journal records both selections for post-hoc comparison | ☑ |
| 8 | Spread monitor missed a profit target between cron runs | WebSocket spread monitor runs persistently, checks should_close on every quote update | ☑ |
| 9 | Position sizing was arbitrary (1 contract always) | Risk-budget sizing: contracts = (equity × max_loss_pct) / max_loss_per_contract | ☑ |
| 10 | Bot opened spreads right before earnings | 2-day earnings blackout filter (yfinance calendar) | ☑ |
| 11 | Rate limits (429) killed a cycle mid-execution | Contract caching reduces MCP calls; retry on 429 with backoff | ☑ |
| 12 | Market gap blew through stop loss overnight | Defined-risk credit spreads: max loss is fixed at entry (width − credit) | ☑ |
| 13 | Hackathon judges asked "how do you know the LLM helps?" | Shadow ablation report (ablation_report.py) with selection overlap + P&L comparison | ☑ |
| 14 | Bot crashed and we couldn't stop it remotely | Kill switch: `python3 kill_switch.py on` pauses all activity via file flag | ☑ |
| 15 | Multi-leg MCP order transmission failed | Day-1 MCP test; fallback to REST if MCP leg array breaks | ☐ |
| 16 | Paper account reset wiped the equity curve mid-week | Never reset the official account; add to runbook | ☐ |
| 17 | Prompt injection in a news headline steered the LLM selector | News is optional context, never touches risk config; LLM output is schema-validated | ☑ |
| 18 | Overnight gap moved underlying past short strike | DTE window [10, 21] keeps expirations far enough from ATM gamma risk | ☑ |
