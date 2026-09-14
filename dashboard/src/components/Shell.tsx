'use client';

import { type ReactNode } from 'react';
import { usePathname } from 'next/navigation';
import { useTelegram } from '@/hooks/useTelegram';

// Minimal dual web/Telegram-Mini-App shell — a stripped-down version of
// Agent Bazaar's MiniAppShell pattern, without the nav/wallet/tab-bar
// pieces this single-page read-only dashboard doesn't need. Works
// identically as a plain web page (the "Application URL" for hackathon
// judging) and as a Telegram Mini App (the demo surface for the pitch
// video) from the same code, same as the marketplace does.

const TABS = [
  { href: '/', label: 'Dashboard' },
  { href: '/compare', label: '3-way compare' },
  { href: '/lab', label: 'Backtest lab' },
];

export function Shell({ children }: { children: ReactNode }) {
  useTelegram();
  const pathname = usePathname();

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      <header className="border-b border-gray-800 px-4 py-3 flex flex-wrap gap-y-2 justify-between items-center">
        <a href="/" className="group">
          <h1 className="text-lg font-bold bg-gradient-to-r from-amber-400 to-orange-500 bg-clip-text text-transparent">
            Alpaca Options Agent
          </h1>
          <p className="text-xs text-gray-500 group-hover:text-gray-400">
            lablab.ai × Alpaca — AI Trading Agents Hackathon
          </p>
        </a>
        <nav className="flex gap-2">
          {TABS.map(({ href, label }) => {
            const active = pathname === href;
            return (
              <a
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={
                  'whitespace-nowrap rounded-md border px-2.5 py-1 text-xs font-medium transition-colors ' +
                  (active
                    ? 'border-sky-400 bg-sky-500 text-white'
                    : 'border-sky-500/40 bg-sky-500/10 text-sky-300 hover:border-sky-400 hover:bg-sky-500/20 hover:text-sky-200')
                }
              >
                {label}
              </a>
            );
          })}
        </nav>
      </header>
      {/* max-w-3xl (768px) is the right width for the Telegram Mini App
          surface this shell also serves, but on a plain desktop browser
          (the hackathon's "Application URL", viewed at 1280px+) it leaves
          the whole dashboard stranded in a narrow column with wasted
          space on both sides. Widening progressively at larger
          breakpoints keeps mobile/Telegram pixel-identical (that layout
          only ever sees the base max-w-3xl) while desktop web gets a
          layout that actually uses the screen. */}
      <main className="max-w-3xl md:max-w-4xl lg:max-w-6xl mx-auto px-4 py-4">{children}</main>
    </div>
  );
}
