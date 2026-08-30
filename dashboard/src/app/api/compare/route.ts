import { NextResponse } from 'next/server';
import { getCompareState } from '@/lib/db';

// Three-strategy comparison (2026-08-30) -- our own judged account, the
// Paco/zeroclaw research experiment, and rookieriot's independent build.
// Same "always fresh" reasoning as /api/state.
export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const state = await getCompareState();
    return NextResponse.json(state);
  } catch (err) {
    console.error('GET /api/compare failed:', err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'Failed to load comparison state' },
      { status: 500 }
    );
  }
}
