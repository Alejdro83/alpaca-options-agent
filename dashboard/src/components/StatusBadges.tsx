'use client';

import { useEffect, useState } from 'react';

// Approximate NYSE session check (9:30–16:00 America/New_York, Mon–Fri,
// no holiday calendar) — labeled "aprox." on purpose. This is a display
// convenience, not what the agent itself uses to decide whether to trade
// (bot.py always asks Alpaca's real get_clock() for that); good enough for
// "is it plausible the agent just acted," not authoritative.
function isMarketLikelyOpen(now: Date): boolean {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: 'numeric',
    hour12: false,
    weekday: 'short',
  }).formatToParts(now);
  const weekday = parts.find((p) => p.type === 'weekday')?.value ?? '';
  const hour = Number(parts.find((p) => p.type === 'hour')?.value ?? 0);
  const minute = Number(parts.find((p) => p.type === 'minute')?.value ?? 0);
  if (weekday === 'Sat' || weekday === 'Sun') return false;
  const minutesOfDay = hour * 60 + minute;
  return minutesOfDay >= 9 * 60 + 30 && minutesOfDay < 16 * 60;
}

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 1) return 'hace segundos';
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `hace ${hours}h`;
  return `hace ${Math.floor(hours / 24)}d`;
}

export function StatusBadges({ lastCycleAt }: { lastCycleAt: string | null }) {
  const [open, setOpen] = useState<boolean | null>(null);

  useEffect(() => {
    const update = () => setOpen(isMarketLikelyOpen(new Date()));
    update();
    const id = setInterval(update, 60_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
      <span
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 ${
          open ? 'bg-emerald-950 text-emerald-400' : 'bg-gray-800 text-gray-400'
        }`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${open ? 'bg-emerald-400' : 'bg-gray-500'}`} />
        {open === null ? 'Comprobando mercado…' : open ? 'Mercado abierto (aprox.)' : 'Mercado cerrado (aprox.)'}
      </span>
      {lastCycleAt && (
        <span className="text-gray-500">Último ciclo del agente: {timeAgo(lastCycleAt)}</span>
      )}
    </div>
  );
}
