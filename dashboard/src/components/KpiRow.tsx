import { type KpiSummary } from '@/lib/stats';

// Four stat tiles — label always in muted text, the number in primary
// text, and color reserved for the one figure that's inherently signed
// (P&L) so it never doubles as decoration on the others.
function Tile({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'good' | 'bad' | 'neutral' }) {
  const valueColor = tone === 'good' ? 'text-emerald-400' : tone === 'bad' ? 'text-red-400' : 'text-white';
  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900/30 p-3">
      <p className="text-[11px] uppercase tracking-wide text-gray-500">{label}</p>
      <p className={`text-lg font-bold ${valueColor}`}>{value}</p>
      {sub && <p className="text-[11px] text-gray-500 mt-0.5">{sub}</p>}
    </div>
  );
}

export function KpiRow({ kpis }: { kpis: KpiSummary }) {
  const pnlTone = kpis.totalRealizedPnl > 0 ? 'good' : kpis.totalRealizedPnl < 0 ? 'bad' : 'neutral';
  const pnlSign = kpis.totalRealizedPnl >= 0 ? '+' : '';

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-4">
      <Tile
        label="P&L realizado"
        value={`${pnlSign}$${kpis.totalRealizedPnl.toFixed(2)}`}
        sub={`${pnlSign}${(kpis.totalRealizedPnlPct * 100).toFixed(2)}% del capital inicial`}
        tone={pnlTone}
      />
      <Tile
        label="Win rate"
        value={kpis.winRate === null ? '—' : `${(kpis.winRate * 100).toFixed(0)}%`}
        sub={kpis.closedCount > 0 ? `${kpis.closedCount} cerrada${kpis.closedCount === 1 ? '' : 's'}` : 'sin cierres aún'}
      />
      <Tile
        label="Operaciones"
        value={String(kpis.closedCount + kpis.openCount)}
        sub={`${kpis.openCount} abierta${kpis.openCount === 1 ? '' : 's'} · ${kpis.closedCount} cerrada${kpis.closedCount === 1 ? '' : 's'}`}
      />
      <Tile
        label="P&L medio / trade"
        value={kpis.avgPnlPerTrade === null ? '—' : `${kpis.avgPnlPerTrade >= 0 ? '+' : ''}$${kpis.avgPnlPerTrade.toFixed(2)}`}
        tone={kpis.avgPnlPerTrade === null ? 'neutral' : kpis.avgPnlPerTrade > 0 ? 'good' : kpis.avgPnlPerTrade < 0 ? 'bad' : 'neutral'}
      />
    </div>
  );
}
