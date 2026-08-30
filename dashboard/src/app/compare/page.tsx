'use client';

import { useEffect, useState } from 'react';
import { Shell } from '@/components/Shell';
import { StrategyComparisonPanel, type CompareState } from '@/components/StrategyComparisonPanel';

const POLL_MS = 30_000;

// Three-strategy comparison page (2026-08-30) -- see StrategyComparisonPanel
// for the full rationale. Same polling pattern as the main page.
export default function ComparePage() {
  const [state, setState] = useState<CompareState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchState = async () => {
      try {
        const res = await fetch('/api/compare');
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
        <p className="text-red-400 text-sm">Failed to load comparison: {error}</p>
      </Shell>
    );
  }

  if (!state) {
    return (
      <Shell>
        <div className="animate-pulse text-gray-500 text-sm">Loading comparison…</div>
      </Shell>
    );
  }

  return (
    <Shell>
      <StrategyComparisonPanel data={state} />
    </Shell>
  );
}
