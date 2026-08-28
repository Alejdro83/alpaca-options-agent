"""Overnight strategy evolution pipeline.

Runs after market close. Collects today's frozen candidates from Supabase,
mutates strategy parameters, replays each variant against today's data,
and auto-promotes winners to state/evolved_params.json.

Deterministic: random.seed based on date so same inputs always produce same
outputs. Promotion is code-only, no LLM involved.

Caveats (honest labeling):
- 1 day of replay data — not statistically significant.
- OI/spread re-filtering uses incumbent thresholds (post-hours data gap).
- BS pricing proxy, not real option chain prices.
"""
from __future__ import annotations

import json
import logging
import math
import random
import statistics
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from evolution_config import (
    PARAM_RANGES,
    POPULATION_SIZE,
    PROMOTION_THRESHOLD,
    PARAMS_PATH,
    REPORT_PATH,
    StrategyParams,
)

import black_scholes
import db

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
RISK_FREE_RATE = 0.045


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _bs_price(spot, strike, dte_days, volatility, option_type):
    if dte_days <= 0 or volatility <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if option_type == "call" else (strike - spot))
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + 0.5 * volatility**2) * t) / (volatility * math.sqrt(t))
    d2 = d1 - volatility * math.sqrt(t)
    N = black_scholes._norm_cdf
    if option_type == "call":
        return spot * N(d1) - strike * math.exp(-RISK_FREE_RATE * t) * N(d2)
    return strike * math.exp(-RISK_FREE_RATE * t) * N(-d2) - spot * N(-d1)


def _strike_for_delta(spot, target_delta, dte_days, volatility, option_type):
    if dte_days <= 0 or volatility <= 0 or spot <= 0:
        return round(spot)
    t = dte_days / 365.0
    d1 = NormalDist().inv_cdf(1 - target_delta) if option_type == "put" else NormalDist().inv_cdf(target_delta)
    k = spot / math.exp(d1 * volatility * math.sqrt(t) - (RISK_FREE_RATE + 0.5 * volatility**2) * t)
    return round(k)


def _vol_percentile(bars_df, lookback=20):
    from signals.indicators import compute_atr
    atr = compute_atr(bars_df["high"], bars_df["low"], bars_df["close"], period=lookback)
    atr_pct = (atr / bars_df["close"]).dropna()
    if len(atr_pct) < lookback * 2:
        return None
    return float(atr_pct.rank(pct=True).iloc[-1])


# ---------------------------------------------------------------------------
# 1. COLLECT
# ---------------------------------------------------------------------------

def collect_today_data(today: date) -> dict:
    """Read decision_journal, spreads, account_snapshots for today from Supabase."""
    result = {"journal": [], "spreads": [], "snapshots": [], "candidates": []}

    try:
        with db._connection() as conn, conn.cursor() as cur:
            schema = db._schema()

            cur.execute(f"""
                SELECT dj.candidates, dj.shadow_selected, dj.gate_rejections,
                       dj.created_at, c.decision, c.reasoning
                FROM {schema}.decision_journal dj
                JOIN {schema}.cycles c ON c.id = dj.cycle_id
                WHERE dj.created_at::date = %s
                ORDER BY dj.created_at
            """, (today.isoformat(),))
            rows = cur.fetchall()
            for row in rows:
                candidates = row[0] if row[0] else []
                result["journal"].append({
                    "candidates": candidates,
                    "shadow_selected": row[1],
                    "gate_rejections": row[2],
                    "created_at": row[3],
                    "decision": row[4],
                    "reasoning": row[5],
                })
                for c in candidates:
                    if c not in result["candidates"]:
                        result["candidates"].append(c)

            cur.execute(f"""
                SELECT underlying, direction, expiration, short_strike, long_strike,
                       short_symbol, long_symbol, contracts, credit_received, max_loss,
                       status, realized_pnl, opened_at, closed_at
                FROM {schema}.spreads
                WHERE opened_at::date = %s OR (closed_at::date = %s AND status != 'open')
                ORDER BY opened_at
            """, (today.isoformat(), today.isoformat()))
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                result["spreads"].append(dict(zip(cols, row)))

            cur.execute(f"""
                SELECT equity, last_equity, cash, open_spreads_count, daily_pl, daily_pl_pct, recorded_at
                FROM {schema}.account_snapshots
                WHERE recorded_at::date = %s
                ORDER BY recorded_at
            """, (today.isoformat(),))
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                result["snapshots"].append(dict(zip(cols, row)))

    except Exception as exc:
        logger.error("Failed to collect today's data: %s", exc)
        raise

    return result


# ---------------------------------------------------------------------------
# 2. MUTATE
# ---------------------------------------------------------------------------

def _incumbent_params() -> StrategyParams:
    """Load incumbent from evolved_params.json, or use defaults."""
    path = BASE_DIR / PARAMS_PATH
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return StrategyParams(
                short_leg_target_delta=data.get("short_leg_target_delta", 0.17),
                min_dte=data.get("min_dte", 10),
                max_dte=data.get("max_dte", 21),
                spread_width_dollars=data.get("spread_width_dollars", 5.0),
                profit_target_pct=data.get("profit_target_pct", 0.50),
                stop_loss_multiple=data.get("stop_loss_multiple", 2.0),
                max_loss_per_spread_pct=data.get("max_loss_per_spread_pct", 0.02),
                min_open_interest=data.get("min_open_interest", 100),
                max_bid_ask_spread_pct=data.get("max_bid_ask_spread_pct", 0.12),
                min_percentile=data.get("min_percentile", 0.40),
            )
        except Exception:
            logger.warning("Failed to parse evolved_params.json, using defaults")
    return StrategyParams()


def _mutate_param(name: str, current_value, rng: random.Random):
    lo, hi = PARAM_RANGES[name]
    if isinstance(current_value, int):
        delta = rng.choice([-2, -1, 1, 2])
        return int(_clamp(current_value + delta, lo, hi))
    delta = rng.uniform(0.8, 1.2)
    return round(_clamp(current_value * delta, lo, hi), 4)


def generate_variants(incumbent: StrategyParams, seed: int) -> list[StrategyParams]:
    """Generate POPULATION_SIZE variants. First 7 mutate one param each;
    variant 8 mutates 2-3 params simultaneously."""
    rng = random.Random(seed)
    param_names = list(asdict(incumbent).keys())
    variants = []

    for i in range(POPULATION_SIZE - 1):
        d = asdict(incumbent)
        name = param_names[i % len(param_names)]
        d[name] = _mutate_param(name, d[name], rng)
        variants.append(StrategyParams(**d))

    multi = asdict(incumbent)
    n_mutate = rng.randint(2, 3)
    names_to_mutate = rng.sample(param_names, n_mutate)
    for name in names_to_mutate:
        multi[name] = _mutate_param(name, multi[name], rng)
    variants.append(StrategyParams(**multi))

    return variants


# ---------------------------------------------------------------------------
# 3. REPLAY
# ---------------------------------------------------------------------------

def _fetch_bars_for_ticker(ticker: str, lookback_days: int = 400) -> pd.DataFrame | None:
    """Fetch daily bars from Alpaca for vol computation."""
    try:
        from alpaca_client import AlpacaClient
        from alpaca.data.timeframe import TimeFrame
        client = AlpacaClient()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days)
        bars = client.get_bars(ticker, TimeFrame.Day, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), limit=300)
        df = pd.DataFrame(bars)
        if df.empty or "close" not in df.columns:
            return None
        return df
    except Exception:
        logger.exception("Failed to fetch bars for %s", ticker)
        return None


def _simulate_variant(
    candidates: list[dict],
    params: StrategyParams,
    bars_cache: dict[str, pd.DataFrame],
    max_positions: int = 5,
) -> dict:
    """Replay today's candidates with variant parameters.

    Returns dict with: candidates_passed, would_have_opened, simulated_pnl,
    max_drawdown, trades (list of per-trade details).
    """
    passed = []
    for c in candidates:
        ticker = c["ticker"]
        bars_df = bars_cache.get(ticker)
        if bars_df is None:
            continue

        pct = _vol_percentile(bars_df)
        if pct is not None and pct < params.min_percentile:
            continue

        passed.append(c)

    trades = []
    for c in passed[:max_positions]:
        ticker = c["ticker"]
        direction = c["direction"]
        strength = c.get("strength", 0)
        bars_df = bars_cache.get(ticker)
        if bars_df is None or len(bars_df) < 2:
            continue

        spot_entry = float(bars_df["close"].iloc[-2])
        spot_close = float(bars_df["close"].iloc[-1])
        # LIMITATION: P&L is estimated from only the last 2 daily bars
        # (entry at T-1 close, exit at T close). This ignores intraday
        # price movement, actual fill timing, and slippage. Real P&L
        # would require order fill data from Alpaca, which is not
        # available in this offline replay path.
        realized_vol = black_scholes.realized_vol_from_bars(bars_df)
        if realized_vol <= 0:
            continue

        is_bull_put = direction == "long"
        option_type = "put" if is_bull_put else "call"
        dte = (params.min_dte + params.max_dte) // 2

        short_strike = _strike_for_delta(
            spot=spot_entry, target_delta=params.short_leg_target_delta,
            dte_days=dte, volatility=realized_vol, option_type=option_type,
        )
        long_strike = (
            short_strike - params.spread_width_dollars
            if is_bull_put
            else short_strike + params.spread_width_dollars
        )

        entry_short = _bs_price(spot_entry, short_strike, dte, realized_vol, option_type)
        entry_long = _bs_price(spot_entry, long_strike, dte, realized_vol, option_type)
        credit = entry_short - entry_long
        if credit <= 0:
            continue

        width = abs(short_strike - long_strike)
        max_loss = width - credit
        if max_loss <= 0:
            continue

        close_short = _bs_price(spot_close, short_strike, max(dte - 1, 0.5), realized_vol, option_type)
        close_long = _bs_price(spot_close, long_strike, max(dte - 1, 0.5), realized_vol, option_type)
        mark_at_close = close_short - close_long

        profit_captured = 1 - (mark_at_close / credit) if credit > 0 else 0
        if profit_captured >= params.profit_target_pct:
            pnl = credit * 100
            exit_reason = "profit_target"
        elif mark_at_close >= credit * params.stop_loss_multiple:
            pnl = (credit - mark_at_close) * 100
            exit_reason = "stop"
        else:
            pnl = (credit - mark_at_close) * 100
            exit_reason = "held"

        trades.append({
            "ticker": ticker,
            "direction": direction,
            "credit": round(credit, 4),
            "max_loss": round(max_loss, 4),
            "pnl": round(pnl, 2),
            "exit_reason": exit_reason,
            "strength": strength,
        })

    total_pnl = sum(t["pnl"] for t in trades)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cumulative += t["pnl"]
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd = max(max_dd, dd)

    return {
        "candidates_passed": len(passed),
        "would_have_opened": len(trades),
        "simulated_pnl": round(total_pnl, 2),
        "max_drawdown": round(max_dd, 2),
        "trades": trades,
    }


def replay_variants(
    candidates: list[dict],
    incumbent: StrategyParams,
    variants: list[StrategyParams],
) -> tuple[dict, list[dict]]:
    """Replay incumbent + all variants. Returns (incumbent_result, variant_results)."""
    tickers = list({c["ticker"] for c in candidates})
    bars_cache: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        df = _fetch_bars_for_ticker(ticker)
        if df is not None:
            bars_cache[ticker] = df

    incumbent_result = _simulate_variant(candidates, incumbent, bars_cache)
    variant_results = []
    for v in variants:
        result = _simulate_variant(candidates, v, bars_cache)
        variant_results.append(result)

    return incumbent_result, variant_results


# ---------------------------------------------------------------------------
# 4. PROMOTE
# ---------------------------------------------------------------------------

def evaluate_promotion(
    incumbent: StrategyParams,
    incumbent_result: dict,
    variants: list[StrategyParams],
    variant_results: list[dict],
) -> tuple[StrategyParams | None, dict | None, str]:
    """Check if any variant beats incumbent. Returns (promoted_params, promoted_result, reason)."""
    best_idx = -1
    best_pnl = incumbent_result["simulated_pnl"]

    for i, (v, r) in enumerate(zip(variants, variant_results)):
        if r["simulated_pnl"] <= incumbent_result["simulated_pnl"]:
            continue
        if r["max_drawdown"] > incumbent_result["max_drawdown"]:
            continue
        if r["candidates_passed"] < incumbent_result["candidates_passed"] * 0.5:
            continue
        improvement = (r["simulated_pnl"] - incumbent_result["simulated_pnl"])
        if incumbent_result["simulated_pnl"] != 0:
            improvement_pct = improvement / abs(incumbent_result["simulated_pnl"])
        else:
            improvement_pct = 1.0 if improvement > 0 else 0.0
        if improvement_pct < PROMOTION_THRESHOLD:
            continue
        if r["simulated_pnl"] > best_pnl:
            best_pnl = r["simulated_pnl"]
            best_idx = i

    if best_idx < 0:
        return None, None, "incumbent held — no variant passed all gates"

    v = variants[best_idx]
    r = variant_results[best_idx]
    improvement_pct = (r["simulated_pnl"] - incumbent_result["simulated_pnl"]) / max(abs(incumbent_result["simulated_pnl"]), 0.01)
    reason = f"variant improved simulated P&L by {improvement_pct:.0%} with {'same' if r['max_drawdown'] <= incumbent_result['max_drawdown'] else 'lower'} max drawdown"
    return v, r, reason


def write_evolved_params(params: StrategyParams, reason: str) -> None:
    """Write promoted params to state/evolved_params.json."""
    path = BASE_DIR / PARAMS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    generation = 1
    if path.exists():
        try:
            old = json.loads(path.read_text())
            generation = old.get("generation", 0) + 1
        except Exception:
            pass

    data = asdict(params)
    data["evolved_at"] = datetime.now(timezone.utc).isoformat()
    data["generation"] = generation
    data["promotion_reason"] = reason

    path.write_text(json.dumps(data, indent=2))
    logger.info("Promoted evolved params (generation %d): %s", generation, reason)


# ---------------------------------------------------------------------------
# 5. REPORT
# ---------------------------------------------------------------------------

def _param_diff_table(incumbent: StrategyParams, best: StrategyParams) -> str:
    lines = ["| Parameter | Incumbent | Best Variant | Changed |",
             "|-----------|-----------|--------------|---------|"]
    inc = asdict(incumbent)
    bst = asdict(best)
    for name in inc:
        i_val = inc[name]
        b_val = bst[name]
        changed = "yes" if i_val != b_val else ""
        lines.append(f"| {name} | {i_val} | {b_val} | {changed} |")
    return "\n".join(lines)


def _result_comparison(incumbent_result: dict, best_result: dict | None) -> str:
    lines = ["| Metric | Incumbent | Best Variant |",
             "|--------|-----------|--------------|"]
    ir = incumbent_result
    br = best_result or {"candidates_passed": 0, "would_have_opened": 0, "simulated_pnl": 0, "max_drawdown": 0}
    lines.append(f"| Candidates passed | {ir['candidates_passed']} | {br['candidates_passed']} |")
    lines.append(f"| Would have opened | {ir['would_have_opened']} | {br['would_have_opened']} |")
    lines.append(f"| Simulated P&L | ${ir['simulated_pnl']:.2f} | ${br['simulated_pnl']:.2f} |")
    lines.append(f"| Max drawdown | ${ir['max_drawdown']:.2f} | ${br['max_drawdown']:.2f} |")
    return "\n".join(lines)


def _evolution_history_table() -> str:
    path = BASE_DIR / PARAMS_PATH
    if not path.exists():
        return "*No evolution history yet.*"
    try:
        data = json.loads(path.read_text())
        gen = data.get("generation", 0)
        evolved_at = data.get("evolved_at", "unknown")
        reason = data.get("promotion_reason", "unknown")
        lines = [
            "| Gen | Evolved At | Reason |",
            "|-----|------------|--------|",
            f"| {gen} | {evolved_at} | {reason} |",
        ]
        return "\n".join(lines)
    except Exception:
        return "*Error reading evolution history.*"


def generate_report(
    today: date,
    incumbent: StrategyParams,
    incumbent_result: dict,
    best_variant: StrategyParams | None,
    best_result: dict | None,
    decision: str,
    reason: str,
    all_variants: list[StrategyParams],
    all_results: list[dict],
) -> str:
    """Generate state/evolution_report.md."""
    lines = [
        f"# Evolution Report — {today.isoformat()}",
        "",
        "**Caveat**: This is exploratory analysis on 1 day of data. Not statistically significant.",
        "",
        "## Decision",
        f"**{decision.upper()}** — {reason}",
        "",
        "## Parameter Diff (Incumbent vs Best Variant)",
        "",
    ]

    if best_variant:
        lines.append(_param_diff_table(incumbent, best_variant))
    else:
        lines.append("*No variant promoted. Incumbent holds.*")

    lines.extend(["", "## Simulated P&L Comparison", ""])
    lines.append(_result_comparison(incumbent_result, best_result))

    lines.extend(["", "## All Variants Tested", ""])
    lines.append("| # | Δ P&L | Passed | Opened | P&L | Max DD | Mutated Param |")
    lines.append("|---|-------|--------|--------|-----|--------|---------------|")
    inc_pnl = incumbent_result["simulated_pnl"]
    for i, (v, r) in enumerate(zip(all_variants, all_results)):
        delta = r["simulated_pnl"] - inc_pnl
        inc_dict = asdict(incumbent)
        v_dict = asdict(v)
        changed = [k for k in inc_dict if inc_dict[k] != v_dict[k]]
        changed_str = ", ".join(changed) if changed else "none"
        lines.append(
            f"| {i+1} | ${delta:+.2f} | {r['candidates_passed']} | "
            f"{r['would_have_opened']} | ${r['simulated_pnl']:.2f} | "
            f"${r['max_drawdown']:.2f} | {changed_str} |"
        )

    lines.extend(["", "## Cumulative Evolution History", ""])
    lines.append(_evolution_history_table())

    lines.extend([
        "",
        "---",
        "*Generated by overnight_evolution.py. Promotion gate: P&L > incumbent, "
        "drawdown ≤ incumbent, improvement > 5%, candidates ≥ 50% of incumbent.*",
    ])

    return "\n".join(lines)


def write_report(content: str) -> None:
    path = BASE_DIR / REPORT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    logger.info("Evolution report written to %s", path)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_evolution() -> None:
    """Full overnight evolution pipeline."""
    today = datetime.now(timezone.utc).date()
    seed = int(today.strftime("%Y%m%d"))
    logger.info("Starting overnight evolution for %s (seed=%d)", today, seed)

    # 1. COLLECT
    logger.info("Step 1: Collecting today's data from Supabase")
    try:
        data = collect_today_data(today)
    except Exception as exc:
        logger.error("Supabase unavailable, skipping evolution: %s", exc)
        return

    candidates = data["candidates"]
    if not candidates:
        logger.warning("0 trades/candidates today — skipping evolution")
        return

    logger.info("Collected %d unique candidates from %d journal entries", len(candidates), len(data["journal"]))

    # 2. MUTATE
    logger.info("Step 2: Generating variants")
    incumbent = _incumbent_params()
    variants = generate_variants(incumbent, seed)
    logger.info("Generated %d variants from incumbent", len(variants))

    # 3. REPLAY
    logger.info("Step 3: Replaying incumbent + variants")
    incumbent_result, variant_results = replay_variants(candidates, incumbent, variants)
    logger.info("Incumbent: P&L=$%.2f, drawdown=$%.2f, passed=%d, opened=%d",
                incumbent_result["simulated_pnl"], incumbent_result["max_drawdown"],
                incumbent_result["candidates_passed"], incumbent_result["would_have_opened"])

    for i, r in enumerate(variant_results):
        logger.info("Variant %d: P&L=$%.2f, drawdown=$%.2f, passed=%d, opened=%d",
                     i + 1, r["simulated_pnl"], r["max_drawdown"],
                     r["candidates_passed"], r["would_have_opened"])

    # 4. PROMOTE
    logger.info("Step 4: Evaluating promotion")
    promoted, promoted_result, reason = evaluate_promotion(incumbent, incumbent_result, variants, variant_results)

    if promoted:
        write_evolved_params(promoted, reason)
        decision = "promoted"
        logger.info("PROMOTED: %s", reason)
    else:
        decision = "shadow"
        logger.info("NO PROMOTION: %s", reason)

    # 5. REPORT
    logger.info("Step 5: Generating report")
    report = generate_report(
        today=today,
        incumbent=incumbent,
        incumbent_result=incumbent_result,
        best_variant=promoted,
        best_result=promoted_result,
        decision=decision,
        reason=reason,
        all_variants=variants,
        all_results=variant_results,
    )
    write_report(report)
    logger.info("Evolution complete")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    run_evolution()
