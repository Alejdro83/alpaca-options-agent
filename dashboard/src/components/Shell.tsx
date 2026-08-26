'use client';

import { type ReactNode } from 'react';
import { useTelegram } from '@/hooks/useTelegram';

// Minimal dual web/Telegram-Mini-App shell — a stripped-down version of
// Agent Bazaar's MiniAppShell pattern, without the nav/wallet/tab-bar
// pieces this single-page read-only dashboard doesn't need. Works
// identically as a plain web page (the "Application URL" for hackathon
// judging) and as a Telegram Mini App (the demo surface for the pitch
// video) from the same code, same as the marketplace does.
export function Shell({ children }: { children: ReactNode }) {
  useTelegram();

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <header className="border-b border-gray-800 px-4 py-3">
        <h1 className="text-lg font-bold bg-gradient-to-r from-amber-400 to-orange-500 bg-clip-text text-transparent">
          Alpaca Options Agent
        </h1>
        <p className="text-xs text-gray-500">lablab.ai × Alpaca — AI Trading Agents Hackathon</p>
      </header>
      <main className="max-w-3xl mx-auto px-4 py-4">{children}</main>
    </div>
  );
}
