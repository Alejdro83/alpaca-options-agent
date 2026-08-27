"""Shadow ablation report — does the LLM add value over mechanical selection?"""
from __future__ import annotations

import json

import psycopg2.extras

import db


def run_report() -> None:
    try:
        with db._connection() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {db._schema()}.decision_journal ORDER BY created_at"
            )
            journals = cur.fetchall()
    except Exception as exc:
        print(f"Failed to read decision_journal: {exc}")
        return

    total = len(journals)
    if total == 0:
        print("No decision journal entries yet.")
        return

    llm_only = 0
    shadow_only = 0
    both_agree = 0
    llm_abstained = 0
    shadow_abstained = 0

    for j in journals:
        raw_llm = j["llm_selected"]
        raw_shadow = j["shadow_selected"]
        llm = set(json.loads(raw_llm) if isinstance(raw_llm, str) else raw_llm)
        shadow = set(json.loads(raw_shadow) if isinstance(raw_shadow, str) else raw_shadow)

        if not llm:
            llm_abstained += 1
        if not shadow:
            shadow_abstained += 1
        if llm == shadow:
            both_agree += 1
        if llm - shadow:
            llm_only += 1
        if shadow - llm:
            shadow_only += 1

    print(f"Total cycles: {total}")
    print(f"LLM abstained: {llm_abstained} ({llm_abstained / total * 100:.0f}%)")
    print(f"Shadow abstained: {shadow_abstained} ({shadow_abstained / total * 100:.0f}%)")
    print(f"Both agree: {both_agree} ({both_agree / total * 100:.0f}%)")
    print(f"LLM-only picks: {llm_only}")
    print(f"Shadow-only picks: {shadow_only}")


if __name__ == "__main__":
    run_report()
