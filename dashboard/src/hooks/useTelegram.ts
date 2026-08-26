'use client';

// Simplified port of Agent Bazaar's useTelegram hook. No initData
// validation/auth here — unlike the marketplace, this dashboard shows no
// per-user or sensitive data, just the trading agent's own public activity,
// so there's nothing to authenticate. It only needs to know whether it's
// running inside Telegram (to call ready()/expand() and pick up the client's
// color scheme) versus a plain browser tab.

import { useEffect, useState } from 'react';

interface TelegramWebApp {
  initData: string;
  colorScheme: 'light' | 'dark';
  ready: () => void;
  expand: () => void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function useTelegram() {
  const [webApp, setWebApp] = useState<TelegramWebApp | null>(null);

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    // Same guard as Agent Bazaar's hook: telegram-web-app.js defines
    // window.Telegram.WebApp unconditionally even in a plain browser tab —
    // initData is the field that's only non-empty inside a real Mini App.
    if (!tg || !tg.initData) return;
    setWebApp(tg);
    tg.ready();
    tg.expand();
  }, []);

  return {
    isInTelegram: webApp !== null,
    colorScheme: webApp?.colorScheme || 'dark',
  };
}
