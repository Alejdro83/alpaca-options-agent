#!/usr/bin/env python3
"""portfolio_greeks_paco.py — independent portfolio Greeks snapshot for Paco
(2026-08-30).

Same reasoning as reconcile_paco.py's own docstring: runs as its OWN
zeroclaw cron job (a plain shell task, not an --agent task), deliberately
decoupled from Paco's own LLM reasoning loop -- a monitoring/dashboard
feature must never depend on the LLM remembering to compute or log it,
and a bug here must never be able to touch a real close (this script
never places or modifies an order, read-only start to finish).

Reuses the judged bot's own portfolio_greeks.py math (net delta/gamma/
theta/vega/rho aggregation, beta-weighted delta from REAL trailing daily
returns -- never a hardcoded beta table) via a light adapter, because
zeroclaw_trading.spreads uses different column names than the judged
bot's alpaca_hackathon.spreads (short_leg/long_leg/call_short_leg/
call_long_leg/symbol, not short_symbol/long_symbol/call_short_symbol/
call_long_symbol/underlying) -- the pure per-symbol helpers
(_fetch_daily_closes-equivalent, _returns_from_closes, _compute_beta) are
imported directly and unchanged; only the spread-row iteration is
Paco-specific.

Uses the official alpaca-py SDK's OptionHistoricalDataClient directly
(not the MCP proxy) -- same reasoning as reconcile_paco.py already
established for get_all_positions: a plain periodic script doesn't need
an MCP subprocess round-trip, and alpaca_client.py (the judged bot's own
equities client, reused here for beta's daily bars) already follows this
direct-SDK pattern successfully.

CRITICAL safety pattern, copied verbatim from reconcile_paco.py: loads
mcp_risk_proxy/.env (Paco's own repurposed trading_bot account) and sets
those as os.environ BEFORE importing alpaca_client/config, so config.alpaca
never accidentally resolves to the judged hackathon account's credentials
(alpaca-options-agent/.env). Never import alpaca_client at module level
for this reason -- only inside main(), after the environment is set.

Usage (no arguments):
    /home/lab-master/alpaca-options-agent/.venv/bin/python3 \\
        /home/lab-master/.zeroclaw/agents/paco/workspace/scripts/portfolio_greeks_paco.py

Exit 0 = snapshot recorded (or nothing to record -- zero open positions is
not an error). Exit 2 = the check itself failed (DB/broker unreachable) --
never raises past main(), same fail-safe posture as reconcile_paco.py,
except a failure here only means "monitoring stayed stale", never a
trading action, so there is no estop/kill-switch escalation.
"""
from __future__ import annotations

import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, "/home/lab-master/alpaca-options-agent")

_SUPABASE_ENV = Path(__file__).resolve().parent / ".env"
_ALPACA_ENV = Path("/home/lab-master/mcp_risk_proxy/.env")
_SCHEMA = "zeroclaw_trading"

BETA_LOOKBACK_DAYS = 95  # ~65 trading days after weekends/holidays -- matches portfolio_greeks.py
BETA_DEFAULT = 1.0


def _load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def _legs_for_spread(spread: dict) -> list[str]:
    """All option symbols for one open spread row -- 2 for a vertical, 4
    for an iron condor (put side + call side). Paco's own column names."""
    legs = [spread["short_leg"], spread["long_leg"]]
    if spread.get("strategy") == "iron_condor":
        legs += [spread.get("call_short_leg"), spread.get("call_long_leg")]
    return [l for l in legs if l]


def _returns_from_closes(closes: dict[str, float]) -> dict[str, float]:
    """Daily % returns keyed by the LATER date of each consecutive pair --
    identical logic to portfolio_greeks.py's own helper, duplicated here
    (not imported) since it takes/returns plain dicts with no dependency
    on the judged bot's mcp/db, so a straight copy is simpler and safer
    than reaching into that module's internals for one pure function."""
    dates = sorted(closes)
    returns = {}
    for prev, cur in zip(dates, dates[1:]):
        if closes[prev]:
            returns[cur] = (closes[cur] - closes[prev]) / closes[prev]
    return returns


def _compute_beta(stock_returns: dict[str, float], spy_returns: dict[str, float]) -> float | None:
    shared_dates = sorted(set(stock_returns) & set(spy_returns))
    if len(shared_dates) < 15:
        return None
    x = [stock_returns[d] for d in shared_dates]
    y = [spy_returns[d] for d in shared_dates]
    spy_var = statistics.variance(y)
    if spy_var == 0:
        return None
    mean_x, mean_y = statistics.mean(x), statistics.mean(y)
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / (len(x) - 1)
    return cov / spy_var


def _closes_by_date(client, symbol: str) -> dict[str, float] | None:
    from alpaca.data.timeframe import TimeFrame

    end = date.today()
    start = end - timedelta(days=BETA_LOOKBACK_DAYS)
    bars = client.get_bars(symbol, timeframe=TimeFrame.Day, start=start.isoformat(), end=end.isoformat(), limit=200)
    if len(bars) < 20:
        return None
    return {b["timestamp"][:10]: b["close"] for b in bars}


def main() -> int:
    try:
        supabase_env = _load_env(_SUPABASE_ENV)
        alpaca_env = _load_env(_ALPACA_ENV)
    except FileNotFoundError as exc:
        print(f"ERROR: could not load required .env: {exc}", file=sys.stderr)
        return 2

    try:
        import os
        for k, v in alpaca_env.items():
            os.environ[k] = v
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionSnapshotRequest
        from alpaca_client import AlpacaClient

        equity_client = AlpacaClient()
        option_client = OptionHistoricalDataClient(
            api_key=os.environ["ALPACA_API_KEY"], secret_key=os.environ["ALPACA_SECRET_KEY"]
        )
    except Exception as exc:
        print(f"ERROR: broker clients unavailable: {exc}", file=sys.stderr)
        return 2

    try:
        conn = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"],
            port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"],
            password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"select * from {_SCHEMA}.spreads where status = 'open'")
                open_spreads = list(cur.fetchall())
    except Exception as exc:
        print(f"ERROR: local book unavailable: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not open_spreads:
        print("portfolio_greeks_paco: no open positions, nothing to record.")
        return 0

    all_symbols: list[str] = []
    for s in open_spreads:
        all_symbols.extend(_legs_for_spread(s))
    all_symbols = sorted(set(all_symbols))

    try:
        snap_req = OptionSnapshotRequest(symbol_or_symbols=all_symbols, feed=OptionsFeed.INDICATIVE)
        snapshots = option_client.get_option_snapshot(snap_req)
    except Exception as exc:
        print(f"ERROR: option snapshot fetch failed: {exc}", file=sys.stderr)
        return 2

    spy_closes = None
    try:
        spy_closes = _closes_by_date(equity_client, "SPY")
    except Exception as exc:
        print(f"WARNING: SPY bars unavailable ({exc}); beta-weighted delta will be partial.", file=sys.stderr)
    spy_returns = _returns_from_closes(spy_closes) if spy_closes else {}
    spy_price = spy_closes[max(spy_closes)] if spy_closes else None

    beta_by_underlying: dict[str, float] = {}
    price_by_underlying: dict[str, float] = {}
    for underlying in sorted({s["symbol"] for s in open_spreads}):
        try:
            closes = _closes_by_date(equity_client, underlying)
        except Exception:
            closes = None
        if not closes:
            print(f"portfolio_greeks_paco: no bars for {underlying}, beta defaults to {BETA_DEFAULT}")
            beta_by_underlying[underlying] = BETA_DEFAULT
            price_by_underlying[underlying] = None
            continue
        price_by_underlying[underlying] = closes[max(closes)]
        beta = _compute_beta(_returns_from_closes(closes), spy_returns) if spy_returns else None
        if beta is None:
            print(f"portfolio_greeks_paco: insufficient overlap to compute beta for {underlying}, defaulting to {BETA_DEFAULT}")
        beta_by_underlying[underlying] = beta if beta is not None else BETA_DEFAULT

    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    per_spread: list[dict] = []
    legs_missing_greeks = 0
    beta_weighted_delta = 0.0
    beta_weighted_delta_complete = spy_price is not None

    leg_role_signs = [("short_leg", -1), ("long_leg", 1), ("call_short_leg", -1), ("call_long_leg", 1)]

    for s in open_spreads:
        contracts = int(s.get("contracts") or 1)
        spread_net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
        for field, sign in leg_role_signs:
            symbol = s.get(field)
            if not symbol:
                continue
            snap = snapshots.get(symbol)
            greeks = getattr(snap, "greeks", None) if snap else None
            if greeks is None:
                legs_missing_greeks += 1
                continue
            spread_net["delta"] += sign * float(greeks.delta or 0.0) * contracts
            spread_net["gamma"] += sign * float(greeks.gamma or 0.0) * contracts
            spread_net["theta"] += sign * float(greeks.theta or 0.0) * contracts
            spread_net["vega"] += sign * float(greeks.vega or 0.0) * contracts
            spread_net["rho"] += sign * float(greeks.rho or 0.0) * contracts

        for g in net:
            net[g] += spread_net[g]

        underlying_price = price_by_underlying.get(s["symbol"])
        spread_beta_weighted = None
        if spy_price is not None and underlying_price:
            share_equiv_delta = spread_net["delta"] * 100
            dollar_delta = share_equiv_delta * underlying_price
            spread_beta_weighted = dollar_delta / spy_price * beta_by_underlying[s["symbol"]]
            beta_weighted_delta += spread_beta_weighted
        else:
            beta_weighted_delta_complete = False

        per_spread.append({
            "spread_id": s["id"], "underlying": s["symbol"],
            "strategy": s.get("strategy", "vertical"), **{k: round(v, 4) for k, v in spread_net.items()},
            "beta": round(beta_by_underlying.get(s["symbol"], BETA_DEFAULT), 3),
            "beta_weighted_delta": round(spread_beta_weighted, 2) if spread_beta_weighted is not None else None,
        })

    if legs_missing_greeks:
        print(f"portfolio_greeks_paco: {legs_missing_greeks} leg(s) had no broker greeks available "
              "(indicative feed only populates greeks for held positions -- can lag right after a fresh open)")
    if not beta_weighted_delta_complete:
        print("portfolio_greeks_paco: beta-weighted delta is a partial sum (missing price data for some underlying)")

    try:
        conn2 = psycopg2.connect(
            host=supabase_env["SUPABASE_DB_HOST"],
            port=supabase_env.get("SUPABASE_DB_PORT", "5432"),
            dbname=supabase_env.get("SUPABASE_DB_NAME", "postgres"),
            user=supabase_env["SUPABASE_DB_USER"],
            password=supabase_env["SUPABASE_DB_PASSWORD"],
            sslmode="require",
        )
        import json
        with conn2, conn2.cursor() as cur:
            cur.execute(
                f"""insert into {_SCHEMA}.portfolio_greeks_snapshots
                    (net_delta, net_gamma, net_theta, net_vega, net_rho, per_spread, beta_weighted_delta)
                    values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                (
                    round(net["delta"], 4), round(net["gamma"], 4), round(net["theta"], 4),
                    round(net["vega"], 4), round(net["rho"], 4), json.dumps(per_spread),
                    round(beta_weighted_delta, 2) if beta_weighted_delta_complete else None,
                ),
            )
            new_id = cur.fetchone()[0]
        conn2.close()
        print(f"portfolio_greeks_paco: recorded snapshot id={new_id}, "
              f"net_delta={net['delta']:.3f} net_theta={net['theta']:.3f} net_vega={net['vega']:.3f}")
        return 0
    except Exception as exc:
        print(f"ERROR: could not write snapshot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
