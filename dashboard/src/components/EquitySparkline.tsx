'use client';

// A plain inline SVG sparkline — no charting library dependency needed for
// a single line (plus an optional SPY overlay) over ≤5 trading days of
// points.
type Point = { equity: number; spy_price?: number | null; snapshot_at: string };

export function EquitySparkline({ points }: { points: Point[] }) {
  if (points.length < 2) {
    return <div className="text-sm text-gray-500">Not enough data yet for a curve.</div>;
  }
  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const width = 600;
  const height = 120;
  const step = width / (points.length - 1);

  const path = points
    .map((p, i) => {
      const x = i * step;
      const y = height - ((p.equity - min) / range) * height;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  const up = values[values.length - 1] >= values[0];

  // SPY overlay (2026-08-29): a synthetic, non-capital-consuming shadow
  // benchmark -- "skill vs market" instead of a bare equity curve a rising
  // tape alone could explain. Plotted as SPY's percent move applied to the
  // equity value at the first snapshot that has a SPY price ("if this
  // account had instead just tracked SPY from that point"), on the SAME
  // dollar y-axis as the real equity line -- so "line above/below" reads
  // directly as "the account under/over-performed a same-dated SPY
  // position". Needs at least 2 real (non-null) SPY points to be worth
  // drawing at all.
  const spyPoints = points
    .map((p, i) => ({ i, price: p.spy_price }))
    .filter((p): p is { i: number; price: number } => p.price != null);

  let spyPath: string | null = null;
  if (spyPoints.length >= 2) {
    const basePrice = spyPoints[0].price;
    const anchorEquity = points[spyPoints[0].i].equity;
    spyPath = spyPoints
      .map((p, idx) => {
        const x = p.i * step;
        const pctMove = (p.price - basePrice) / basePrice;
        const impliedEquity = anchorEquity * (1 + pctMove);
        const y = height - ((impliedEquity - min) / range) * height;
        return `${idx === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-24" preserveAspectRatio="none">
      {spyPath && (
        <path d={spyPath} fill="none" stroke="#94a3b8" strokeWidth={1.5} strokeDasharray="4,3" />
      )}
      <path d={path} fill="none" stroke={up ? '#34d399' : '#f87171'} strokeWidth={2} />
    </svg>
  );
}
