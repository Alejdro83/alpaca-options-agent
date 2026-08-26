'use client';

// A plain inline SVG sparkline — no charting library dependency needed for
// a single line over ≤5 trading days of points.
export function EquitySparkline({ points }: { points: { equity: number; snapshot_at: string }[] }) {
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

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-24" preserveAspectRatio="none">
      <path d={path} fill="none" stroke={up ? '#34d399' : '#f87171'} strokeWidth={2} />
    </svg>
  );
}
