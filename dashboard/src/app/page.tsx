'use client';

import { useEffect, useState } from 'react';
import { Shell } from '@/components/Shell';
import { EquitySparkline } from '@/components/EquitySparkline';
import { KpiRow } from '@/components/KpiRow';
import { RiskGatesPanel } from '@/components/RiskGatesPanel';
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
    contracts: number;
    credit_received: number;
    max_loss: number;
    status: string;
    realized_pnl: number | null;
    opened_at: string;
    closed_at: string | null;
  }>;
  cycles: Array<{
    id: number;
    ran_at: string;
    decision: string | null;
    reasoning: string | null;
    error: string | null;
  }>;
  equityCurve: Array<{ equity: number; snapshot_at: string }>;
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

  const { latestSnapshot, spreads, cycles, equityCurve } = state;
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
                <p className="text-2xl font-bold">${latestSnapshot.equity.toLocaleString()}</p>
              </div>
              {latestSnapshot.daily_pl !== null && (
                <p className={latestSnapshot.daily_pl >= 0 ? 'text-emerald-400' : 'text-red-400'}>
                  {latestSnapshot.daily_pl >= 0 ? '+' : ''}
                  ${latestSnapshot.daily_pl.toFixed(2)}
                  {latestSnapshot.daily_pl_pct !== null &&
                    ` (${(latestSnapshot.daily_pl_pct * 100).toFixed(2)}%)`}
                </p>
              )}
            </div>
            <EquitySparkline points={equityCurve} />
            <p className="text-xs text-gray-500 mt-2">
              {latestSnapshot.open_spreads_count} open spread{latestSnapshot.open_spreads_count === 1 ? '' : 's'} ·
              last updated {new Date(latestSnapshot.snapshot_at).toLocaleString()}
            </p>
          </section>

          <section className="mb-4">
            <h2 className="text-sm font-semibold text-gray-300 mb-2">Open spreads ({openSpreads.length})</h2>
            {openSpreads.length === 0 ? (
              <p className="text-sm text-gray-500">No open positions right now.</p>
            ) : (
              <div className="space-y-2">
                {openSpreads.map((s) => (
                  <div key={s.id} className="rounded-lg border border-gray-800 bg-gray-900/30 p-3 text-sm">
                    <div className="flex justify-between">
                      <span className="font-semibold">
                        {s.underlying} {s.direction === 'bull_put' ? 'bull put' : 'bear call'}
                      </span>
                      <span className="text-gray-500">exp {s.expiration}</span>
                    </div>
                    <p className="text-gray-400 text-xs mt-1">
                      short ${s.short_strike} / long ${s.long_strike} × {s.contracts} — credit $
                      {Number(s.credit_received).toFixed(2)}, max loss ${Number(s.max_loss).toFixed(2)}
                    </p>
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
                      <span>
                        {s.underlying} {s.direction === 'bull_put' ? 'bull put' : 'bear call'} — {s.status}
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
