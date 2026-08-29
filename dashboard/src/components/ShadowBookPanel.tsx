'use client';

interface ShadowPolicySummary {
  policy: string;
  realized: number;
  open_count: number;
  closed_count: number;
  win_rate: number;
}

interface ShadowPnlPoint {
  policy: string;
  pnl: number;
  closed_at: string;
}

interface LlmPnlPoint {
  pnl: number;
  closed_at: string;
}

interface ShadowBookData {
  summaries: ShadowPolicySummary[];
  shadowPnlSeries: ShadowPnlPoint[];
  llmPnlSeries: LlmPnlPoint[];
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

// Shadow book (2026-08-29): the live ablation this dashboard's own
// mechanical rule (_shadow_select) and a random baseline get, alongside
// the LLM's real book -- "did the LLM's judgment add dollars over the
// rule, over random?", same three-policy comparison as backtest_ablation.py's
// offline version but measured on real, live decisions instead of history.
export function ShadowBookPanel({ data }: { data: ShadowBookData }) {
  const { summaries, shadowPnlSeries, llmPnlSeries } = data;

  if (summaries.length === 0 && shadowPnlSeries.length === 0 && llmPnlSeries.length === 0) {
    return null;
  }

  const llmCum = cumulativeSeries(llmPnlSeries);
  const shadowPoints = shadowPnlSeries.filter((p) => p.policy === 'shadow');
  const randomPoints = shadowPnlSeries.filter((p) => p.policy === 'random');
  const shadowCum = cumulativeSeries(shadowPoints);
  const randomCum = cumulativeSeries(randomPoints);
  const llmTotal = llmCum.length ? llmCum[llmCum.length - 1] : 0;

  const allValues = [...llmCum, ...shadowCum, ...randomCum];
  if (allValues.length < 2) {
    return (
      <section className="mb-4">
        <h2 className="text-sm font-semibold text-gray-300 mb-2">Shadow book</h2>
        <p className="text-sm text-gray-500">Not enough closed trades yet for a P&L chart.</p>
        {summaries.length > 0 && <SummaryText summaries={summaries} llmTotal={llmTotal} hasLlmData={llmPnlSeries.length > 0} />}
      </section>
    );
  }

  const min = Math.min(...allValues, 0);
  const max = Math.max(...allValues, 0);
  const width = 600;
  const height = 120;

  const llmPath = buildPath(llmCum, width, height, min, max);
  const shadowPath = buildPath(shadowCum, width, height, min, max);
  const randomPath = buildPath(randomCum, width, height, min, max);

  return (
    <section className="rounded-xl border border-gray-800 bg-gray-900/40 p-4 mb-4">
      <h2 className="text-sm font-semibold text-gray-300 mb-2">Shadow book — counterfactual P&L</h2>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-24" preserveAspectRatio="none">
        {llmPath && <path d={llmPath} fill="none" stroke="#34d399" strokeWidth={2} />}
        {shadowPath && <path d={shadowPath} fill="none" stroke="#60a5fa" strokeWidth={1.5} strokeDasharray="6,3" />}
        {randomPath && <path d={randomPath} fill="none" stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="2,3" />}
      </svg>
      <div className="flex items-center gap-4 mt-1 text-xs text-gray-400">
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-emerald-400" /> LLM
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-blue-400 border-dashed" /> Rule
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 border-t-2 border-amber-400 border-dashed" /> Random
        </span>
      </div>
      <SummaryText summaries={summaries} llmTotal={llmTotal} hasLlmData={llmPnlSeries.length > 0} />
    </section>
  );
}

function SummaryText({
  summaries,
  llmTotal,
  hasLlmData,
}: {
  summaries: ShadowPolicySummary[];
  llmTotal: number;
  hasLlmData: boolean;
}) {
  const byPolicy = Object.fromEntries(summaries.map((s) => [s.policy, s]));
  const fmt = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;

  const parts: string[] = [];
  const shadowRow = byPolicy['shadow'];
  const randomRow = byPolicy['random'];

  if (hasLlmData) parts.push(`LLM: ${fmt(llmTotal)}`);
  if (shadowRow) parts.push(`Rule: ${fmt(Number(shadowRow.realized))}`);
  if (randomRow) parts.push(`Random: ${fmt(Number(randomRow.realized))}`);

  if (parts.length === 0) return null;

  return (
    <p className="text-xs text-gray-500 mt-2">
      {parts.join(' | ')}
      {(shadowRow || randomRow) && (
        <> · open: {shadowRow ? Number(shadowRow.open_count) : 0} / {randomRow ? Number(randomRow.open_count) : 0}</>
      )}
    </p>
  );
}
