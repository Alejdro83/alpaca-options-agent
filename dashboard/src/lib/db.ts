// Server-only — direct Postgres against the same Supabase project Agent
// Bazaar uses, isolated in its own `alpaca_hackathon` schema (see
// alpaca-options-agent's db.py for why this goes through Postgres directly
// rather than PostgREST: that schema isn't in the project's "exposed
// schemas" list, and this avoids needing that dashboard setting changed).
//
// This file must never be imported from a Client Component — the DB
// credentials live in server-only env vars, read only inside the
// `/api/state` Route Handler.
import { Pool } from 'pg';

let pool: Pool | null = null;

function getPool(): Pool {
  if (!pool) {
    pool = new Pool({
      host: process.env.SUPABASE_DB_HOST,
      port: Number(process.env.SUPABASE_DB_PORT || 5432),
      database: process.env.SUPABASE_DB_NAME || 'postgres',
      user: process.env.SUPABASE_DB_USER,
      password: process.env.SUPABASE_DB_PASSWORD,
      ssl: { rejectUnauthorized: false },
      max: 3,
    });
  }
  return pool;
}

const SCHEMA = process.env.SUPABASE_SCHEMA || 'alpaca_hackathon';

export interface AccountSnapshot {
  equity: number;
  last_equity: number | null;
  cash: number | null;
  open_spreads_count: number;
  daily_pl: number | null;
  daily_pl_pct: number | null;
  spy_price: number | null;
  snapshot_at: string;
}

export interface Spread {
  id: number;
  underlying: string;
  direction: string;
  expiration: string;
  short_strike: number;
  long_strike: number;
  short_symbol: string | null;
  long_symbol: string | null;
  // 'credit' (default) | 'debit' (alpaca_hackathon_schema_debit.sql, 2026-09-02
  // debit-spread overlay). For 'debit', credit_received is NEGATIVE (the
  // debit paid) and short_strike/short_symbol mean the leg the bot BOUGHT.
  structure: string;
  contracts: number;
  credit_received: number;
  max_loss: number;
  status: string;
  realized_pnl: number | null;
  opened_at: string;
  closed_at: string | null;
  // Iron condor support (alpaca_hackathon_schema_iron_condor.sql, applied
  // 2026-08-28). strategy is 'vertical' | 'iron_condor'. For a vertical
  // spread, short_strike/long_strike/short_symbol/long_symbol above are the
  // only side and the call_* columns are null. For an iron condor,
  // short_strike/long_strike above are specifically the PUT side and
  // call_short_strike/call_long_strike are the CALL side — all four
  // populated. No iron_condor rows exist yet; the backend that creates them
  // is still in progress on a separate branch.
  strategy: string;
  call_short_strike: number | null;
  call_long_strike: number | null;
  call_short_symbol: string | null;
  call_long_symbol: string | null;
}

export interface Cycle {
  id: number;
  ran_at: string;
  decision: string | null;
  reasoning: string | null;
  error: string | null;
}

export interface PortfolioGreeksSnapshot {
  net_delta: number | null;
  net_gamma: number | null;
  net_theta: number | null;
  net_vega: number | null;
  net_rho: number | null;
  beta_weighted_delta: number | null;
  per_spread: Array<{
    spread_id: number;
    underlying: string;
    strategy: string;
    delta: number;
    gamma: number;
    theta: number;
    vega: number;
    rho: number;
    beta: number | null;
    beta_weighted_delta: number | null;
  }>;
  snapshot_at: string;
}

export interface ShadowPolicySummary {
  policy: string;
  realized: number;
  open_count: number;
  closed_count: number;
  win_rate: number;
}

export interface ShadowPnlPoint {
  policy: string;
  pnl: number;
  closed_at: string;
}

export interface LlmPnlPoint {
  pnl: number;
  closed_at: string;
}

export async function getDashboardState() {
  const client = await getPool().connect();
  try {
    const [snapshot, spreads, cycles] = await Promise.all([
      client.query<AccountSnapshot>(
        `select equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct, spy_price, snapshot_at
         from ${SCHEMA}.account_snapshots order by snapshot_at desc limit 1`
      ),
      client.query<Spread>(
        `select id, underlying, direction, expiration, short_strike, long_strike,
                short_symbol, long_symbol, strategy, structure,
                call_short_strike, call_long_strike, call_short_symbol, call_long_symbol,
                contracts, credit_received, max_loss, status, realized_pnl, opened_at, closed_at
         from ${SCHEMA}.spreads order by opened_at desc limit 50`
      ),
      client.query<Cycle>(
        `select id, ran_at, decision, reasoning, error
         from ${SCHEMA}.cycles order by ran_at desc limit 20`
      ),
      // Equity curve for the chart — every snapshot, not just the latest.
    ]);
    const curve = await client.query<Pick<AccountSnapshot, 'equity' | 'spy_price' | 'snapshot_at'>>(
      `select equity, spy_price, snapshot_at from ${SCHEMA}.account_snapshots order by snapshot_at asc`
    );

    // Shadow book (2026-08-29) -- per-policy aggregate summaries + P&L time
    // series for the mechanical-rule and random counterfactual policies,
    // plus the real LLM/book's own series for the same chart. Wrapped in
    // try/catch: shadow_positions is a brand-new table, so a dashboard
    // deployed slightly ahead of its migration must still render everything
    // else.
    let shadowSummaries: ShadowPolicySummary[] = [];
    let shadowPnlSeries: ShadowPnlPoint[] = [];
    let llmPnlSeries: LlmPnlPoint[] = [];
    try {
      const summaryResult = await client.query<ShadowPolicySummary>(
        `select
           policy,
           coalesce(sum(realized_pnl) filter (where status != 'open'), 0) as realized,
           count(*) filter (where status = 'open') as open_count,
           count(*) filter (where status != 'open') as closed_count,
           coalesce(
             count(*) filter (where status = 'closed_profit')::float /
             nullif(count(*) filter (where status != 'open'), 0),
             0
           ) as win_rate
         from ${SCHEMA}.shadow_positions
         group by policy`
      );
      shadowSummaries = summaryResult.rows;

      const pnlResult = await client.query<ShadowPnlPoint>(
        `select policy, realized_pnl as pnl, closed_at
         from ${SCHEMA}.shadow_positions
         where status != 'open' and realized_pnl is not null
         order by closed_at asc`
      );
      shadowPnlSeries = pnlResult.rows;

      const llmResult = await client.query<LlmPnlPoint>(
        `select realized_pnl as pnl, closed_at
         from ${SCHEMA}.spreads
         where status != 'open' and realized_pnl is not null
         order by closed_at asc`
      );
      llmPnlSeries = llmResult.rows;
    } catch {
      // shadow_positions table may not exist yet -- non-fatal
    }

    // Real portfolio Greeks (2026-08-29) -- monitoring only, see
    // portfolio_greeks.py. Same defensive try/catch as shadow book above:
    // a dashboard deployed ahead of the migration must still render
    // everything else.
    let portfolioGreeks: PortfolioGreeksSnapshot | null = null;
    try {
      const greeksResult = await client.query<PortfolioGreeksSnapshot>(
        `select net_delta, net_gamma, net_theta, net_vega, net_rho, beta_weighted_delta, per_spread, snapshot_at
         from ${SCHEMA}.portfolio_greeks_snapshots
         order by snapshot_at desc limit 1`
      );
      portfolioGreeks = greeksResult.rows[0] ?? null;
    } catch {
      // portfolio_greeks_snapshots table may not exist yet -- non-fatal
    }

    return {
      latestSnapshot: snapshot.rows[0] ?? null,
      spreads: spreads.rows,
      cycles: cycles.rows,
      equityCurve: curve.rows,
      portfolioGreeks,
      shadowBook: {
        summaries: shadowSummaries,
        shadowPnlSeries,
        llmPnlSeries,
      },
    };
  } finally {
    client.release();
  }
}

// --- Backtest ablation lab (2026-08-29) -- see backtest_ablation.py -------

export interface AblationConfigRow {
  config: string;
  n_trades: number;
  total_pnl: number;
  avg_pnl: number | null;
  win_rate: number | null;
  max_drawdown: number | null;
  run_at: string;
}

export interface AblationTradeRow {
  config: string;
  symbol: string | null;
  direction: string | null;
  entry_date: string | null;
  exit_date: string | null;
  credit: number | null;
  pnl: number | null;
  exit_reason: string | null;
}

export async function getAblationState() {
  const client = await getPool().connect();
  try {
    // Only the most recent run -- config rows share one run_at per batch
    // (see backtest_ablation.py's _record_run). Real bug caught while
    // wiring this up: fetching run_at into JS then passing it back as a
    // query param round-trips through a JS Date object, which only has
    // millisecond precision -- Postgres' timestamptz has microseconds, so
    // the re-serialized value silently stopped matching any row and this
    // always returned empty. Fixed by scoping both queries with a
    // same-query subquery instead, so the timestamp never leaves Postgres.
    const summary = await client.query<AblationConfigRow>(
      `select config, n_trades, total_pnl, avg_pnl, win_rate, max_drawdown, run_at
       from ${SCHEMA}.backtest_ablation
       where run_at = (select max(run_at) from ${SCHEMA}.backtest_ablation)
       order by id asc`
    );
    if (summary.rows.length === 0) {
      return { summary: [] as AblationConfigRow[], trades: [] as AblationTradeRow[] };
    }
    const trades = await client.query<AblationTradeRow>(
      `select config, symbol, direction, entry_date, exit_date, credit, pnl, exit_reason
       from ${SCHEMA}.backtest_ablation_trades
       where run_at = (select max(run_at) from ${SCHEMA}.backtest_ablation)
       order by id asc limit 500`
    );
    return { summary: summary.rows, trades: trades.rows };
  } finally {
    client.release();
  }
}

// --- Three-strategy comparison (2026-08-30) --------------------------------
//
// One project, three independent implementations of the same underlying
// signal/risk backbone: this repo's own LLM+deterministic-gate bot (verticals
// + iron condor), a parallel autonomous-agent experiment ("Paco", built on
// the zeroclaw framework, same risk_gate/regime code imported directly --
// see mcp_risk_proxy/server.py and signals/regime.py), and rookieriot's
// independent build (verticals-only, narrower ETF/large-cap universe). All
// three reset to a fresh $100,000 paper account on 2026-08-30 specifically
// so this comparison starts from the same baseline once real trading begins
// 2026-08-31.
//
// Paco's account is NOT eligible for hackathon judging (it's a repurposed
// account, not brand-new-and-dedicated) -- it is presented here purely as a
// research comparison, never as a substitute for this repo's own judged
// account (`getDashboardState` above, schema `alpaca_hackathon`).

const PACO_SCHEMA = 'zeroclaw_trading';
const ROOKIERIOT_STATE_URL = 'https://alpaca-trading-rookieriot.vercel.app/api/state';

export interface PacoState {
  latestSnapshot: {
    equity: number;
    daily_pnl: number | null;
    snapshot_at: string;
  } | null;
  equityCurve: Array<{ equity: number; spy_price: number | null; snapshot_at: string }>;
  openCount: number;
  strategyMix: Array<{ strategy: string; structure: string; count: number }>;
  portfolioGreeks: PortfolioGreeksSnapshot | null;
}

async function getPacoState(): Promise<PacoState> {
  const client = await getPool().connect();
  try {
    const snapshot = await client.query(
      `select equity, daily_pnl, ts as snapshot_at
       from ${PACO_SCHEMA}.account_snapshots order by ts desc limit 1`
    );
    // spy_price added 2026-09-01 (Alex noticed Paco's curve had no dashed
    // SPY overlay like the judged bot's -- it wasn't a missing feature,
    // account_snapshot_paco.py just started recording this column).
    const curve = await client.query(
      `select equity, spy_price, ts as snapshot_at
       from ${PACO_SCHEMA}.account_snapshots order by ts asc`
    );
    const openCountResult = await client.query(
      `select count(*)::int as n from ${PACO_SCHEMA}.spreads where status = 'open'`
    );
    // structure added 2026-09-01 (Paco can now build directional debit
    // spreads alongside its usual credit ones -- without this the
    // dashboard couldn't tell them apart, both just say "vertical").
    // strategyMix itself was already being fetched but never actually
    // rendered anywhere -- fixed in StrategyComparisonPanel at the same time.
    const mixResult = await client.query(
      `select strategy, structure, count(*)::int as count from ${PACO_SCHEMA}.spreads
       where status = 'open' group by strategy, structure`
    );
    let portfolioGreeks: PortfolioGreeksSnapshot | null = null;
    try {
      const greeksResult = await client.query<PortfolioGreeksSnapshot>(
        `select net_delta, net_gamma, net_theta, net_vega, net_rho, beta_weighted_delta, per_spread, snapshot_at
         from ${PACO_SCHEMA}.portfolio_greeks_snapshots order by snapshot_at desc limit 1`
      );
      portfolioGreeks = greeksResult.rows[0] ?? null;
    } catch {
      // table may not exist yet on an older deploy -- non-fatal
    }
    return {
      latestSnapshot: snapshot.rows[0] ?? null,
      equityCurve: curve.rows,
      openCount: openCountResult.rows[0]?.n ?? 0,
      strategyMix: mixResult.rows,
      portfolioGreeks,
    };
  } finally {
    client.release();
  }
}

export interface RookieriotState {
  latestSnapshot: {
    equity: number;
    daily_pl: number | null;
    snapshot_at: string;
  } | null;
  equityCurve: Array<{ equity: number; spy_price: number | null; snapshot_at: string }>;
  openCount: number;
}

async function getRookieriotState(): Promise<RookieriotState | null> {
  // Their own public read-only endpoint -- same shape this repo's own
  // /api/state exposes, no shared credentials needed. Best-effort: their
  // deploy being down must never break this dashboard's own page.
  try {
    const res = await fetch(ROOKIERIOT_STATE_URL, { next: { revalidate: 0 } });
    if (!res.ok) return null;
    const data = await res.json();
    return {
      latestSnapshot: data.latestSnapshot
        ? {
            equity: Number(data.latestSnapshot.equity),
            daily_pl: data.latestSnapshot.daily_pl !== null ? Number(data.latestSnapshot.daily_pl) : null,
            snapshot_at: data.latestSnapshot.snapshot_at,
          }
        : null,
      equityCurve: (data.equityCurve ?? []).map((p: { equity: string; spy_price: string | null; snapshot_at: string }) => ({
        equity: Number(p.equity),
        spy_price: p.spy_price !== null ? Number(p.spy_price) : null,
        snapshot_at: p.snapshot_at,
      })),
      openCount: (data.spreads ?? []).filter((s: { status: string }) => s.status === 'open').length,
    };
  } catch {
    return null;
  }
}

export async function getCompareState() {
  const [ours, paco, rookieriot] = await Promise.all([
    getDashboardState(),
    getPacoState().catch(() => null),
    getRookieriotState(),
  ]);
  return { ours, paco, rookieriot };
}
