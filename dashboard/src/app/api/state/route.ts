import { NextResponse } from 'next/server';
import { getDashboardState } from '@/lib/db';

// No secrets in the response shape — equity/spreads/reasoning are exactly
// what the hackathon submission needs judges to be able to see publicly.
//
// Always hit Postgres fresh — this is polled every 30s from the client and
// the whole point is showing the agent's current state, not a stale build.
export const dynamic = 'force-dynamic';

export async function GET() {
  try {
    const state = await getDashboardState();
    return NextResponse.json(state);
  } catch (err) {
    console.error('GET /api/state failed:', err);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : 'Failed to load dashboard state' },
      { status: 500 }
    );
  }
}
