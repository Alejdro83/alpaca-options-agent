// Pure KPI calculations over the same `spreads` array page.tsx already
// fetches — no new API calls, just aggregation the dashboard wasn't doing
// before. Kept separate from page.tsx so the math is testable and the
// component stays about rendering, not arithmetic.

export interface SpreadForStats {
  status: string;
  realized_pnl: number | null;
}

// The hackathon's dedicated account started at exactly $100,000 (verified
// against the real account 2026-08-26) — used as the fixed denominator for
// a "% of starting capital" figure, since equity itself moves as spreads
// close and isn't the right base for that specific number.
export const STARTING_EQUITY = 100_000;

export interface KpiSummary {
  totalRealizedPnl: number;
  totalRealizedPnlPct: number;
  closedCount: number;
  openCount: number;
  winRate: number | null; // null when there's nothing closed yet to rate
  avgPnlPerTrade: number | null;
}

export function computeKpis(spreads: SpreadForStats[]): KpiSummary {
  const closed = spreads.filter((s) => s.status !== 'open' && s.realized_pnl !== null);
  const open = spreads.filter((s) => s.status === 'open');
  const totalRealizedPnl = closed.reduce((sum, s) => sum + Number(s.realized_pnl), 0);
  const wins = closed.filter((s) => Number(s.realized_pnl) > 0).length;

  return {
    totalRealizedPnl,
    totalRealizedPnlPct: totalRealizedPnl / STARTING_EQUITY,
    closedCount: closed.length,
    openCount: open.length,
    winRate: closed.length > 0 ? wins / closed.length : null,
    avgPnlPerTrade: closed.length > 0 ? totalRealizedPnl / closed.length : null,
  };
}
