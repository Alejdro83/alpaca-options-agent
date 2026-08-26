#!/usr/bin/env bash
# Hermes cron entry point — same lock/silent-delivery shape as
# trading_bot/run_paper_cron.sh, simplified because bot.py already decides
# for itself what's worth printing (only opens/closes/errors reach stdout;
# see bot.py's own module docstring) rather than this wrapper having to
# grep the output after the fact.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p state
exec 200>state/bot.lock
# A cycle can take a while (MCP subprocess spin-up + several option-chain
# calls + an LLM round-trip); the lock stops two invocations from ever
# racing to open/close the same spread if one run overruns the schedule.
if ! flock -n 200; then
    echo "bot.py already running (lock held), skipping this invocation"
    exit 0
fi
source .venv/bin/activate
set -a
source .env
set +a

OUTPUT=$(python bot.py 2>&1) || {
    echo "⚠️ OPTIONS AGENT ERROR:"
    echo "$OUTPUT" | tail -20
    exit 0
}

if [ -n "$OUTPUT" ]; then
    echo "$OUTPUT"
fi
