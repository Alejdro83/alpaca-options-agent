'use client';

import { useEffect, useState } from 'react';
import { Shell } from '@/components/Shell';

interface AblationConfigRow {
  config: string;
  n_trades: number;
  total_pnl: number;
  avg_pnl: number | null;
  win_rate: number | null;
  max_drawdown: number | null;
  run_at: string;
}

interface AblationTradeRow {
  config: string;
  symbol: string | null;
  direction: string | null;
  entry_date: string | null;
  exit_date: string | null;
  credit: number | null;
  pnl: number | null;
  exit_reason: string | null;
}

interface LabState {
  summary: AblationConfigRow[];
  trades: AblationTradeRow[];
}

// Incremental backtest ablation lab (2026-08-29) -- see backtest_ablation.py.
// Same honest-scope caveats as that script: Black-Scholes-simulated spread
// economics on real historical bars/signals/filters, not real historical
// option-chain prices -- a consistency check between our own filter choices,
// not a market-realistic backtest. Answers "which filter is actually earning
// its keep, and does the final rule beat trading on schedule at random?".
export default function LabPage() {
  const [state, setState] = useState<LabState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('/api/lab')
      .then((r) => r.json())
      .then((d) => (d.error ? setError(d.error) : setState(d)))
      .catch((e) => setError(String(e)));
  }, []);

  return (
    <Shell>
      <div className="mb-4">
        <a href="/" className="text-xs text-amber-400 hover:underline">
          ← back to dashboard
        </a>
      </div>
      <h2 className="text-lg font-semibold mb-1">Backtest ablation lab</h2>
      <p className="text-xs text-gray-500 mb-4">
        How much does each filter layer actually contribute? Real historical bars/signals/filters,
        Black-Scholes-simulated spread economics (no historical options-chain data available — see
        README.md&apos;s &quot;honest scope notes&quot;). Not a market-realistic backtest — a consistency
        check between our own filter choices.
      </p>

      {error && <p className="text-sm text-red-400">Failed to load: {error}</p>}
      {!error && !state && <p className="text-sm text-gray-500">Loading…</p>}
      {state && state.summary.length === 0 && (
        <p className="text-sm text-gray-500">
          No ablation run recorded yet — run <code className="text-gray-400">backtest_ablation.py</code> once
          to populate this page.
        </p>
      )}

      {state && state.summary.length > 0 && (
        <>
          <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 border-b border-gray-800">
                  <th className="pb-2 pr-3">Config</th>
                  <th className="pb-2 pr-3 text-right">Trades</th>
                  <th className="pb-2 pr-3 text-right">Total P&amp;L</th>
                  <th className="pb-2 pr-3 text-right">Avg P&amp;L</th>
                  <th className="pb-2 pr-3 text-right">Win %</th>
                  <th className="pb-2 text-right">Max DD</th>
                </tr>
              </thead>
              <tbody>
                {state.summary.map((r) => (
                  <tr key={r.config} className="border-b border-gray-900">
                    <td className="py-2 pr-3 font-medium">{r.config}</td>
                    <td className="py-2 pr-3 text-right text-gray-400">{r.n_trades}</td>
                    <td className={`py-2 pr-3 text-right font-semibold ${Number(r.total_pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                      {Number(r.total_pnl) >= 0 ? '+' : ''}${Number(r.total_pnl).toFixed(2)}
                    </td>
                    <td className="py-2 pr-3 text-right text-gray-400">
                      {r.avg_pnl !== null ? `$${Number(r.avg_pnl).toFixed(2)}` : '—'}
                    </td>
                    <td className="py-2 pr-3 text-right text-gray-400">
                      {r.win_rate !== null ? `${(Number(r.win_rate) * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="py-2 text-right text-gray-400">
                      {r.max_drawdown !== null ? `$${Number(r.max_drawdown).toFixed(2)}` : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-xs text-gray-500 mt-3">
              Run at {new Date(state.summary[0].run_at).toLocaleString()}
            </p>
          </section>

          <section className="mb-4">
            <h3 className="text-sm font-semibold text-gray-300 mb-2">Sample trades ({state.trades.length})</h3>
            <div className="rounded-xl border border-gray-800 bg-gray-900/40 overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-gray-900">
                  <tr className="text-left text-gray-500 border-b border-gray-800">
                    <th className="p-2">Config</th>
                    <th className="p-2">Symbol</th>
                    <th className="p-2">Dir</th>
                    <th className="p-2">Entry</th>
                    <th className="p-2">Exit</th>
                    <th className="p-2 text-right">Credit</th>
                    <th className="p-2 text-right">P&amp;L</th>
                    <th className="p-2">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {state.trades.map((t, i) => (
                    <tr key={i} className="border-b border-gray-900">
                      <td className="p-2 text-gray-400">{t.config}</td>
                      <td className="p-2">{t.symbol}</td>
                      <td className="p-2 text-gray-400">{t.direction}</td>
                      <td className="p-2 text-gray-400">{t.entry_date}</td>
                      <td className="p-2 text-gray-400">{t.exit_date}</td>
                      <td className="p-2 text-right text-gray-400">
                        {t.credit !== null ? `$${Number(t.credit).toFixed(2)}` : '—'}
                      </td>
                      <td className={`p-2 text-right ${t.pnl !== null && Number(t.pnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {t.pnl !== null ? `${Number(t.pnl) >= 0 ? '+' : ''}$${Number(t.pnl).toFixed(2)}` : '—'}
                      </td>
                      <td className="p-2 text-gray-500">{t.exit_reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </Shell>
  );
}
