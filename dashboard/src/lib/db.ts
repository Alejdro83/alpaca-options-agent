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

export async function getDashboardState() {
  const client = await getPool().connect();
  try {
    const [snapshot, spreads, cycles] = await Promise.all([
      client.query<AccountSnapshot>(
        `select equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct, snapshot_at
         from ${SCHEMA}.account_snapshots order by snapshot_at desc limit 1`
      ),
      client.query<Spread>(
        `select id, underlying, direction, expiration, short_strike, long_strike,
                short_symbol, long_symbol, strategy,
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
    const curve = await client.query<Pick<AccountSnapshot, 'equity' | 'snapshot_at'>>(
      `select equity, snapshot_at from ${SCHEMA}.account_snapshots order by snapshot_at asc`
    );

    return {
      latestSnapshot: snapshot.rows[0] ?? null,
      spreads: spreads.rows,
      cycles: cycles.rows,
      equityCurve: curve.rows,
    };
  } finally {
    client.release();
  }
}
