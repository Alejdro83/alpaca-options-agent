import { NextResponse } from 'next/server';
import { getAblationState } from '@/lib/db';

// Same convention as /api/state: no secrets, always fresh (this is a
// snapshot of the last backtest_ablation.py run, not a live-trading path,
// but there's no reason to serve a stale cached build for it either).
export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const state = await getAblationState();
    return NextResponse.json(state);
  } catch (err) {
    console.error('GET /api/lab failed:', err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'Failed to load ablation lab state' },
      { status: 500 }
    );
  }
}
