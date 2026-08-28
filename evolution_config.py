from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StrategyParams:
    short_leg_target_delta: float = 0.17
    min_dte: int = 10
    max_dte: int = 21
    spread_width_dollars: float = 5.0
    profit_target_pct: float = 0.50
    stop_loss_multiple: float = 2.0
    max_loss_per_spread_pct: float = 0.02
    min_open_interest: int = 100
    max_bid_ask_spread_pct: float = 0.12
    # Real bug fixed 2026-08-28: this field was named vol_min_percentile,
    # but config.VolatilityFilter's real attribute (which bot.py's _vol()
    # reads evolved overrides against) is min_percentile — the mismatch
    # meant a promoted change to this parameter would silently never take
    # effect. Renamed to match; default also corrected from 0.25 (a same-
    # day reactive value reverted in config.py on 2026-08-27) to 0.40, the
    # real production default, so the evolution incumbent baseline matches
    # what's actually running.
    min_percentile: float = 0.40


PARAM_RANGES: dict[str, tuple[float, float] | tuple[int, int]] = {
    "short_leg_target_delta": (0.10, 0.25),
    "min_dte": (7, 14),
    "max_dte": (14, 30),
    "spread_width_dollars": (3.0, 10.0),
    "profit_target_pct": (0.30, 0.70),
    "stop_loss_multiple": (1.5, 3.0),
    "max_loss_per_spread_pct": (0.01, 0.05),
    "min_open_interest": (50, 200),
    "max_bid_ask_spread_pct": (0.08, 0.20),
    "min_percentile": (0.15, 0.50),
}

POPULATION_SIZE = 8
PROMOTION_THRESHOLD = 0.05
PARAMS_PATH = "state/evolved_params.json"
REPORT_PATH = "state/evolution_report.md"

# Layer 2 safety net (2026-08-28): a promotion is only ever justified by one
# day's simulated replay (see overnight_evolution.py's own docstring on why
# that's not statistically significant on its own). MIN_TRADES_FOR_REVERT_CHECK
# is how many REAL closed trades a generation needs -- both the active one
# and the one before it -- before its real performance is trusted enough to
# judge or compare; below that, the auto-revert check stays quiet rather
# than act on noise.
MIN_TRADES_FOR_REVERT_CHECK = 5
