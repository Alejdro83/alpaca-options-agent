'use client';

import { EquitySparkline } from './EquitySparkline';

interface Track {
  name: string;
  subtitle: string;
  equity: number | null;
  dailyPl: number | null;
  openCount: number | null;
  curve: Array<{ equity: number; spy_price?: number | null; snapshot_at: string }>;
  greeks: {
    net_delta: number | null;
    net_theta: number | null;
    net_vega: number | null;
    beta_weighted_delta: number | null;
  } | null;
  unavailable?: string;
}

function fmtMoney(v: number | null): string {
  if (v === null) return '—';
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtG(v: number | null): string {
  return v === null || v === undefined ? '—' : v.toFixed(3);
}

function TrackCard({ track }: { track: Track }) {
  return (
    <div className="rounded-lg bg-gray-900/60 p-3 flex-1 min-w-[220px]">
      <div className="flex justify-between items-baseline mb-1">
        <p className="text-sm font-semibold text-gray-200">{track.name}</p>
        {track.dailyPl !== null && (
          <span className={`text-xs ${track.dailyPl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {track.dailyPl >= 0 ? '+' : ''}
            {fmtMoney(track.dailyPl)}
          </span>
        )}
      </div>
      <p className="text-[11px] text-gray-600 mb-2">{track.subtitle}</p>

      {track.unavailable ? (
        <p className="text-xs text-gray-600 italic">{track.unavailable}</p>
      ) : (
        <>
          <p className="text-xl font-bold text-white mb-1">{fmtMoney(track.equity)}</p>
          <p className="text-[11px] text-gray-500 mb-2">
            {track.openCount === null ? '—' : `${track.openCount} open position${track.openCount === 1 ? '' : 's'}`}
          </p>
          <EquitySparkline points={track.curve} />
          {track.greeks && (
            <div className="mt-2 pt-2 border-t border-gray-800 flex justify-between text-[11px] text-gray-500">
              <span>Δ {fmtG(track.greeks.net_delta)}</span>
              <span>Θ {fmtG(track.greeks.net_theta)}</span>
              <span>V {fmtG(track.greeks.net_vega)}</span>
              {track.greeks.beta_weighted_delta !== null && (
                <span>βΔ {fmtG(track.greeks.beta_weighted_delta)}</span>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export interface CompareState {
  ours: {
    latestSnapshot: { equity: number; daily_pl: number | null } | null;
    equityCurve: Array<{ equity: number; spy_price: number | null; snapshot_at: string }>;
    spreads: Array<{ status: string }>;
    portfolioGreeks: {
      net_delta: number | null;
      net_theta: number | null;
      net_vega: number | null;
      beta_weighted_delta: number | null;
    } | null;
  };
  paco: {
    latestSnapshot: { equity: number; daily_pnl: number | null } | null;
    equityCurve: Array<{ equity: number; snapshot_at: string }>;
    openCount: number;
    strategyMix: Array<{ strategy: string; count: number }>;
    portfolioGreeks: {
      net_delta: number | null;
      net_theta: number | null;
      net_vega: number | null;
      beta_weighted_delta: number | null;
    } | null;
  } | null;
  rookieriot: {
    latestSnapshot: { equity: number; daily_pl: number | null } | null;
    equityCurve: Array<{ equity: number; spy_price: number | null; snapshot_at: string }>;
    openCount: number;
  } | null;
}

// Three independent implementations of the same underlying signal/risk
// backbone, all reset to a fresh $100,000 paper account on 2026-08-30 so
// this comparison starts from the same baseline once real trading begins:
// this repo's own LLM+deterministic-gate bot (verticals + iron condor, the
// account submitted for judging), a parallel autonomous-agent experiment
// ("Paco" -- zeroclaw framework, same risk_gate/regime code imported
// directly, NOT eligible for judging -- see AGENTS.md), and rookieriot's
// independent build (verticals-only, narrower universe).
export function StrategyComparisonPanel({ data }: { data: CompareState }) {
  const ourOpenCount = data.ours.spreads.filter((s) => s.status === 'open').length;

  const tracks: Track[] = [
    {
      name: 'Ours (judged)',
      subtitle: 'Verticals + iron condor, LLM + deterministic gate',
      equity: data.ours.latestSnapshot ? Number(data.ours.latestSnapshot.equity) : null,
      dailyPl: data.ours.latestSnapshot?.daily_pl !== null && data.ours.latestSnapshot?.daily_pl !== undefined
        ? Number(data.ours.latestSnapshot.daily_pl)
        : null,
      openCount: data.ours.latestSnapshot ? ourOpenCount : null,
      curve: data.ours.equityCurve,
      greeks: data.ours.portfolioGreeks,
    },
    data.paco
      ? {
          name: 'Paco (research)',
          subtitle: 'Autonomous zeroclaw agent, same risk backbone — not judged',
          equity: data.paco.latestSnapshot ? Number(data.paco.latestSnapshot.equity) : null,
          dailyPl: data.paco.latestSnapshot?.daily_pnl ?? null,
          openCount: data.paco.openCount,
          curve: data.paco.equityCurve,
          greeks: data.paco.portfolioGreeks,
        }
      : {
          name: 'Paco (research)',
          subtitle: 'Autonomous zeroclaw agent, same risk backbone — not judged',
          equity: null,
          dailyPl: null,
          openCount: null,
          curve: [],
          greeks: null,
          unavailable: 'Unavailable right now.',
        },
    data.rookieriot
      ? {
          name: 'rookieriot',
          subtitle: 'Independent build, verticals-only, narrower universe',
          equity: data.rookieriot.latestSnapshot ? Number(data.rookieriot.latestSnapshot.equity) : null,
          dailyPl: data.rookieriot.latestSnapshot?.daily_pl ?? null,
          openCount: data.rookieriot.openCount,
          curve: data.rookieriot.equityCurve,
          greeks: null,
        }
      : {
          name: 'rookieriot',
          subtitle: 'Independent build, verticals-only, narrower universe',
          equity: null,
          dailyPl: null,
          openCount: null,
          curve: [],
          greeks: null,
          unavailable: 'Their dashboard is unreachable right now.',
        },
  ];

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4">
      <h2 className="text-sm font-semibold text-gray-300 mb-1">Three strategies, one risk backbone</h2>
      <p className="text-xs text-gray-500 mb-3">
        Same signal/regime code, same deterministic risk gate — three independent decision layers,
        compared side by side. All three reset to a fresh $100,000 account on 2026-08-30; real
        trading starts together on 2026-08-31. Paco&apos;s account is a research experiment, not
        eligible for hackathon judging.
      </p>
      <div className="flex flex-col sm:flex-row gap-3">
        {tracks.map((t) => (
          <TrackCard key={t.name} track={t} />
        ))}
      </div>
    </section>
  );
}
