'use client';

interface PolicySummary {
  policy: string;
  realized: number;
  open_count: number;
  closed_count: number;
  win_rate: number;
}

interface PnlPoint {
  policy: string;
  pnl: number;
  closed_at: string;
}

interface LlmPnlPoint {
  pnl: number;
  closed_at: string;
}

function cumulativeSeries(points: { pnl: number }[]): number[] {
  const result: number[] = [];
  let sum = 0;
  for (const p of points) {
    sum += Number(p.pnl);
    result.push(sum);
  }
  return result;
}

function buildPath(values: number[], width: number, height: number, min: number, max: number): string {
  if (values.length < 2) return '';
  const range = max - min || 1;
  const step = width / (values.length - 1);
  return values
    .map((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / range) * height;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
}

// Exit-rule counterfactuals (2026-08-29, research pass): mirrors the LLM's
// REAL picks (same underlying/strike/credit/contracts as bot.py's actual
// order) under two alternative EXIT rules -- llm_tight_stop (1x credit
// stop) and llm_no_stop (no stop-loss trigger, only profit target/force-
// close). Isolates a single-source-but-concrete research finding (tight or
// no stop beat a middle multiple like our real 2x, for short-DTE credit
// spreads) without touching the real book's own exits -- same
// let-real-data-decide approach as the shadow/random selection comparison.
export function ExitRuleComparisonPanel({
  summaries,
  policySeries,
  llmSeries,
}: {
  summaries: PolicySummary[];
  policySeries: PnlPoint[];
  llmSeries: LlmPnlPoint[];
}) {
  const tightStopPoints = policySeries.filter((p) => p.policy === 'llm_tight_stop');
  const noStopPoints = policySeries.filter((p) => p.policy === 'llm_no_stop');

  if (tightStopPoints.length === 0 && noStopPoints.length === 0) {
    return null;
  }

  const llmCum = cumulativeSeries(llmSeries);
  const tightCum = cumulativeSeries(tightStopPoints);
  const noStopCum = cumulativeSeries(noStopPoints);
  const llmTotal = llmCum.length ? llmCum[llmCum.length - 1] : 0;

  const allValues = [...llmCum, ...tightCum, ...noStopCum];
  const byPolicy = Object.fromEntries(summaries.map((s) => [s.policy, s]));
  const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;

  const parts: string[] = [];
  if (llmSeries.length > 0) parts.push(`Real (2x stop): ${fmt(llmTotal)}`);
  if (byPolicy['llm_tight_stop']) parts.push(`Tight (1x): ${fmt(Number(byPolicy['llm_tight_stop'].realized))}`);
  if (byPolicy['llm_no_stop']) parts.push(`No stop: ${fmt(Number(byPolicy['llm_no_stop'].realized))}`);

  if (allValues.length < 2) {
    return (
      <section className="mb-4">
        <h2 className="text-sm font-semibold text-gray-300 mb-2">Exit rule comparison</h2>
        <p className="text-sm text-gray-500">Not enough closed trades yet for a P&L chart.</p>
        {parts.length > 0 && <p className="text-xs text-gray-500 mt-2">{parts.join(' | ')}</p>}
      </section>
    );
  }

  const min = Math.min(...allValues, 0);
  const max = Math.max(...allValues, 0);
  const width = 600;
  const height = 120;

  const llmPath = buildPath(llmCum, width, height, min, max);
  const tightPath = buildPath(tightCum, width, height, min, max);
  const noStopPath = buildPath(noStopCum, width, height, min, max);

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4">
      <h2 className="text-sm font-semibold text-gray-300 mb-2">
        Exit rule comparison — same picks, different stop
      </h2>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-24" preserveAspectRatio="none">
        {llmPath && <path d={llmPath} fill="none" stroke="#34d399" strokeWidth={2} />}
        {tightPath && <path d={tightPath} fill="none" stroke="#818cf8" strokeWidth={1.5} strokeDasharray="6,3" />}
        {noStopPath && <path d={noStopPath} fill="none" stroke="#fb923c" strokeWidth={1.5} strokeDasharray="2,3" />}
      </svg>
      <div className="flex items-center gap-4 mt-1 text-xs text-gray-400">
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-emerald-400" /> Real (2x)
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-indigo-400 border-dashed" /> Tight (1x)
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-orange-400 border-dashed" /> No stop
        </span>
      </div>
      {parts.length > 0 && <p className="text-xs text-gray-500 mt-2">{parts.join(' | ')}</p>}
    </section>
  );
}
