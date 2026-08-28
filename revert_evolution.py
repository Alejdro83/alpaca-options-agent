#!/usr/bin/env python3
"""Manual revert for overnight_evolution.py's promoted parameters — the
Layer 1 half of the evolution audit trail (Layer 2 is the automatic
performance-based revert inside overnight_evolution.py itself).

Usage:
    python3 revert_evolution.py --list              # show evolution history
    python3 revert_evolution.py --baseline          # back to config.py defaults
    python3 revert_evolution.py <generation>         # back to a past generation's params

Every promotion overnight_evolution.py ever made is a permanent row in the
`evolution_history` table (never overwritten, unlike evolved_params.json
which only holds the current state) — this reads that history to know what
a past generation's parameters actually were, writes state/evolved_params.json
to match, and logs the revert itself as its own row (decision=
"manual_revert") so the trail stays complete.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import db
from evolution_config import PARAMS_PATH, StrategyParams

BASE_DIR = Path(__file__).resolve().parent


def _current_state() -> tuple[int, dict | None]:
    """Returns (current_generation, current_full_params_dict_or_None)."""
    path = BASE_DIR / PARAMS_PATH
    if not path.exists():
        return 0, None
    try:
        data = json.loads(path.read_text())
        return int(data.get("generation", 0)), data
    except Exception:
        return 0, None


def _write_params(params: StrategyParams, generation: int, reason: str) -> None:
    path = BASE_DIR / PARAMS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(params)
    data["evolved_at"] = datetime.now(timezone.utc).isoformat()
    data["generation"] = generation
    data["promotion_reason"] = reason
    path.write_text(json.dumps(data, indent=2))


def cmd_list() -> None:
    rows = db.get_evolution_history(limit=30)
    if not rows:
        print("No evolution history yet.")
        return
    for r in rows:
        reverted = f" [REVERTED: {r['reverted_reason']}]" if r.get("reverted_at") else ""
        print(f"gen {r['generation']:>3}  {r['ran_at']}  {r['decision']:<15}  {r['reason']}{reverted}")


def cmd_baseline() -> None:
    current_gen, current_params = _current_state()
    if current_gen == 0:
        print("Already on baseline (config.py defaults) — nothing to revert.")
        return
    path = BASE_DIR / PARAMS_PATH
    if path.exists():
        path.unlink()
    reason = f"manual revert: generation {current_gen} -> baseline (config.py defaults)"
    if current_gen:
        db.mark_generation_reverted(current_gen, reason)
    db.record_evolution_history(
        generation=0, decision="manual_revert",
        params_before=current_params, params_after=None, reason=reason,
    )
    print(f"Reverted to baseline. state/evolved_params.json removed. ({reason})")


def cmd_to_generation(target_gen: int) -> None:
    current_gen, current_params = _current_state()
    if current_gen == target_gen:
        print(f"Already on generation {target_gen} — nothing to revert.")
        return

    rows = db.get_evolution_history(limit=200)
    match = next((r for r in rows if r["generation"] == target_gen and r["decision"] == "promoted"), None)
    if match is None:
        print(f"No 'promoted' entry found for generation {target_gen} in evolution_history. "
              f"Use --list to see what's actually available.")
        sys.exit(1)

    restored = StrategyParams(**{
        k: v for k, v in match["params_after"].items()
        if k in StrategyParams.__dataclass_fields__
    })
    reason = f"manual revert: generation {current_gen} -> generation {target_gen}"
    _write_params(restored, target_gen, reason)
    if current_gen:
        db.mark_generation_reverted(current_gen, reason)
    db.record_evolution_history(
        generation=target_gen, decision="manual_revert",
        params_before=current_params, params_after=asdict(restored), reason=reason,
    )
    print(f"Reverted to generation {target_gen}: {asdict(restored)}")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "--list":
        cmd_list()
    elif arg == "--baseline":
        cmd_baseline()
    else:
        try:
            target = int(arg)
        except ValueError:
            print(__doc__)
            sys.exit(1)
        cmd_to_generation(target)


if __name__ == "__main__":
    main()
