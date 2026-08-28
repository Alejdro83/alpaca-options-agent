#!/usr/bin/env python3
"""Kill switch — pause/resume the options agent without SSH.

Usage:
    python3 kill_switch.py on     # pause all activity
    python3 kill_switch.py off    # resume
    python3 kill_switch.py status # check if paused

The bot checks for state/PAUSE at the start of each cycle and before
each spread open. File-based (no DB, no network) — works even if
Supabase is down.
"""
import sys
from pathlib import Path

PAUSE_FILE = Path(__file__).resolve().parent / "state" / "PAUSE"


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("Usage: python3 kill_switch.py [on|off|status]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "on":
        PAUSE_FILE.parent.mkdir(exist_ok=True)
        PAUSE_FILE.write_text("paused\n")
        print("KILL SWITCH ON — bot will skip all cycles and spread opens")

    elif action == "off":
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            print("KILL SWITCH OFF — bot will resume on next cycle")
        else:
            print("Kill switch was not active")

    elif action == "status":
        if PAUSE_FILE.exists():
            print("PAUSED — bot is not trading")
        else:
            print("ACTIVE — bot is trading normally")


if __name__ == "__main__":
    main()
