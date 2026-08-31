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
  const width = 600;
  const height = 120;
  const step = width / (points.length - 1);

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

  const impliedEquities: number[] = [];
  let basePrice: number | null = null;
  let anchorEquity: number | null = null;
  if (spyPoints.length >= 2) {
    basePrice = spyPoints[0].price;
    anchorEquity = points[spyPoints[0].i].equity;
    for (const p of spyPoints) {
      const pctMove = (p.price - basePrice) / basePrice;
      impliedEquities.push(anchorEquity * (1 + pctMove));
    }
  }

  // Real bug fixed 2026-08-31: min/max/range used to come from `values`
  // (the real equity curve) alone. With zero trades since the account
  // reset, equity sits perfectly flat at exactly $100,000 -- min===max, so
  // `range` fell back to the literal `|| 1`. The SPY overlay's dollar swings
  // (hundreds of dollars) then got divided by that `1` instead of a real
  // range, blowing its y-coordinates thousands of pixels outside the
  // viewBox -- the line was still being drawn, just entirely off-screen,
  // which read as "the SPY curve looks static" (it wasn't static, it was
  // invisible). Fixed by including the SPY-implied values in the same
  // min/max the real equity line uses, so both curves always share one
  // sane scale regardless of whether real equity has moved yet.
  const allValues = impliedEquities.length > 0 ? [...values, ...impliedEquities] : values;
  const min = Math.min(...allValues);
  const max = Math.max(...allValues);
  const range = max - min || 1;

  const path = points
    .map((p, i) => {
      const x = i * step;
      const y = height - ((p.equity - min) / range) * height;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  const up = values[values.length - 1] >= values[0];

  let spyPath: string | null = null;
  if (impliedEquities.length >= 2) {
    spyPath = spyPoints
      .map((p, idx) => {
        const x = p.i * step;
        const y = height - ((impliedEquities[idx] - min) / range) * height;
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
