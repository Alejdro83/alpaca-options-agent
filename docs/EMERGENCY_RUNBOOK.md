# Emergency Flatten Runbook

## When to use

Something looks wrong with open positions and you need to close everything
NOW. Examples:
- A position is moving against you fast and you want out immediately
- You suspect a bug in the bot's risk logic
- The contest deadline is approaching and positions are still open
- Any situation where "close everything" is the safest action

## How to invoke

```bash
cd /path/to/alpaca-options-agent
source .venv/bin/activate
set -a
source .env
set +a
python3 emergency_flatten.py
```

The script lists all open spreads and asks you to type `flatten` to confirm.
For scripted/automated use (e.g. from a Hermes cron one-shot job):

```bash
python3 emergency_flatten.py --yes
```

This skips the confirmation prompt.

## What "closed_emergency" means

Each spread closed by this tool is recorded in the `spreads` table with
`status = 'closed_emergency'` and `realized_pnl = NULL`. This is distinct
from the normal close reasons (`closed_profit`, `closed_stop`, `closed_expiry`)
so anyone reading the DB later knows this was a manual intervention, not a
normal rule-based exit.

`realized_pnl` is NULL because the actual fill price isn't known at
market-order submission time — the order may fill at a different price than
the last mark. This is the same "honest unknown over a fabricated number"
convention the bot's own `manage_open_spreads` uses for its own force-close
path.

## Kill switch vs emergency flatten

The kill switch (`python3 kill_switch.py on`, which creates `state/PAUSE`)
stops NEW cycles — the bot won't screen for candidates or open new spreads.
But it does nothing about already-open positions.

`emergency_flatten.py` closes existing positions.

In a real emergency, use both together:
1. `python3 kill_switch.py on` — stop new activity
2. `python3 emergency_flatten.py` — close what's already open
3. Investigate
4. `python3 kill_switch.py off` — resume when ready
