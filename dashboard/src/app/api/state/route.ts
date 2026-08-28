import { NextResponse } from 'next/server';
import { getDashboardState } from '@/lib/db';

// No secrets in the response shape — equity/spreads/reasoning are exactly
// what the hackathon submission needs judges to be able to see publicly.
export async function GET() {
  try {
    const state = await getDashboardState();
    return NextResponse.json(state);
  } catch (err) {
    console.error('Failed to load dashboard state:', err);
    return NextResponse.json({ error: 'Failed to load state' }, { status: 500 });
  }
}
