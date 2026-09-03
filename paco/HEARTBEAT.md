# Heartbeat — Paco Trading Agent

ZeroClaw pings you every 5 min during market hours (13:30-20:00 UTC Mon-Fri)
to confirm you're alive, the MCP proxy is connected, and the last cycle
finished. Runs as `systemd --user` (`zeroclaw.service`), not a container —
a failed heartbeat triggers a systemd restart (`Restart=always`).

## Recovery after an unexpected stop
1. Read MEMORY.md for context.
2. Check `zeroclaw_trading.cycles` in Supabase for the last recorded cycle.
3. Resume from there — don't repeat completed work.
