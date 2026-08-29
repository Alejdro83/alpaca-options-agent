"""Correlation-cluster membership — a real gap the per-underlying
concentration cap (config.risk.max_concentration_pct) does NOT cover
(2026-08-29, from a research pass on further-improvements-worth-making
before the contest deadline): five concurrent spreads on five different
mega-cap tech names are not five independent bets during a broad selloff
-- mega-cap tech pairwise correlations are well documented to spike toward
~0.9 in stress (see e.g. https://www.schwab.com/learn/story/every-breadth-
you-take-market-concentration-risks -- industry commentary, not a single
peer-reviewed figure, but the concentration phenomenon itself is
uncontroversial and directly relevant given this project's own screening
universe recurrently surfaces the same handful of mega-cap tech names).

This is deliberately NOT a full sector taxonomy -- building and maintaining
one is out of scope for the time remaining before the hackathon deadline.
It is exactly one cluster (the names whose correlation risk is both
best-documented and most likely to actually appear in this project's own
S&P 500 / Nasdaq 100 screening universe), checked via risk_gate's new
cluster-exposure gate (max_cluster_concentration_pct) alongside the
existing per-underlying cap, not instead of it.
"""
from __future__ import annotations

# "Magnificent Seven" plus the two other names most commonly cited
# alongside them for correlation purposes (AVGO, AMD -- both large-cap
# semiconductor names that move with the same AI/growth-tech factor).
# Deliberately a flat set, not a hierarchy -- add clusters here (as
# additional named sets) if a second one becomes worth tracking, rather
# than growing this one into an ad-hoc sector map.
MEGA_CAP_TECH: frozenset[str] = frozenset({
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "AVGO", "AMD",
})

_CLUSTERS: dict[str, frozenset[str]] = {
    "mega_cap_tech": MEGA_CAP_TECH,
}


def cluster_for(ticker: str) -> str | None:
    """Which correlation cluster `ticker` belongs to, or None if it isn't
    in any defined cluster -- callers must treat None as "no cluster gate
    applies to this ticker", not as a rejection.
    """
    for name, members in _CLUSTERS.items():
        if ticker in members:
            return name
    return None
