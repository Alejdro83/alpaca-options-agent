#!/usr/bin/env bash
# Persistent entry point for the WebSocket real-time spread monitor — run
# as a systemd --user service (see alpaca-spread-monitor.service), not as
# a cron job: spread_monitor.py runs forever (while self._running), so a
# daily-trigger cron model was the wrong tool (real bug fixed 2026-08-28 —
# see PREMORTEM.md / pendientes.md for the incident: the job that tried to
# do this had its script content pasted into the wrong jobs.json field and
# failed every day since creation, so this had never actually run before).
set -uo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate
set -a
source .env
set +a
exec python3 spread_monitor.py
