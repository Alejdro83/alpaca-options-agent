'use client';

interface PerSpreadGreeks {
  spread_id: number;
  underlying: string;
  strategy: string;
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
}

interface PortfolioGreeksSnapshot {
  net_delta: number | null;
  net_gamma: number | null;
  net_theta: number | null;
  net_vega: number | null;
  net_rho: number | null;
  per_spread: PerSpreadGreeks[];
  snapshot_at: string;
}

// Real broker-computed Greeks (2026-08-29) -- NOT the Black-Scholes proxy
// spread_builder.py uses to pick new candidates (Alpaca only returns real
// greeks for contracts already held as positions, confirmed live -- see
// portfolio_greeks.py). Monitoring only.
export function PortfolioGreeksPanel({ data }: { data: PortfolioGreeksSnapshot | null }) {
  if (!data || data.per_spread.length === 0) {
    return null;
  }

  const fmt = (v: number | null, digits = 3) => (v === null ? '—' : Number(v).toFixed(digits));

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4">
      <h2 className="text-sm font-semibold text-gray-300 mb-2">
        Portfolio Greeks — real, broker-computed
      </h2>
      <p className="text-xs text-gray-500 mb-3">
        Net exposure across all open spreads. Only available for held positions (Alpaca's
        indicative feed) — new-candidate selection still uses a realized-vol Black-Scholes proxy.
      </p>
      <div className="grid grid-cols-5 gap-2 text-center mb-3">
        {[
          ['Delta', data.net_delta],
          ['Gamma', data.net_gamma],
          ['Theta', data.net_theta],
          ['Vega', data.net_vega],
          ['Rho', data.net_rho],
        ].map(([label, value]) => (
          <div key={label as string} className="rounded-lg bg-gray-900/60 py-2">
            <p className="text-xs text-gray-500">{label}</p>
            <p className={`text-sm font-semibold ${Number(value) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
              {fmt(value as number | null)}
            </p>
          </div>
        ))}
      </div>
      <div className="space-y-1">
        {data.per_spread.map((s) => (
          <div key={s.spread_id} className="flex justify-between text-xs text-gray-500">
            <span>{s.underlying} ({s.strategy})</span>
            <span>
              Δ {fmt(s.delta)} · Θ {fmt(s.theta)} · V {fmt(s.vega)}
            </span>
          </div>
        ))}
      </div>
      <p className="text-xs text-gray-600 mt-2">
        as of {new Date(data.snapshot_at).toLocaleString()}
      </p>
    </section>
  );
}
