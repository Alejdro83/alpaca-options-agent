"""Quiet-market end-of-day diagnostic (2026-09-02).

Real gap this closes: every existing relaxation in this project (the
volatility filter's adaptive relaxed_min_percentile, the VIXY-driven iron
condor delta override) reacts within a SINGLE cycle -- none of them have
any memory of "the whole trading day produced zero real trades." Prompted
by Alex asking whether such a day-level check exists.

Deliberately report-only: this module NEVER relaxes a gate, NEVER opens
anything, NEVER changes any config. It only tells a human what happened
today so THEY can decide whether any real gate is worth revisiting --
same "not backtested, watch real results, a human decides" discipline
already used for every other threshold in this project (see config.py's
own comments). An automatic same-day relaxation was discussed and
deliberately NOT built.

Wired into overnight_evolution.py's own --dry-run cron (2026-09-02) rather
than getting its own cron/notification channel -- that job already fires
at 22:00 UTC, 2 hours after market close, with today's data fully
settled, and already delivers a short Discord summary via the same
"--no-agent" job. Piggybacking avoids exactly the "noisy duplicate
message" mistake already found and fixed once in this project
(run_options_cron.sh's stdout, 2026-08-27).
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import db

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
REPORT_PATH = "state/quiet_market_report.md"
_REASON_TRUNCATE = 140


def _collect_today(today: date) -> dict:
    """Real spreads opened today + every real cycle's candidates/
    rejections for today, straight from Supabase -- same connection/schema
    helpers overnight_evolution.py's own collect_today_data uses, but
    pulling `pre_trade_rejections` too (that one doesn't need it for its
    own mutation/replay purpose; this diagnostic does, to show the LAST
    real gate a candidate failed, not just the first).
    """
    result: dict = {"journal": [], "spreads": []}
    with db._connection() as conn, conn.cursor() as cur:
        schema = db._schema()
        cur.execute(
            f"""
            SELECT dj.candidates, dj.gate_rejections, dj.pre_trade_rejections, dj.created_at
            FROM {schema}.decision_journal dj
            WHERE dj.created_at::date = %s
            ORDER BY dj.created_at
            """,
            (today.isoformat(),),
        )
        for row in cur.fetchall():
            result["journal"].append({
                "candidates": row[0] or [],
                "gate_rejections": row[1] or [],
                "pre_trade_rejections": row[2] or [],
                "created_at": row[3],
            })

        cur.execute(
            f"SELECT underlying, opened_at FROM {schema}.spreads WHERE opened_at::date = %s",
            (today.isoformat(),),
        )
        result["spreads"] = [{"underlying": r[0], "opened_at": r[1]} for r in cur.fetchall()]

    return result


def check_quiet_market_day(today: date) -> str | None:
    """Returns a short (Discord-safe, a few lines) summary if TODAY
    produced zero real trades, or None if at least one real position
    opened today -- stay silent then, matching this project's own
    "deliver only when something happened" convention rather than a daily
    "all clear" message nobody needs.

    Writes the full breakdown (every real rejection reason seen today,
    verbatim) to state/quiet_market_report.md regardless -- the short
    string returned here is only ever the Discord-safe summary of it.
    """
    try:
        data = _collect_today(today)
    except Exception:
        logger.exception("Quiet-market check failed to collect today's data (non-fatal)")
        return None

    if data["spreads"]:
        return None  # at least one real trade opened today -- nothing to report

    all_candidates: list = []
    all_gate_rejections: list = []
    all_pretrade_rejections: list = []
    for entry in data["journal"]:
        all_candidates.extend(entry["candidates"])
        all_gate_rejections.extend(entry["gate_rejections"])
        all_pretrade_rejections.extend(entry["pre_trade_rejections"])

    lines = [
        f"# Quiet-market report — {today.isoformat()}",
        "",
        f"0 real trades opened today, across {len(data['journal'])} real cycle(s).",
        f"- Candidates that reached the LLM step (already passed risk_gate.check_new_spread): {len(all_candidates)}",
        f"- Rejected by the risk gate: {len(all_gate_rejections)}",
        f"- Selected by the LLM but rejected on the final pre-trade re-check: {len(all_pretrade_rejections)}",
        "",
    ]
    if all_gate_rejections:
        lines.append("## Risk gate rejections (verbatim reasons)")
        for r in all_gate_rejections:
            reasons = "; ".join(r.get("reasons", []))
            lines.append(f"- {r.get('ticker')}: {reasons}")
        lines.append("")
    if all_pretrade_rejections:
        lines.append("## Pre-trade re-check rejections (verbatim reasons)")
        for r in all_pretrade_rejections:
            lines.append(f"- {r.get('ticker')}: {r.get('reason')}")
        lines.append("")

    report_path = BASE_DIR / REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines))

    if not all_candidates and not all_gate_rejections and not all_pretrade_rejections:
        detail = "ningún candidato llegó siquiera a evaluarse (probable falta de liquidez o fallo de build, no de riesgo)"
    else:
        example = None
        if all_gate_rejections:
            r = all_gate_rejections[0]
            example = f"{r.get('ticker')}: {'; '.join(r.get('reasons', []))[:_REASON_TRUNCATE]}"
        elif all_pretrade_rejections:
            r = all_pretrade_rejections[0]
            example = f"{r.get('ticker')}: {str(r.get('reason'))[:_REASON_TRUNCATE]}"
        detail = (
            f"{len(all_candidates)} candidato(s) llegaron a la IA, "
            f"{len(all_gate_rejections)} rechazados por el risk gate, "
            f"{len(all_pretrade_rejections)} en el re-chequeo final"
            + (f". Ejemplo: {example}" if example else "")
        )

    return (
        f"🌙 Mercado tranquilo — {today.isoformat()}: 0 operaciones reales. {detail}. "
        f"Detalle completo: state/quiet_market_report.md"
    )
