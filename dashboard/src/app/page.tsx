'use client';

import { useEffect, useState } from 'react';
import { Shell } from '@/components/Shell';
import { EquitySparkline } from '@/components/EquitySparkline';
import { ExitRuleComparisonPanel } from '@/components/ExitRuleComparisonPanel';
import { KpiRow } from '@/components/KpiRow';
import { PortfolioGreeksPanel } from '@/components/PortfolioGreeksPanel';
import { RiskGatesPanel } from '@/components/RiskGatesPanel';
import { ShadowBookPanel } from '@/components/ShadowBookPanel';
import { StatusBadges } from '@/components/StatusBadges';
import { computeKpis } from '@/lib/stats';

interface DashboardState {
  latestSnapshot: {
    equity: number;
    last_equity: number | null;
    cash: number | null;
    open_spreads_count: number;
    daily_pl: number | null;
    daily_pl_pct: number | null;
    snapshot_at: string;
  } | null;
  spreads: Array<{
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
    // 'vertical' (existing directional credit spread) or 'iron_condor'.
    // See alpaca_hackathon_schema_iron_condor.sql. For iron_condor rows,
    // short_strike/long_strike/short_symbol/long_symbol above are the PUT
    // side and the call_* fields below are the CALL side.
    strategy: string;
    call_short_strike: number | null;
    call_long_strike: number | null;
    call_short_symbol: string | null;
    call_long_symbol: string | null;
  }>;
  cycles: Array<{
    id: number;
    ran_at: string;
    decision: string | null;
    reasoning: string | null;
    error: string | null;
  }>;
  equityCurve: Array<{ equity: number; spy_price: number | null; snapshot_at: string }>;
  shadowBook: {
    summaries: Array<{
      policy: string;
      realized: number;
      open_count: number;
      closed_count: number;
      win_rate: number;
    }>;
    shadowPnlSeries: Array<{ policy: string; pnl: number; closed_at: string }>;
    llmPnlSeries: Array<{ pnl: number; closed_at: string }>;
  };
  portfolioGreeks: {
    net_delta: number | null;
    net_gamma: number | null;
    net_theta: number | null;
    net_vega: number | null;
    net_rho: number | null;
    per_spread: Array<{
      spread_id: number;
      underlying: string;
      strategy: string;
      delta: number;
      gamma: number;
      theta: number;
      vega: number;
      rho: number;
    }>;
    snapshot_at: string;
  } | null;
}

type SpreadRow = DashboardState['spreads'][number];

// strategy is the canonical field, but a fresh iron_condor row also carries
// direction === 'iron_condor' literally (see alpaca_hackathon_schema_iron_condor.sql)
// instead of 'bull_put'/'bear_call' — checking both is belt-and-suspenders.
function isIronCondor(s: SpreadRow): boolean {
  return s.strategy === 'iron_condor' || s.direction === 'iron_condor';
}

function directionLabel(s: SpreadRow): string {
  if (isIronCondor(s)) return 'Iron Condor';
  return s.direction === 'bull_put' ? 'bull put' : 'bear call';
}

// Small badge so an iron condor row is unmistakable at a glance next to the
// existing directional vertical spreads.
function StrategyBadge({ s }: { s: SpreadRow }) {
  if (!isIronCondor(s)) return null;
  return (
    <span className="inline-flex items-center rounded-full bg-violet-950 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-violet-400">
      4-leg
    </span>
  );
}

// A vertical spread has one short/long pair; an iron condor has two (put
// side using the pre-existing short_strike/long_strike/short_symbol/long_symbol
// columns, call side using the new call_* columns) — show all 4 legs.
function SpreadLegs({ s }: { s: SpreadRow }) {
  if (!isIronCondor(s)) {
    return (
      <p className="text-gray-400 text-xs mt-1">
        short ${s.short_strike} / long ${s.long_strike} × {s.contracts} — credit $
        {Number(s.credit_received).toFixed(2)}, max loss ${Number(s.max_loss).toFixed(2)}
      </p>
    );
  }
  return (
    <div className="text-gray-400 text-xs mt-1 space-y-0.5">
      <p>
        put: short ${s.short_strike}
        {s.short_symbol ? ` (${s.short_symbol})` : ''} / long ${s.long_strike}
        {s.long_symbol ? ` (${s.long_symbol})` : ''}
      </p>
      <p>
        call: short ${s.call_short_strike}
        {s.call_short_symbol ? ` (${s.call_short_symbol})` : ''} / long ${s.call_long_strike}
        {s.call_long_symbol ? ` (${s.call_long_symbol})` : ''}
      </p>
      <p>
        × {s.contracts} — credit ${Number(s.credit_received).toFixed(2)}, max loss $
        {Number(s.max_loss).toFixed(2)}
      </p>
    </div>
  );
}

const POLL_MS = 30_000;

export default function DashboardPage() {
  const [state, setState] = useState<DashboardState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchState = async () => {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) {
          setState(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load');
      }
    };
    fetchState();
    const interval = setInterval(fetchState, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  if (error) {
    return (
      <Shell>
        <p className="text-red-400 text-sm">Failed to load agent state: {error}</p>
      </Shell>
    );
  }

  if (!state) {
    return (
      <Shell>
        <div className="animate-pulse text-gray-500 text-sm">Loading agent state…</div>
      </Shell>
    );
  }

  const { latestSnapshot, spreads, cycles, equityCurve, shadowBook, portfolioGreeks } = state;
  const openSpreads = spreads.filter((s) => s.status === 'open');
  const closedSpreads = spreads.filter((s) => s.status !== 'open');
  const kpis = computeKpis(spreads);

  return (
    <Shell>
      <StatusBadges lastCycleAt={cycles[0]?.ran_at ?? null} />
      {latestSnapshot && <KpiRow kpis={kpis} />}
      {!latestSnapshot ? (
        <p className="text-gray-500 text-sm">
          No account snapshots yet — the agent hasn&apos;t run its first cycle. Check back once the
          hackathon&apos;s dedicated Alpaca account is live and the cron job is enabled.
        </p>
      ) : (
        <>
          <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4">
            <div className="flex justify-between items-baseline mb-2">
              <div>
                <p className="text-xs text-gray-500">Account equity</p>
                <p className="text-2xl font-bold">${Number(latestSnapshot.equity).toLocaleString()}</p>
              </div>
              {latestSnapshot.daily_pl !== null && (
                <p className={Number(latestSnapshot.daily_pl) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                  {Number(latestSnapshot.daily_pl) >= 0 ? '+' : ''}
                  ${Number(latestSnapshot.daily_pl).toFixed(2)}
                  {latestSnapshot.daily_pl_pct !== null &&
                    ` (${(Number(latestSnapshot.daily_pl_pct) * 100).toFixed(2)}%)`}
                </p>
              )}
            </div>
            <EquitySparkline points={equityCurve} />
            {equityCurve.some((p) => p.spy_price != null) && (
              <p className="text-xs text-gray-500 mt-1 flex items-center gap-1.5">
                <span className="inline-block w-3 border-t border-dashed border-gray-400" />
                SPY, same-dated (skill vs market)
              </p>
            )}
            <p className="text-xs text-gray-500 mt-2">
              {latestSnapshot.open_spreads_count} open spread{latestSnapshot.open_spreads_count === 1 ? '' : 's'} ·
              last updated {new Date(latestSnapshot.snapshot_at).toLocaleString()}
            </p>
          </section>

          <ShadowBookPanel data={shadowBook} />

          <ExitRuleComparisonPanel
            summaries={shadowBook.summaries}
            policySeries={shadowBook.shadowPnlSeries}
            llmSeries={shadowBook.llmPnlSeries}
          />

          <PortfolioGreeksPanel data={portfolioGreeks} />

          <section className="mb-4">
            <h2 className="text-sm font-semibold text-gray-300 mb-2">Open spreads ({openSpreads.length})</h2>
            {openSpreads.length === 0 ? (
              <p className="text-sm text-gray-500">No open positions right now.</p>
            ) : (
              <div className="space-y-2">
                {openSpreads.map((s) => (
                  <div key={s.id} className="rounded-lg border border-gray-800 bg-gray-900/30 p-3 text-sm">
                    <div className="flex justify-between">
                      <span className="font-semibold flex items-center gap-1.5">
                        {s.underlying} {directionLabel(s)}
                        <StrategyBadge s={s} />
                      </span>
                      <span className="text-gray-500">exp {s.expiration}</span>
                    </div>
                    <SpreadLegs s={s} />
                  </div>
                ))}
              </div>
            )}
          </section>

          {closedSpreads.length > 0 && (
            <section className="mb-4">
              <h2 className="text-sm font-semibold text-gray-300 mb-2">Closed spreads</h2>
              <div className="space-y-2">
                {closedSpreads.map((s) => (
                  <div key={s.id} className="rounded-lg border border-gray-800 bg-gray-900/20 p-3 text-sm">
                    <div className="flex justify-between">
                      <span className="flex items-center gap-1.5">
                        {s.underlying} {directionLabel(s)} — {s.status}
                        <StrategyBadge s={s} />
                      </span>
                      {s.realized_pnl !== null && (
                        <span className={Number(s.realized_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                          {Number(s.realized_pnl) >= 0 ? '+' : ''}
                          ${Number(s.realized_pnl).toFixed(2)}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </>
      )}

      <RiskGatesPanel />

      <section>
        <h2 className="text-sm font-semibold text-gray-300 mb-2">Recent agent decisions</h2>
        {cycles.length === 0 ? (
          <p className="text-sm text-gray-500">No cycles logged yet.</p>
        ) : (
          <div className="space-y-2">
            {cycles.map((c) => (
              <div key={c.id} className="rounded-lg border border-gray-800 bg-gray-900/20 p-3 text-sm">
                <div className="flex justify-between text-xs text-gray-500 mb-1">
                  <span>{c.decision ?? 'unknown'}</span>
                  <span>{new Date(c.ran_at).toLocaleString()}</span>
                </div>
                {c.error ? (
                  <p className="text-red-400 text-xs">{c.error}</p>
                ) : (
                  <p className="text-gray-300">{c.reasoning}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </Shell>
  );
}
