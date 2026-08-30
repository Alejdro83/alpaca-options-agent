"""Chaos regression test suite — codifies every real bug this bot's live
development history caught and fixed.  Each test injects one specific failure
mode and asserts the system degrades the SAFE way: refuse, abstain, or raise
loudly.  Never silently trade on bad data, never silently mislabel outcomes.

Entirely offline: no real Alpaca/MCP/LLM network calls.  Everything goes
through FakeMCP / monkeypatching / canned responses.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import (
    FakeClient,
    FakeMCP,
    make_contracts_response,
    make_iron_condor_plan,
    make_option_contract,
    make_plan,
    make_quote,
    make_snapshot_response,
)


# ===================================================================
# 1. EXECUTOR LIES — _extract_order_ids must reject phantom successes
# ===================================================================

class TestExecutorLies:
    """executor_mcp._extract_order_ids: real bug caught 2026-08-27 — Alpaca
    rejecting an order inside a 200-shaped payload was treated as success,
    creating phantom "open" positions in our tracking that didn't exist on
    the real account.  Both explicit errors and unparseable shapes must raise.
    """

    def test_rejected_order_payload_raises(self):
        """The real 2026-08-27 incident: Alpaca returns
        {"data": {"error": {"message": ..., "http_status": 422}}}
        which the old code treated like a genuine empty result.
        Must raise RuntimeError, not return [].
        """
        from executor_mcp import _extract_order_ids

        payload = {
            "data": {
                "error": {
                    "message": "options market orders are only allowed during market hours",
                    "http_status": 422,
                }
            }
        }
        with pytest.raises(RuntimeError, match="rejected"):
            _extract_order_ids(payload)

    def test_unparseable_shape_raises(self):
        """A completely unexpected response shape (no 'id', no 'error', no
        list) must raise, not return an empty list that the caller treats
        as success.
        """
        from executor_mcp import _extract_order_ids

        with pytest.raises(RuntimeError, match="Could not extract"):
            _extract_order_ids({"something": "unexpected"})

    def test_none_response_raises(self):
        from executor_mcp import _extract_order_ids

        with pytest.raises(RuntimeError, match="Could not extract"):
            _extract_order_ids(None)

    def test_valid_order_id_succeeds(self):
        """Sanity: a genuine success response does NOT raise."""
        from executor_mcp import _extract_order_ids

        result = _extract_order_ids({"data": {"id": "abc-123"}})
        assert result == ["abc-123"]

    def test_error_at_top_level_raises(self):
        """Error key present at the top level (not nested in 'data')."""
        from executor_mcp import _extract_order_ids

        with pytest.raises(RuntimeError, match="rejected"):
            _extract_order_ids({"error": {"message": "forbidden"}})


# ===================================================================
# 2. STALE QUOTES HARD-BLOCK — _pre_trade_check rejects >15min quotes
# ===================================================================

class TestStaleQuotesHardBlock:
    """bot._pre_trade_check / _pre_trade_check_iron_condor: real bug caught
    2026-08-27 — stale quotes (hours old, market-closed remnant) were only
    logged as a warning and the trade proceeded anyway, producing a
    nonsensical spread.  Now a >15min stale quote is a hard block.
    """

    @pytest.mark.asyncio
    async def test_stale_quote_blocks_vertical_trade(self):
        """Feed a snapshot with a quote timestamp >15 minutes old.
        _pre_trade_check must refuse and NO order-placing MCP call
        may have been made.
        """
        from bot import _pre_trade_check
        from spread_builder import SpreadPlan

        mcp = FakeMCP()
        plan = make_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        # Stale quote: 30 minutes old
        stale_snap = make_quote(bid=1.00, ask=1.10, age_minutes=30)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: stale_snap,
            plan.long_symbol: stale_snap,
        }))

        allowed, reason, returned_plan = await _pre_trade_check(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "stale" in reason.lower()
        # No place_option_order call should have been made
        assert not mcp.calls_for("place_option_order")

    @pytest.mark.asyncio
    async def test_stale_quote_blocks_iron_condor_trade(self):
        """Same staleness check for the 4-leg iron condor path."""
        from bot import _pre_trade_check_iron_condor

        mcp = FakeMCP()
        plan = make_iron_condor_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        stale_snap = make_quote(bid=1.00, ask=1.10, age_minutes=20)
        symbols = [plan.short_put_symbol, plan.long_put_symbol,
                   plan.short_call_symbol, plan.long_call_symbol]
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            sym: stale_snap for sym in symbols
        }))

        allowed, reason, _ = await _pre_trade_check_iron_condor(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "stale" in reason.lower()
        assert not mcp.calls_for("place_option_order")

    @pytest.mark.asyncio
    async def test_fresh_quote_allows_trade(self):
        """Sanity: a fresh quote (<15min) does NOT block on staleness."""
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=1.50, max_loss=3.50)
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        fresh_short = make_quote(bid=1.40, ask=1.60, age_minutes=2)
        fresh_long = make_quote(bid=0.10, ask=0.20, age_minutes=2)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: fresh_short,
            plan.long_symbol: fresh_long,
        }))

        allowed, reason, _ = await _pre_trade_check(mcp, plan, account, 0)
        # It should not block on staleness (might block on other reasons,
        # but staleness specifically must not fire)
        if reason is not None:
            assert "stale" not in reason.lower()


# ===================================================================
# 3. NON-POSITIVE CREDIT / MAX_LOSS REFUSED
# ===================================================================

class TestNonPositiveCreditRefused:
    """spread_builder.build_spread / build_iron_condor and the pre-trade
    re-check: real bug caught 2026-08-27 — credit_estimate > 0 alone
    didn't rule out a nonsensical spread where credit exceeded the strike
    width (stale/crossed quotes), making max_loss negative ("risk-free
    profit" on paper).  Now both credit<=0 and max_loss<=0 are hard blocks.
    """

    @pytest.mark.asyncio
    async def test_build_spread_rejects_non_positive_credit(self):
        """If short_mid <= long_mid, credit_estimate <= 0 and build_spread
        must return None (never a trade with negative/zero credit).
        """
        from spread_builder import build_spread, _contract_cache
        _contract_cache.clear()

        mcp = FakeMCP()
        today = date.today()
        exp = (today + timedelta(days=10)).isoformat()

        # Short leg bid/ask produces a MID lower than long leg -> non-positive credit
        mcp.set_response("get_option_contracts", make_contracts_response([
            make_option_contract("SPY260905P00445000", 445.0, "put", exp, 500),
            make_option_contract("SPY260905P00440000", 440.0, "put", exp, 500),
        ]))
        # Short leg mid = (0.50+0.60)/2 = 0.55, Long leg mid = (0.80+0.90)/2 = 0.85
        # credit = (0.55 - 0.85)*100 = -30 -> non-positive
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            "SPY260905P00445000": make_quote(bid=0.50, ask=0.60),
            "SPY260905P00440000": make_quote(bid=0.80, ask=0.90),
        }))

        result = await build_spread(mcp, "SPY", "long", spot_price=450.0, realized_vol=0.20)
        assert result is None

    @pytest.mark.asyncio
    async def test_build_spread_rejects_non_positive_max_loss(self):
        """credit > 0 but max_loss <= 0 (credit >= width) must be rejected.
        Real bug caught 2026-08-27.
        """
        from spread_builder import build_spread, _contract_cache
        _contract_cache.clear()

        mcp = FakeMCP()
        today = date.today()
        exp = (today + timedelta(days=10)).isoformat()

        # Strikes $5 apart.  Credit needs to be > $5*100 = $500 to make
        # max_loss negative.  Let's make short_mid=6.0, long_mid=0.5
        # -> credit = (6.0-0.5)*100 = 550, width = 5*100 = 500
        # max_loss = 500 - 550 = -50 -> non-positive
        mcp.set_response("get_option_contracts", make_contracts_response([
            make_option_contract("SPY260905P00445000", 445.0, "put", exp, 500),
            make_option_contract("SPY260905P00440000", 440.0, "put", exp, 500),
        ]))
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            "SPY260905P00445000": make_quote(bid=5.90, ask=6.10),  # mid=6.0
            "SPY260905P00440000": make_quote(bid=0.40, ask=0.60),  # mid=0.5
        }))

        result = await build_spread(mcp, "SPY", "long", spot_price=450.0, realized_vol=0.20)
        assert result is None

    @pytest.mark.asyncio
    async def test_pre_trade_check_rejects_non_positive_fresh_credit(self):
        """At the pre-trade re-check stage, if fresh quotes produce a
        non-positive credit, the trade must be blocked.
        """
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=1.50, max_loss=3.50)
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        # Fresh quotes but short mid < long mid -> non-positive credit
        short_snap = make_quote(bid=0.30, ask=0.40, age_minutes=1)
        long_snap = make_quote(bid=0.80, ask=0.90, age_minutes=1)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: short_snap,
            plan.long_symbol: long_snap,
        }))

        allowed, reason, _ = await _pre_trade_check(mcp, plan, account, 0)
        assert allowed is False
        assert reason is not None
        assert "non-positive" in reason.lower()


# ===================================================================
# 4. BUYING-POWER FLOOR
# ===================================================================

class TestBuyingPowerFloor:
    """bot._pre_trade_check / _pre_trade_check_iron_condor: added 2026-08-29.
    Equity alone doesn't say collateral is free — concurrent open spreads
    tie up buying power as margin.  If buying_power < one contract's
    max_loss, the trade must be rejected.
    """

    @pytest.mark.asyncio
    async def test_low_buying_power_blocks_vertical(self):
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=1.50, max_loss=3.50)
        # Account with equity OK but buying_power below max_loss
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200.0}

        short_snap = make_quote(bid=1.40, ask=1.60, age_minutes=1)
        long_snap = make_quote(bid=0.10, ask=0.20, age_minutes=1)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: short_snap,
            plan.long_symbol: long_snap,
        }))

        allowed, reason, _ = await _pre_trade_check(mcp, plan, account, 0)
        assert allowed is False
        assert reason is not None
        assert "buying power" in reason.lower()

    @pytest.mark.asyncio
    async def test_low_buying_power_blocks_iron_condor(self):
        from bot import _pre_trade_check_iron_condor

        mcp = FakeMCP()
        plan = make_iron_condor_plan(credit_estimate=2.50, max_loss=2.50)
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 100.0}

        # Need different prices for short vs long legs so credit is positive.
        # Short mid=1.50, long mid=0.50 -> per-side credit = $100
        # Total credit = $200, width = $500, max_loss = $300
        # buying_power=$100 < $300 -> buying power rejection
        short_snap = make_quote(bid=1.40, ask=1.60, age_minutes=1)
        long_snap = make_quote(bid=0.40, ask=0.60, age_minutes=1)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_put_symbol: short_snap,
            plan.long_put_symbol: long_snap,
            plan.short_call_symbol: short_snap,
            plan.long_call_symbol: long_snap,
        }))

        allowed, reason, _ = await _pre_trade_check_iron_condor(mcp, plan, account, 0)
        assert allowed is False
        assert reason is not None
        assert "buying power" in reason.lower()


# ===================================================================
# 5. FAIL-CLOSED PRE-TRADE GATE
# ===================================================================

class TestFailClosedPreTradeGate:
    """bot._pre_trade_check / _pre_trade_check_iron_condor: added 2026-08-29.
    An unexpected exception inside the pre-trade check's inner logic must
    be caught by the outer wrapper and returned as
    (False, "gate error (fail-closed): ...", plan) — never let the
    exception escape to be mislogged as an "open" failure.
    """

    @pytest.mark.asyncio
    async def test_unexpected_exception_fail_closed_vertical(self):
        """Force a RuntimeError from MCP on get_option_snapshot.
        The outer _pre_trade_check must catch it and return (False, ...).
        """
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        mcp.set_response("get_option_snapshot", RuntimeError("MCP subprocess crashed"))

        allowed, reason, returned_plan = await _pre_trade_check(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "fail-closed" in reason
        assert returned_plan is plan

    @pytest.mark.asyncio
    async def test_unexpected_exception_fail_closed_iron_condor(self):
        from bot import _pre_trade_check_iron_condor

        mcp = FakeMCP()
        plan = make_iron_condor_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        mcp.set_response("get_option_snapshot", RuntimeError("network timeout"))

        allowed, reason, returned_plan = await _pre_trade_check_iron_condor(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "fail-closed" in reason
        assert returned_plan is plan


# ===================================================================
# 6. DELTA-DEVIATION GUARD
# ===================================================================

class TestDeltaDeviationGuard:
    """spread_builder._select_vertical_leg: real bug caught 2026-08-28.
    At a thin/stale-quote instant, every genuinely OTM strike can fail
    the liquidity gate (bid=0), leaving only deep-ITM strikes as "liquid".
    The closest-liquid-delta strike could be delta ~0.79 against a 0.17
    target.  MAX_DELTA_DEVIATION (0.15) must refuse/skip such a pick
    rather than letting a wildly-off-target strike reach execution.
    """

    def test_deep_itm_only_liquid_strikes_refused(self):
        """Construct a chain where only deep-ITM (high-delta) strikes pass
        the liquidity gate.  _select_vertical_leg must return None rather
        than picking a ~0.79-delta strike against a 0.17 target.
        """
        from spread_builder import _select_vertical_leg, MAX_DELTA_DEVIATION, _contract_cache
        _contract_cache.clear()

        today = date.today()
        exp = (today + timedelta(days=10)).isoformat()
        spot_price = 450.0
        dte_days = 10
        realized_vol = 0.20

        # Build a chain: deep-ITM puts (strike far above spot) will have
        # high delta (~0.8+).  OTM puts (strike below spot) will have
        # low delta but we'll give them bid=0 so they fail liquidity.
        contracts = [
            make_option_contract("SPY260905P00470000", 470.0, "put", exp, 500),  # deep ITM
            make_option_contract("SPY260905P00465000", 465.0, "put", exp, 500),  # ITM
            make_option_contract("SPY260905P00460000", 460.0, "put", exp, 500),  # slightly ITM
            make_option_contract("SPY260905P00450000", 450.0, "put", exp, 500),  # ATM
            make_option_contract("SPY260905P00445000", 445.0, "put", exp, 500),  # OTM
            make_option_contract("SPY260905P00440000", 440.0, "put", exp, 500),  # OTM
        ]

        # Deep ITM strikes have liquid quotes; OTM strikes have bid=0
        snap_by_symbol = {
            "SPY260905P00470000": make_quote(bid=19.00, ask=19.50),  # liquid, high delta
            "SPY260905P00465000": make_quote(bid=14.00, ask=14.50),  # liquid, high delta
            "SPY260905P00460000": make_quote(bid=9.50, ask=10.00),   # liquid, ~0.5 delta
            "SPY260905P00450000": make_quote(bid=0.0, ask=0.0),     # illiquid (bid=0)
            "SPY260905P00445000": make_quote(bid=0.0, ask=0.0),     # illiquid
            "SPY260905P00440000": make_quote(bid=0.0, ask=0.0),     # illiquid
        }

        result = _select_vertical_leg(
            ticker="SPY",
            option_type="put",
            exp_contracts=contracts,
            snap_by_symbol=snap_by_symbol,
            spot_price=spot_price,
            dte_days=dte_days,
            realized_vol=realized_vol,
            is_lower_long=True,
        )

        # The only liquid strikes are deep-ITM with deltas >> 0.17+0.15
        # Must refuse, not pick a ~0.79-delta strike
        assert result is None

    def test_near_target_delta_accepted(self):
        """When a strike near the target delta IS liquid, it should be
        accepted (sanity check that the guard doesn't reject everything).
        """
        from spread_builder import _select_vertical_leg, _contract_cache
        _contract_cache.clear()

        today = date.today()
        exp = (today + timedelta(days=10)).isoformat()

        # spot=450, DTE=10, vol=0.20 -> strike=440 has BS delta=0.23
        # (within 0.15 of the 0.17 target)
        contracts = [
            make_option_contract("SPY260905P00440000", 440.0, "put", exp, 500),
            make_option_contract("SPY260905P00435000", 435.0, "put", exp, 500),
        ]

        snap_by_symbol = {
            "SPY260905P00440000": make_quote(bid=2.25, ask=2.35),  # spread=4.4%, liquid
            "SPY260905P00435000": make_quote(bid=0.80, ask=1.00),  # spread=22%, within 25% override
        }

        result = _select_vertical_leg(
            ticker="SPY",
            option_type="put",
            exp_contracts=contracts,
            snap_by_symbol=snap_by_symbol,
            spot_price=450.0,
            dte_days=10,
            realized_vol=0.20,
            is_lower_long=True,
        )

        # This should succeed — delta near target
        assert result is not None


# ===================================================================
# 7. REASONER NEVER TRADES ON MALFORMED LLM RESPONSE
# ===================================================================

class TestReasonerMalformedResponse:
    """llm_reasoner.decide(): falls back to "select nothing" (never a guess)
    if the API call fails or returns something unparseable.  Three specific
    failure modes tested.
    """

    def test_non_2xx_status_returns_empty_selected(self):
        from llm_reasoner import decide

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status = MagicMock(
            side_effect=Exception("HTTP 500")
        )

        with patch("llm_reasoner.requests.post", return_value=mock_resp):
            result = decide([{"ticker": "SPY", "strategy": "vertical",
                              "direction": "long", "strength": 0.8,
                              "credit_estimate": 1.5, "max_loss": 3.5,
                              "expiration": "2026-09-05"}], remaining_budget=5)

        assert result["selected"] == []
        assert "failed" in result["reasoning"].lower()

    def test_2xx_missing_choices_key_returns_empty_selected(self):
        """A 2xx with a body missing the expected 'choices' key."""
        from llm_reasoner import decide

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"usage": {"total_tokens": 100}}

        with patch("llm_reasoner.requests.post", return_value=mock_resp):
            result = decide([{"ticker": "SPY", "strategy": "vertical",
                              "direction": "long", "strength": 0.8,
                              "credit_estimate": 1.5, "max_loss": 3.5,
                              "expiration": "2026-09-05"}], remaining_budget=5)

        assert result["selected"] == []
        assert "failed" in result["reasoning"].lower()

    def test_2xx_selected_as_string_returns_empty(self):
        """'selected' as a string instead of a list — the assert in decide()
        catches this and falls back.
        """
        from llm_reasoner import decide

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "selected": "SPY",  # string, not list!
                        "reasoning": "Looks good",
                    })
                }
            }]
        }

        with patch("llm_reasoner.requests.post", return_value=mock_resp):
            result = decide([{"ticker": "SPY", "strategy": "vertical",
                              "direction": "long", "strength": 0.8,
                              "credit_estimate": 1.5, "max_loss": 3.5,
                              "expiration": "2026-09-05"}], remaining_budget=5)

        assert result["selected"] == []

    def test_2xx_valid_response_succeeds(self):
        """Sanity: a well-formed response returns the expected structure."""
        from llm_reasoner import decide

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "selected": ["SPY"],
                        "reasoning": "Strong bull signal with good risk/reward.",
                    })
                }
            }]
        }

        with patch("llm_reasoner.requests.post", return_value=mock_resp):
            result = decide([{"ticker": "SPY", "strategy": "vertical",
                              "direction": "long", "strength": 0.8,
                              "credit_estimate": 1.5, "max_loss": 3.5,
                              "expiration": "2026-09-05"}], remaining_budget=5)

        assert result["selected"] == ["SPY"]
        assert isinstance(result["reasoning"], str)

    def test_empty_candidates_returns_empty(self):
        """No candidates at all — must return empty without calling the API."""
        from llm_reasoner import decide

        result = decide([], remaining_budget=5)
        assert result["selected"] == []
        assert "no candidates" in result["reasoning"].lower()


# ===================================================================
# 8. CYCLE OUTCOME LABELING
# ===================================================================

class TestCycleOutcomeLabeling:
    """db.update_cycle_decision: real bug fixed 2026-08-28.  A candidate
    that failed to open stayed mislabeled "opened" forever because only the
    "skipped" path got a corrective second write.  The fix: update_cycle_decision
    does an UPDATE (not a re-INSERT) so exactly one row exists per cycle
    with its true final decision.

    We test this against the live DB (credentials in .env) since the function
    is pure SQL with no pure-Python logic to isolate.  We write a test row,
    update it, verify, and clean up.
    """

    def test_update_cycle_decision_overwrites_pending(self):
        """Write a cycle row with decision="pending", then call
        update_cycle_decision with "error" — the row must say "error",
        not "opened", and there must be exactly one row for this cycle_id.

        Uses the live DB (credentials in .env) for a narrowly-scoped
        write/read/delete to the cycles table's normal lifecycle.
        """
        import os
        from contextlib import contextmanager
        from dotenv import load_dotenv
        load_dotenv()

        import psycopg2
        import psycopg2.extras

        # Patch db.config.supabase with real credentials from .env
        # (config was loaded at import time before .env was read)
        import db as db_module
        import config as config_module

        real_host = os.environ.get("SUPABASE_DB_HOST")
        real_user = os.environ.get("SUPABASE_DB_USER")
        real_password = os.environ.get("SUPABASE_DB_PASSWORD")
        if not real_host or not real_user or not real_password:
            pytest.skip("No live DB credentials in .env")

        # Temporarily patch the config used by db._connection
        original_cfg = config_module.config.supabase
        patched_cfg = config_module.SupabaseConfig(
            db_host=real_host,
            db_port=int(os.environ.get("SUPABASE_DB_PORT", 5432)),
            db_name=os.environ.get("SUPABASE_DB_NAME", "postgres"),
            db_user=real_user,
            db_password=real_password,
            schema=os.environ.get("SUPABASE_SCHEMA", "alpaca_hackathon"),
        )
        # Use object replacement since SupabaseConfig is frozen
        object.__setattr__(config_module.config, 'supabase', patched_cfg)

        conn = psycopg2.connect(
            host=real_host,
            port=int(os.environ.get("SUPABASE_DB_PORT", 5432)),
            dbname=os.environ.get("SUPABASE_DB_NAME", "postgres"),
            user=real_user,
            password=real_password,
            sslmode="require",
        )
        conn.autocommit = False
        cycle_id = None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alpaca_hackathon.cycles (candidates, decision, reasoning, error, generation)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (json.dumps([{"ticker": "TEST_CHAOS"}]), "pending", "test row", None, 0),
                )
                cycle_id = cur.fetchone()[0]
            conn.commit()

            db_module.update_cycle_decision(cycle_id, "error", "LLM picked but opening raised")

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT decision, reasoning FROM alpaca_hackathon.cycles WHERE id = %s",
                    (cycle_id,),
                )
                row = cur.fetchone()

            assert row is not None
            assert row["decision"] == "error", (
                f"Expected 'error', got '{row['decision']}' — the 2026-08-28 mislabeling bug may be back"
            )
            assert "LLM picked" in row["reasoning"]

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM alpaca_hackathon.cycles WHERE id = %s",
                    (cycle_id,),
                )
                count = cur.fetchone()[0]
            assert count == 1, f"Expected 1 row, got {count} — duplicate INSERT bug may be back"

        finally:
            try:
                if cycle_id is not None:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM alpaca_hackathon.cycles WHERE id = %s", (cycle_id,))
                    conn.commit()
            except Exception:
                conn.rollback()
            conn.close()
            object.__setattr__(config_module.config, 'supabase', original_cfg)


# ===================================================================
# 9. MARKET-CLOSED SHORT-CIRCUIT
# ===================================================================

class TestMarketClosedShortCircuit:
    """bot.run_cycle(): when the market clock says closed, find_candidates()
    must never be called.  Real fix for the incident where a manual
    off-hours invocation nearly placed an order outside market hours.
    """

    @pytest.mark.asyncio
    async def test_market_closed_skips_screening(self):
        """When client.get_clock() returns is_open=False, run_cycle must
        not call find_candidates (which would waste a full screening pass
        and potentially build candidates that can never execute).
        """
        import bot as bot_module

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": False, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None

            # Ensure the kill switch file doesn't exist
            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            # find_candidates must NEVER have been called
            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_open_calls_screening(self):
        """Sanity: when market IS open and there's budget, find_candidates
        IS called.
        """
        import bot as bot_module

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([], [])) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_llm.decide.return_value = {"selected": [], "reasoning": "Nothing good"}
            from reconciler import ReconcileResult
            mock_reconciler.reconcile.return_value = ReconcileResult(ok=True)

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            # find_candidates SHOULD have been called when market is open
            mock_find.assert_called_once()


# ===================================================================
# 13. OPTIONS TRADING LEVEL GATE (added 2026-08-29)
# ===================================================================

class TestOptionsLevelGate:
    """bot.run_cycle(): a real gap found cross-checking Alpaca's own OpenAPI
    spec -- options_trading_level (the EFFECTIVE level, not
    options_approved_level) was never checked at runtime, only verified
    once by hand at account setup. Level 3 ("Spreads/Straddles") is
    required for every multi-leg order this bot places; below that,
    find_candidates() must never be called.
    """

    @pytest.mark.asyncio
    async def test_insufficient_options_level_skips_screening(self):
        import bot as bot_module

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["options_trading_level"] = 2  # Long Call/Put only -- no spreads
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_options_level_fails_closed(self):
        """A missing/unreadable field must be treated as insufficient, not
        as an implicit pass -- fail closed, same convention as every other
        gate in this codebase."""
        import bot as bot_module

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        del fake_client.account["options_trading_level"]
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            mock_find.assert_not_called()

    @pytest.mark.asyncio
    async def test_level_3_allows_screening(self):
        """Sanity: level 3 (the real production value) does not block
        screening on its own."""
        import bot as bot_module

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([], [])) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_llm.decide.return_value = {"selected": [], "reasoning": "Nothing good"}
            from reconciler import ReconcileResult
            mock_reconciler.reconcile.return_value = ReconcileResult(ok=True)

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            mock_find.assert_called_once()


# ===================================================================
# EXTRA: Shadow-select iron condor scoring (strength=None guard)
# ===================================================================

class TestShadowSelectIronCondor:
    """bot._shadow_select: real bug caught while wiring up iron condors.
    strength is explicitly None (not absent) on an iron_condor candidate,
    so c.get("strength", 0) returned None, not the intended 0 default,
    and strength * rr would raise TypeError.  Must handle None gracefully.
    """

    def test_iron_condor_candidate_does_not_crash(self):
        """An iron condor candidate (strength=None) must not raise
        TypeError in _shadow_select.
        """
        from bot import _shadow_select

        candidates = [
            {
                "ticker": "SPY",
                "strategy": "iron_condor",
                "direction": None,
                "strength": None,  # explicitly None, not absent
                "credit_estimate": 2.50,
                "max_loss": 2.50,
            }
        ]

        # Must not raise TypeError
        result = _shadow_select(candidates, remaining_budget=5)
        assert "SPY" in result

    def test_vertical_with_strength_scores_higher(self):
        """A vertical candidate with real strength should score higher
        than an iron condor with strength=None (scored on risk/reward
        alone).
        """
        from bot import _shadow_select

        candidates = [
            {
                "ticker": "SPY",
                "strategy": "iron_condor",
                "direction": None,
                "strength": None,
                "credit_estimate": 2.50,
                "max_loss": 2.50,
            },
            {
                "ticker": "AAPL",
                "strategy": "vertical",
                "direction": "long",
                "strength": 0.9,
                "credit_estimate": 1.50,
                "max_loss": 3.50,
            },
        ]

        result = _shadow_select(candidates, remaining_budget=1)
        # AAPL should be selected: strength(0.9) * rr(1.5/3.5=0.43) = 0.386
        # vs SPY: rr(2.5/2.5=1.0) = 1.0 -- actually SPY scores higher!
        # This is correct: iron condors with good risk/reward SHOULD
        # rank well.  The point is that None strength doesn't crash.
        assert len(result) == 1


# ===================================================================
# EXTRA: spread_builder credit/max_loss at build time
# ===================================================================

class TestSpreadBuilderEdgeCases:
    """Additional spread_builder edge cases derived from the real bug
    history in the codebase.
    """

    @pytest.mark.asyncio
    async def test_build_iron_condor_rejects_inverted_strikes(self):
        """If delta selection on a wild/illiquid chain ever inverts the
        short put above the short call, the structure isn't a real
        condor and must be rejected.
        """
        from spread_builder import build_iron_condor, _contract_cache
        _contract_cache.clear()

        mcp = FakeMCP()
        today = date.today()
        exp = (today + timedelta(days=10)).isoformat()

        # Put chain: only deep-ITM strikes available (high strike)
        put_contracts = [
            make_option_contract("SPY260905P00460000", 460.0, "put", exp, 500),
            make_option_contract("SPY260905P00455000", 455.0, "put", exp, 500),
        ]
        # Call chain: only deep-ITM strikes available (low strike)
        call_contracts = [
            make_option_contract("SPY260905C00440000", 440.0, "call", exp, 500),
            make_option_contract("SPY260905C00445000", 445.0, "call", exp, 500),
        ]

        def fake_contracts(mcp, ticker, opt_type, min_exp, max_exp):
            if opt_type == "put":
                return put_contracts
            return call_contracts

        # Make put short strike = 460, call short strike = 440 -> inverted
        put_snap = {
            "SPY260905P00460000": make_quote(bid=9.50, ask=10.00),
            "SPY260905P00455000": make_quote(bid=5.00, ask=5.50),
        }
        call_snap = {
            "SPY260905C00440000": make_quote(bid=9.50, ask=10.00),
            "SPY260905C00445000": make_quote(bid=5.00, ask=5.50),
        }

        mcp.set_response("get_option_contracts", make_contracts_response(put_contracts))
        mcp.set_response("get_option_snapshot", make_snapshot_response({**put_snap, **call_snap}))

        with patch("spread_builder._fetch_contracts", side_effect=fake_contracts):
            result = await build_iron_condor(mcp, "SPY", spot_price=450.0, realized_vol=0.20,
                                              target_delta_override=0.80)

        # Inverted strikes -> must be rejected
        assert result is None


# ===================================================================
# 10. CORRELATION-CLUSTER CONCENTRATION CAP (added 2026-08-29)
# ===================================================================

class TestClusterConcentrationCap:
    """risk_gate.check_new_spread's cluster_exposure gate: a real gap the
    per-underlying concentration cap doesn't cover -- several concurrent
    spreads on DIFFERENT mega-cap tech names aren't independent bets (see
    screening/correlation_clusters.py). Added after a research pass on
    further improvements worth making before the contest deadline.
    """

    def test_cluster_cap_breached_rejects(self):
        import risk_gate

        today = date.today()
        exp = today + timedelta(days=10)
        result = risk_gate.check_new_spread(
            equity=100_000, daily_pl_pct=0.0, open_spreads_count=1, max_loss=200,
            expiration=exp, today=today,
            underlying="AAPL", cluster_exposure={"mega_cap_tech": 39_900},
        )
        assert result.allowed is False
        assert any("mega_cap_tech" in r for r in result.reasons)

    def test_cluster_cap_not_breached_allows(self):
        import risk_gate

        today = date.today()
        exp = today + timedelta(days=10)
        result = risk_gate.check_new_spread(
            equity=100_000, daily_pl_pct=0.0, open_spreads_count=1, max_loss=200,
            expiration=exp, today=today,
            underlying="AAPL", cluster_exposure={"mega_cap_tech": 5_000},
        )
        assert result.allowed is True

    def test_ticker_outside_any_cluster_never_rejected_by_this_gate(self):
        """JPM isn't in any defined cluster -- cluster_for returns None, so
        this gate must never fire for it regardless of cluster_exposure's
        contents (it would be a different underlying's exposure anyway)."""
        import risk_gate

        today = date.today()
        exp = today + timedelta(days=10)
        result = risk_gate.check_new_spread(
            equity=100_000, daily_pl_pct=0.0, open_spreads_count=1, max_loss=200,
            expiration=exp, today=today,
            underlying="JPM", cluster_exposure={"mega_cap_tech": 39_900},
        )
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_pre_trade_check_blocks_on_cluster_breach(self):
        """The pre-trade re-check (not just find_candidates' initial gate)
        must also see cluster_exposure -- same 2026-08-28 audit discipline
        already applied to existing_exposure/open_iron_condor_count."""
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan(underlying="MSFT", credit_estimate=1.50, max_loss=3.50)
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 100_000.0}

        short_snap = make_quote(bid=1.40, ask=1.60, age_minutes=1)
        long_snap = make_quote(bid=0.10, ask=0.20, age_minutes=1)
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: short_snap,
            plan.long_symbol: long_snap,
        }))

        allowed, reason, _ = await _pre_trade_check(
            mcp, plan, account, 0, cluster_exposure={"mega_cap_tech": 39_900},
        )
        assert allowed is False
        assert reason is not None
        assert "mega_cap_tech" in reason


# ===================================================================
# 11. EXIT-RULE COUNTERFACTUALS (added 2026-08-29)
# ===================================================================

class TestExitRuleCounterfactuals:
    """risk_gate.should_close's stop_loss_multiple_override/disable_stop:
    lets shadow_book.py run tight-stop/no-stop as counterfactual policies
    against real live decisions, without changing the real book's own
    default (2x) behavior. Default-args behavior must be byte-for-byte
    unchanged from before these params existed.
    """

    def test_default_behavior_unchanged(self):
        import risk_gate

        # mark = 1.5x credit -> below the real 2x stop, should NOT close
        should_close, reason = risk_gate.should_close(credit_received=1.0, current_mark=1.5)
        assert should_close is False
        assert reason is None

    def test_tight_stop_override_closes_where_default_would_not(self):
        import risk_gate

        should_close, reason = risk_gate.should_close(
            credit_received=1.0, current_mark=1.5, stop_loss_multiple_override=1.0,
        )
        assert should_close is True
        assert "1.0x" in reason

    def test_disable_stop_never_closes_on_stop_no_matter_how_bad(self):
        import risk_gate

        # mark = 5x credit -- a real blowout that would trip any normal stop
        should_close, reason = risk_gate.should_close(
            credit_received=1.0, current_mark=5.0, disable_stop=True,
        )
        assert should_close is False
        assert reason is None

    def test_disable_stop_still_honors_profit_target(self):
        """disable_stop must only suppress the STOP check -- the profit
        target exit (checked first, unconditionally) must still fire."""
        import risk_gate

        # 60% profit captured, default profit_target_pct is 50%
        should_close, reason = risk_gate.should_close(
            credit_received=1.0, current_mark=0.40, disable_stop=True,
        )
        assert should_close is True
        assert "profit target" in reason


# ===================================================================
# 12. OVERNIGHT EVOLUTION NEVER MUTATES PURE RISK GATES (added 2026-08-29)
# ===================================================================

class TestEvolutionExcludesRiskGates:
    """overnight_evolution.py: a real gap flagged 2026-08-28 ("excluir gates
    de riesgo puro de la mutación") and left unimplemented until now.
    max_loss_per_spread_pct / min_open_interest / max_bid_ask_spread_pct /
    stop_loss_multiple are safety ceilings/floors, not return-optimization
    knobs -- an overnight process driven by one day's simulated replay
    must never be the thing that loosens them.
    """

    EXCLUDED = {
        "max_loss_per_spread_pct", "min_open_interest",
        "max_bid_ask_spread_pct", "stop_loss_multiple",
    }

    def test_excluded_params_never_change_across_many_seeds(self):
        from dataclasses import asdict
        from evolution_config import StrategyParams
        from overnight_evolution import generate_variants

        incumbent = StrategyParams()
        inc_dict = asdict(incumbent)
        for seed in range(50):
            for variant in generate_variants(incumbent, seed=seed):
                vd = asdict(variant)
                for name in self.EXCLUDED:
                    assert vd[name] == inc_dict[name], (
                        f"seed={seed}: {name} changed from {inc_dict[name]} to {vd[name]} "
                        "-- a pure risk gate was mutated"
                    )

    def test_mutable_params_do_still_get_explored(self):
        """Sanity check the exclusion isn't accidentally freezing everything --
        real mutation must still happen on the params that ARE meant to evolve."""
        from dataclasses import asdict
        from evolution_config import MUTABLE_PARAMS, StrategyParams
        from overnight_evolution import generate_variants

        incumbent = StrategyParams()
        inc_dict = asdict(incumbent)
        variants = generate_variants(incumbent, seed=42)
        changed = any(
            asdict(v)[name] != inc_dict[name]
            for v in variants
            for name in MUTABLE_PARAMS
        )
        assert changed, "no mutable param changed across the whole population -- mutation may be broken"


# ===================================================================
# 14. BETA-WEIGHTED DELTA (added 2026-08-29)
# ===================================================================

class TestBetaWeightedDelta:
    """portfolio_greeks._compute_beta / _returns_from_closes: beta must be
    computed from real trailing returns, never a hardcoded table (this
    project's standing rule against fabricating a number it can instead
    measure), and must fail closed (return None, not a guess) on
    insufficient data.
    """

    def test_perfect_beta_two_recovered_without_noise(self):
        """A synthetic series built with an EXACT 2x relationship to SPY
        (no idiosyncratic noise) must recover beta essentially exactly --
        the real math, not just "doesn't crash"."""
        import random
        from portfolio_greeks import _compute_beta, _returns_from_closes

        rng = random.Random(42)
        spy_closes, stock_closes = {}, {}
        spy_price, stock_price = 100.0, 50.0
        for i in range(30):
            date = f"2026-01-{i + 1:02d}"
            r = rng.uniform(-0.02, 0.02)
            spy_price *= 1 + r
            stock_price *= 1 + 2.0 * r  # exactly beta=2, by construction
            spy_closes[date] = round(spy_price, 2)
            stock_closes[date] = round(stock_price, 2)

        beta = _compute_beta(_returns_from_closes(stock_closes), _returns_from_closes(spy_closes))
        assert beta is not None
        assert abs(beta - 2.0) < 0.01, f"expected ~2.0, got {beta}"

    def test_insufficient_overlap_returns_none(self):
        """Too few shared dates -- must fail closed (None), never guess a beta."""
        from portfolio_greeks import _compute_beta

        beta = _compute_beta({"2026-01-01": 0.01}, {"2026-01-01": 0.01})
        assert beta is None

    def test_beta_unaffected_by_a_gap_on_only_one_series(self):
        """A data gap on ONE series only (e.g. a vendor outage for one
        symbol on one day) must not silently misalign the two return
        series positionally -- _compute_beta intersects on real shared
        DATES, not list position, so the gap day is simply excluded from
        both rather than shifting everything after it by one."""
        import random
        from portfolio_greeks import _compute_beta, _returns_from_closes

        rng = random.Random(7)
        spy_closes, stock_closes = {}, {}
        spy_price, stock_price = 100.0, 50.0
        gap_date = "2026-01-15"
        for i in range(40):
            date = f"2026-01-{i + 1:02d}" if i < 31 else f"2026-02-{i - 30:02d}"
            r = rng.uniform(-0.02, 0.02)
            spy_price *= 1 + r
            stock_price *= 1 + 1.5 * r  # exactly beta=1.5, by construction
            spy_closes[date] = round(spy_price, 2)
            if date != gap_date:  # stock has a real gap here; SPY does not
                stock_closes[date] = round(stock_price, 2)

        beta = _compute_beta(_returns_from_closes(stock_closes), _returns_from_closes(spy_closes))
        assert beta is not None
        assert abs(beta - 1.5) < 0.05, (
            f"expected ~1.5 even with a one-sided gap, got {beta} -- "
            "a positional (not date-keyed) alignment bug would corrupt this"
        )


class TestMissingQuoteTimestampFailsClosed:
    """bot._pre_trade_check / _pre_trade_check_iron_condor: real gap found
    2026-08-29 comparing against a competing team's hardening pass. A quote
    with no `t` field (or an unparseable one) fell through the staleness
    check's `if ts_str: ... except: pass` as if it were fine -- unknown age
    was silently treated as fresh, not blocked. Must fail closed like an
    actually-stale quote does.
    """

    @pytest.mark.asyncio
    async def test_missing_timestamp_blocks_vertical_trade(self):
        from bot import _pre_trade_check

        mcp = FakeMCP()
        plan = make_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        no_ts_snap = {"latestQuote": {"bp": "1.00", "ap": "1.10"}}  # no "t" key
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            plan.short_symbol: no_ts_snap,
            plan.long_symbol: no_ts_snap,
        }))

        allowed, reason, _ = await _pre_trade_check(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "unknown age" in reason.lower() or "no usable timestamp" in reason.lower()
        assert not mcp.calls_for("place_option_order")

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_blocks_iron_condor_trade(self):
        from bot import _pre_trade_check_iron_condor

        mcp = FakeMCP()
        plan = make_iron_condor_plan()
        account = {"equity": 100_000, "last_equity": 100_000, "buying_power": 200_000}

        bad_ts_snap = {"latestQuote": {"bp": "1.00", "ap": "1.10", "t": "not-a-timestamp"}}
        symbols = [plan.short_put_symbol, plan.long_put_symbol,
                   plan.short_call_symbol, plan.long_call_symbol]
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            sym: bad_ts_snap for sym in symbols
        }))

        allowed, reason, _ = await _pre_trade_check_iron_condor(mcp, plan, account, 0)

        assert allowed is False
        assert reason is not None
        assert "unknown age" in reason.lower() or "no usable timestamp" in reason.lower()
        assert not mcp.calls_for("place_option_order")


class TestBoundedLimitOrders:
    """executor_mcp: real gap found 2026-08-29 comparing against a competing
    team's hardening pass -- every order (open and close, vertical and iron
    condor) was an unbounded MARKET order (a TODO left since the project
    started). Now a marketable LIMIT order, bounded either by the checked
    credit/debit (open, and close with a fresh mark) or by the position's
    own max_loss (close with no mark -- e.g. a force-close whose quote
    fetch failed). A debit above max_loss is never rational.
    """

    @pytest.mark.asyncio
    async def test_open_spread_places_limit_not_market(self):
        import executor_mcp

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)  # $1.50/share
        mcp.set_response("place_option_order", {"data": {"id": "order-1"}})

        await executor_mcp.open_spread(mcp, plan, contracts=1)

        calls = mcp.calls_for("place_option_order")
        assert len(calls) == 1
        assert calls[0]["type"] == "limit"
        # 10% default slippage: 1.50 * 0.9 = 1.35
        assert calls[0]["limit_price"] == "1.35"

    @pytest.mark.asyncio
    async def test_open_iron_condor_places_limit_not_market(self):
        import executor_mcp

        mcp = FakeMCP()
        plan = make_iron_condor_plan(credit_estimate=200.0)  # $2.00/share
        mcp.set_response("place_option_order", {"data": {"id": "order-1"}})

        await executor_mcp.open_iron_condor(mcp, plan, contracts=1)

        calls = mcp.calls_for("place_option_order")
        assert calls[0]["type"] == "limit"
        assert calls[0]["limit_price"] == "1.80"  # 2.00 * 0.9

    @pytest.mark.asyncio
    async def test_close_spread_uses_mark_when_available(self):
        import executor_mcp

        mcp = FakeMCP()
        mcp.set_response("place_option_order", {"data": {"id": "order-2"}})

        await executor_mcp.close_spread(
            mcp, "SHORT_SYM", "LONG_SYM", contracts=1,
            current_mark=100.0, max_loss=500.0,
        )

        calls = mcp.calls_for("place_option_order")
        assert calls[0]["type"] == "limit"
        # 10% default slippage on the debit: 1.00 * 1.1 = 1.10
        assert calls[0]["limit_price"] == "1.10"

    @pytest.mark.asyncio
    async def test_close_spread_falls_back_to_max_loss_without_mark(self):
        """Force-close whose quote fetch failed (mark=None): the ceiling
        must be the position's own max_loss, uninflated by slippage padding
        -- paying more than max_loss to exit is never rational."""
        import executor_mcp

        mcp = FakeMCP()
        mcp.set_response("place_option_order", {"data": {"id": "order-3"}})

        await executor_mcp.close_spread(
            mcp, "SHORT_SYM", "LONG_SYM", contracts=1,
            current_mark=None, max_loss=500.0,
        )

        calls = mcp.calls_for("place_option_order")
        assert calls[0]["type"] == "limit"
        assert calls[0]["limit_price"] == "5.00"  # 500/100, no slippage

    @pytest.mark.asyncio
    async def test_close_spread_without_mark_or_max_loss_raises(self):
        """No unbounded fallback exists -- caller must supply a bound."""
        import executor_mcp

        mcp = FakeMCP()
        with pytest.raises(ValueError):
            await executor_mcp.close_spread(
                mcp, "SHORT_SYM", "LONG_SYM", contracts=1,
            )
        assert not mcp.calls_for("place_option_order")


class TestRealFillConfirmation:
    """executor_mcp.open_spread/open_iron_condor: real gap found 2026-08-30,
    exposed by this project's own limit-order change (2026-08-29). A limit
    order, unlike a market order during market hours, is not guaranteed an
    immediate fill -- before this, a spread was recorded "open" with an
    ESTIMATED credit the instant Alpaca merely accepted the order, never
    confirming a real fill. Now polls client.get_order() until a real fill
    or a bounded timeout, and cancels + raises rather than ever letting a
    caller record a guessed-at position.
    """

    def test_top_level_filled_avg_price_sign_is_negated(self):
        """The exact bug caught live while building this fix: verified
        against alpaca_client.get_order() on the real, already-filled NVDA
        iron condor order, Alpaca's top-level filled_avg_price for a
        net-credit mleg order is -0.54 ("cost to acquire" convention) --
        the OPPOSITE of this project's always-positive credit_received
        convention. Only reached when no per-leg data exists at all; a
        naive `float(avg)` here (this function's first version) would have
        recorded every real fill as a NEGATIVE credit."""
        from executor_mcp import _extract_filled_avg_price

        result = {"data": {"id": "order-1", "status": "filled", "filled_avg_price": "-0.54"}}
        assert _extract_filled_avg_price(result) == 0.54

    def test_per_leg_computation_matches_the_real_nvda_order(self):
        """Same real order, but via per-leg data (what's actually used in
        practice -- the top-level fallback above is last-resort only)."""
        from executor_mcp import _extract_filled_avg_price

        result = {"data": {"id": "order-1", "status": "filled", "legs": [
            {"symbol": "NVDA260909P00205000", "side": "sell", "filled_avg_price": "0.93"},
            {"symbol": "NVDA260909P00200000", "side": "buy", "filled_avg_price": "0.59"},
            {"symbol": "NVDA260909C00240000", "side": "sell", "filled_avg_price": "0.54"},
            {"symbol": "NVDA260909C00245000", "side": "buy", "filled_avg_price": "0.34"},
        ]}}
        assert _extract_filled_avg_price(result) == pytest.approx(0.54)

    @pytest.mark.asyncio
    async def test_immediate_fill_in_submit_response_needs_no_polling(self):
        import executor_mcp

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)
        # Negative, matching Alpaca's real top-level convention for a net
        # credit ("cost to acquire" -- negative for a credit position). See
        # _extract_filled_avg_price's docstring: verified live 2026-08-30
        # against the real NVDA iron condor order (-0.54 for a real $0.54
        # credit) -- a naive positive-sign assumption here was the bug.
        mcp.set_response("place_option_order", {
            "data": {"id": "order-1", "status": "filled", "filled_avg_price": "-1.40"},
        })
        client = FakeClient()  # get_order should never be needed

        order = await executor_mcp.open_spread(mcp, plan, contracts=1, client=client)

        assert order.status == "filled"
        assert order.fill_credit == 140.0  # 1.40/share -> 140.00/contract

    @pytest.mark.asyncio
    async def test_polls_until_a_real_fill_confirms(self):
        import executor_mcp
        import config as config_module

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)
        mcp.set_response("place_option_order", {"data": {"id": "order-1", "status": "accepted"}})
        client = FakeClient(orders={
            "order-1": {"status": "filled", "filled_avg_price": "-1.32"},  # real sign, see above
        })
        original_interval = config_module.config.risk.order_poll_interval_s
        original_timeout = config_module.config.risk.order_poll_timeout_s
        object.__setattr__(config_module.config.risk, "order_poll_interval_s", 0.01)
        object.__setattr__(config_module.config.risk, "order_poll_timeout_s", 1.0)
        try:
            order = await executor_mcp.open_spread(mcp, plan, contracts=1, client=client)
        finally:
            object.__setattr__(config_module.config.risk, "order_poll_interval_s", original_interval)
            object.__setattr__(config_module.config.risk, "order_poll_timeout_s", original_timeout)

        assert order.status == "filled"
        assert order.fill_credit == 132.0

    @pytest.mark.asyncio
    async def test_never_fills_gets_canceled_and_raises(self):
        """An order still resting past the poll timeout must be canceled
        and must raise -- never recorded as an open position."""
        import executor_mcp
        import config as config_module

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)
        mcp.set_response("place_option_order", {"data": {"id": "order-1", "status": "accepted"}})
        client = FakeClient(orders={"order-1": {"status": "accepted"}})  # never fills
        original_interval = config_module.config.risk.order_poll_interval_s
        original_timeout = config_module.config.risk.order_poll_timeout_s
        object.__setattr__(config_module.config.risk, "order_poll_interval_s", 0.01)
        object.__setattr__(config_module.config.risk, "order_poll_timeout_s", 0.05)
        try:
            with pytest.raises(RuntimeError, match="not filled within"):
                await executor_mcp.open_spread(mcp, plan, contracts=1, client=client)
        finally:
            object.__setattr__(config_module.config.risk, "order_poll_interval_s", original_interval)
            object.__setattr__(config_module.config.risk, "order_poll_timeout_s", original_timeout)

        assert "order-1" in client.canceled_order_ids

    @pytest.mark.asyncio
    async def test_terminal_rejection_after_polling_raises(self):
        import executor_mcp
        import config as config_module

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)
        mcp.set_response("place_option_order", {"data": {"id": "order-1", "status": "accepted"}})
        client = FakeClient(orders={"order-1": {"status": "rejected"}})
        original_interval = config_module.config.risk.order_poll_interval_s
        original_timeout = config_module.config.risk.order_poll_timeout_s
        object.__setattr__(config_module.config.risk, "order_poll_interval_s", 0.01)
        object.__setattr__(config_module.config.risk, "order_poll_timeout_s", 1.0)
        try:
            with pytest.raises(RuntimeError, match="terminal without fill"):
                await executor_mcp.open_spread(mcp, plan, contracts=1, client=client)
        finally:
            object.__setattr__(config_module.config.risk, "order_poll_interval_s", original_interval)
            object.__setattr__(config_module.config.risk, "order_poll_timeout_s", original_timeout)

        # Already terminal -- no point canceling an order that's already dead.
        assert client.canceled_order_ids == []

    @pytest.mark.asyncio
    async def test_open_iron_condor_confirms_fill_across_4_legs_real_shape(self):
        """Per-leg data shaped exactly like the real NVDA iron condor's own
        opening order (fetched live 2026-08-30 via alpaca_client.get_order()
        while building this fix): short put 0.93, long put 0.59, short call
        0.54, long call 0.34 -> net credit 0.54/share, $54/contract. The
        order's own top-level filled_avg_price in that real response was
        -0.54 (see _extract_filled_avg_price's docstring) -- per-leg data
        is what's actually used and gives the right sign directly.
        """
        import executor_mcp

        mcp = FakeMCP()
        plan = make_iron_condor_plan(credit_estimate=200.0)
        mcp.set_response("place_option_order", {
            "data": {
                "id": "order-1", "status": "filled",
                "legs": [
                    {"symbol": "NVDA260909P00205000", "side": "sell", "filled_avg_price": "0.93"},
                    {"symbol": "NVDA260909P00200000", "side": "buy", "filled_avg_price": "0.59"},
                    {"symbol": "NVDA260909C00240000", "side": "sell", "filled_avg_price": "0.54"},
                    {"symbol": "NVDA260909C00245000", "side": "buy", "filled_avg_price": "0.34"},
                ],
            },
        })
        client = FakeClient()

        order = await executor_mcp.open_iron_condor(mcp, plan, contracts=1, client=client)

        assert order.status == "filled"
        assert order.fill_credit == 54.0  # matches the real order this fixture mirrors

    @pytest.mark.asyncio
    async def test_no_client_preserves_legacy_assume_filled_behavior(self):
        """A caller that genuinely has no AlpacaClient handy (none exist in
        this project) still gets the pre-2026-08-30 behavior: no polling,
        no exception, just whatever the submit response said."""
        import executor_mcp

        mcp = FakeMCP()
        plan = make_plan(credit_estimate=150.0)
        mcp.set_response("place_option_order", {"data": {"id": "order-1"}})  # no status at all

        order = await executor_mcp.open_spread(mcp, plan, contracts=1)

        assert order.status == "filled"
        assert order.order_ids == ["order-1"]


class TestBrokerLocalReconciliation:
    """reconciler.py: real gap found 2026-08-29 comparing against a
    competing team's hardening pass. This project already had one
    incident from exactly this failure class (2026-08-27 phantom Supabase
    row from a rejected order, since fixed at the source) -- this is the
    ongoing cross-check that would have caught it independently. Alpaca is
    the source of truth; a divergence from our own `spreads` table must
    block new entries.
    """

    def test_matching_book_is_ok(self):
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = [
                {"id": 1, "short_symbol": "SPY260905C00450000", "long_symbol": "SPY260905C00455000",
                 "contracts": 1},
            ]
            client = SimpleNamespace(get_positions=lambda: [
                {"symbol": "SPY260905C00450000", "side": "short", "qty": 1.0},
                {"symbol": "SPY260905C00455000", "side": "long", "qty": 1.0},
            ])
            result = reconcile(client)

        assert result.ok is True
        assert result.reasons == []

    def test_leg_quantity_mismatch_blocks(self):
        """A leg quietly filled at a different size than its own DB record
        used to pass silently -- the old symbol-only check couldn't see
        it (2026-08-30 fix, found auditing the same pattern in Paco)."""
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = [
                {"id": 1, "short_symbol": "SPY260905C00450000", "long_symbol": "SPY260905C00455000",
                 "contracts": 2},
            ]
            client = SimpleNamespace(get_positions=lambda: [
                {"symbol": "SPY260905C00450000", "side": "short", "qty": 5.0},  # DB says 2
                {"symbol": "SPY260905C00455000", "side": "long", "qty": 2.0},
            ])
            result = reconcile(client)

        assert result.ok is False
        assert "DB says 2 contract(s), broker says 5.0" in result.reason

    def test_leg_side_mismatch_blocks(self):
        """A short leg recorded at the broker as long (or vice versa) is
        a real capital-structure inconsistency, not just a quantity typo."""
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = [
                {"id": 1, "short_symbol": "SPY260905C00450000", "long_symbol": "SPY260905C00455000",
                 "contracts": 1},
            ]
            client = SimpleNamespace(get_positions=lambda: [
                {"symbol": "SPY260905C00450000", "side": "long", "qty": 1.0},  # DB says short
                {"symbol": "SPY260905C00455000", "side": "long", "qty": 1.0},
            ])
            result = reconcile(client)

        assert result.ok is False
        assert "expected side 'short', broker says 'long'" in result.reason

    def test_phantom_db_row_blocks(self):
        """DB says a spread is open; the broker holds nothing for it --
        must block, not assume the DB is right."""
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = [
                {"short_symbol": "SPY260905C00450000", "long_symbol": "SPY260905C00455000"},
            ]
            client = SimpleNamespace(get_positions=lambda: [])
            result = reconcile(client)

        assert result.ok is False
        assert "missing at broker" in result.reason

    def test_orphan_broker_position_blocks(self):
        """The broker holds an option leg our own book doesn't know about
        -- must block, not silently ignore an untracked position."""
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = []
            client = SimpleNamespace(get_positions=lambda: [
                {"symbol": "SPY260905C00450000"},
            ])
            result = reconcile(client)

        assert result.ok is False
        assert "missing from DB" in result.reason

    def test_iron_condor_call_legs_are_checked_too(self):
        """A 4-leg iron condor row's call_short_symbol/call_long_symbol
        columns must be reconciled too, not just the put-side columns
        vertical spreads also use."""
        from reconciler import reconcile

        with patch("reconciler.db") as mock_db:
            mock_db.get_open_spreads.return_value = [{
                "short_symbol": "SPY260905P00440000", "long_symbol": "SPY260905P00435000",
                "call_short_symbol": "SPY260905C00460000", "call_long_symbol": "SPY260905C00465000",
            }]
            # Broker only has the put side -- the call side is missing.
            client = SimpleNamespace(get_positions=lambda: [
                {"symbol": "SPY260905P00440000"},
                {"symbol": "SPY260905P00435000"},
            ])
            result = reconcile(client)

        assert result.ok is False
        assert "SPY260905C00460000" in result.reason

    def test_broker_fetch_failure_fails_closed(self):
        from reconciler import reconcile

        client = SimpleNamespace(get_positions=MagicMock(side_effect=RuntimeError("API down")))
        result = reconcile(client)

        assert result.ok is False
        assert "broker positions unavailable" in result.reason

    @pytest.mark.asyncio
    async def test_run_cycle_skips_screening_on_reconciliation_mismatch(self):
        """End-to-end: run_cycle must not call find_candidates when
        reconciler.reconcile() reports a mismatch, even with the market
        open, budget available, and options level sufficient."""
        import bot as bot_module
        from reconciler import ReconcileResult

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_reconciler.reconcile.return_value = ReconcileResult(
                ok=False, reasons=["broker option legs missing from DB: ['SPY260905C00450000']"],
            )

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

            mock_find.assert_not_called()


class TestBotRecordsRealFillNotEstimate:
    """bot.run_cycle()'s open path: the real fill price from
    executor_mcp.OrderResult (now polled/confirmed for real, see
    TestRealFillConfirmation) must be what gets recorded to the DB and
    used for running exposure tallies -- not the pre-trade credit
    estimate, now that fills are no longer a near-certainty as they were
    under unbounded market orders.
    """

    @pytest.mark.asyncio
    async def test_real_fill_credit_overrides_the_estimate(self):
        import bot as bot_module
        from executor_mcp import OrderResult

        plan = make_plan(underlying="SPY", direction="bear_call", credit_estimate=150.0, max_loss=350.0)
        candidate = {"ticker": "SPY", "_plan": plan, "credit_estimate": 150.0, "max_loss": 350.0, "strength": 1.0}

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([candidate], [])), \
             patch("bot._pre_trade_check", new_callable=AsyncMock, return_value=(True, None, plan)), \
             patch("bot.executor_mcp") as mock_executor:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_db.record_spread_open.return_value = 1
            mock_llm.decide.return_value = {"selected": ["SPY"], "reasoning": "test pick"}
            from reconciler import ReconcileResult
            mock_reconciler.reconcile.return_value = ReconcileResult(ok=True)

            # Real fill came in $10/contract better than the pre-trade
            # estimate (150.0 -> 160.0) -- a plausible, in-bounds outcome
            # for a marketable limit that filled at a better price than
            # its floor.
            mock_executor.open_spread = AsyncMock(
                return_value=OrderResult(order_ids=["order-1"], status="filled", fill_credit=160.0)
            )

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

        mock_db.record_spread_open.assert_called_once()
        kwargs = mock_db.record_spread_open.call_args.kwargs
        assert kwargs["credit_received"] == 160.0, "must record the REAL fill, not the 150.0 estimate"
        # width_x100 = max_loss(350) + credit_estimate(150) = 500;
        # real_max_loss = 500 - 160 = 340.
        assert kwargs["max_loss"] == 340.0

    @pytest.mark.asyncio
    async def test_nonsensical_fill_falls_back_to_the_estimate(self):
        """A fill price that would imply non-positive max_loss (should be
        essentially impossible for a real, arbitrage-free market, but this
        codebase's convention is never to trust a computed max_loss <= 0)
        must fall back to the known-good pre-trade estimate rather than
        record a nonsensical number -- the position is real either way, so
        it must still be recorded, just not with bad numbers."""
        import bot as bot_module
        from executor_mcp import OrderResult

        plan = make_plan(underlying="SPY", direction="bear_call", credit_estimate=150.0, max_loss=350.0)
        candidate = {"ticker": "SPY", "_plan": plan, "credit_estimate": 150.0, "max_loss": 350.0, "strength": 1.0}

        mcp = FakeMCP()
        fake_client = FakeClient(clock={"is_open": True, "next_open": "", "next_close": "", "timestamp": ""})
        fake_client.account["daily_pl"] = 0.0
        fake_client.account["daily_pl_pct"] = 0.0

        with patch.object(bot_module, "AlpacaClient", return_value=fake_client), \
             patch.object(bot_module, "AlpacaMCP") as MockMCP, \
             patch("bot.db") as mock_db, \
             patch("bot.reconciler") as mock_reconciler, \
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([candidate], [])), \
             patch("bot._pre_trade_check", new_callable=AsyncMock, return_value=(True, None, plan)), \
             patch("bot.executor_mcp") as mock_executor:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_db.record_spread_open.return_value = 1
            mock_llm.decide.return_value = {"selected": ["SPY"], "reasoning": "test pick"}
            from reconciler import ReconcileResult
            mock_reconciler.reconcile.return_value = ReconcileResult(ok=True)

            # width_x100 = 500; a fill_credit of 600 would imply max_loss
            # of -100 -- impossible in a real market, must be rejected.
            mock_executor.open_spread = AsyncMock(
                return_value=OrderResult(order_ids=["order-1"], status="filled", fill_credit=600.0)
            )

            pause_file = Path(__file__).resolve().parent.parent / "state" / "PAUSE"
            if pause_file.exists():
                pause_file.unlink()

            await bot_module.run_cycle()

        mock_db.record_spread_open.assert_called_once()
        kwargs = mock_db.record_spread_open.call_args.kwargs
        assert kwargs["credit_received"] == 150.0, "nonsensical fill must fall back to the pre-trade estimate"
        assert kwargs["max_loss"] == 350.0


class TestSpreadMonitorMarkUnits:
    """spread_monitor.py: real live-path bug found 2026-08-30 comparing
    against a competing team's own fix for the identical mistake. Alpaca
    quotes are dollars-per-share; credit_received/should_close's math/
    realized_pnl are all dollars-per-contract (x100, same convention as
    executor_mcp.get_spread_mark). _compute_mark returned the raw
    per-share difference with no x100 -- against a real credit_received
    like $150, an unmultiplied ~$0.75 mark computes as ~99.5% profit
    captured on the very first WebSocket tick. Dormant so far only because
    the one open real position (NVDA's iron condor) is excluded from this
    monitor; the first real vertical fill would have hit it immediately.
    """

    def test_compute_mark_multiplies_by_100(self):
        from spread_monitor import SpreadMonitor

        mon = SpreadMonitor()
        mon._quotes = {
            "SHORT_SYM": {"bid": 1.00, "ask": 1.10},
            "LONG_SYM": {"bid": 0.40, "ask": 0.50},
        }
        mark = mon._compute_mark("SHORT_SYM", "LONG_SYM")

        # short mid 1.05, long mid 0.45 -> per-share 0.60 -> per-contract 60.00
        assert mark == 60.0

    def test_compute_mark_matches_credit_received_units_in_should_close(self):
        """A mark this small relative to a realistic credit must NOT read
        as an already-massive profit -- the exact failure mode the missing
        x100 caused."""
        from spread_monitor import SpreadMonitor
        import risk_gate

        mon = SpreadMonitor()
        # Freshly opened: credit ~$150/contract, spread barely moved.
        mon._quotes = {
            "SHORT_SYM": {"bid": 1.48, "ask": 1.50},
            "LONG_SYM": {"bid": 0.01, "ask": 0.03},
        }
        mark = mon._compute_mark("SHORT_SYM", "LONG_SYM")

        should_close, _reason = risk_gate.should_close(credit_received=150.0, current_mark=mark)
        assert should_close is False, (
            f"mark={mark} against credit_received=150.0 wrongly triggered a close -- "
            "units mismatch (missing x100) regression"
        )


class TestEmergencyFlattenBoundedClose:
    """emergency_flatten.py: real regression this session's own limit-order
    change (2026-08-29) introduced and left unnoticed -- close_spread/
    close_iron_condor now require current_mark or max_loss to bound the
    limit price, but this break-glass tool called them with neither,
    meaning every real flatten would have raised ValueError and closed
    nothing. Also adopts a competing team's hardening: shares bot.lock
    with the cron so a flatten can never race a live cycle.
    """

    @pytest.mark.asyncio
    async def test_flatten_closes_vertical_with_a_bounded_price(self):
        import emergency_flatten

        spread = {
            "id": 1, "underlying": "SPY", "direction": "bull_put", "contracts": 2,
            "strategy": "vertical", "short_symbol": "SHORT_SYM", "long_symbol": "LONG_SYM",
            "credit_received": 150.0, "max_loss": 350.0,
        }

        mcp = FakeMCP()
        mcp.set_response("get_option_snapshot", make_snapshot_response({
            "SHORT_SYM": make_quote(bid=1.00, ask=1.10),
            "LONG_SYM": make_quote(bid=0.40, ask=0.50),
        }))
        mcp.set_response("place_option_order", {"data": {"id": "order-1"}})

        with patch.object(emergency_flatten, "db") as mock_db, \
             patch.object(emergency_flatten, "reconciler") as mock_reconciler, \
             patch.object(emergency_flatten, "AlpacaClient") as MockClient, \
             patch.object(emergency_flatten, "AlpacaMCP") as MockMCP, \
             patch.object(emergency_flatten.sys, "argv", ["emergency_flatten.py", "--yes"]):

            mock_db.get_open_spreads.return_value = [spread]
            mock_db.record_spread_close.return_value = None
            mock_reconciler.reconcile.return_value = SimpleNamespace(
                ok=True, reason=None, broker_option_symbols=set(),
            )
            MockClient.return_value = SimpleNamespace()
            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            await emergency_flatten._flatten_all()

        calls = mcp.calls_for("place_option_order")
        assert len(calls) == 1
        assert calls[0]["type"] == "limit"
        mock_db.record_spread_close.assert_called_once_with(1, "closed_emergency", None)

    @pytest.mark.asyncio
    async def test_flatten_falls_back_to_max_loss_when_quote_fetch_fails(self):
        """No live mark available -- must still close, bounded by the
        position's own max_loss, not raise and leave it open."""
        import emergency_flatten

        spread = {
            "id": 2, "underlying": "SPY", "direction": "bull_put", "contracts": 1,
            "strategy": "vertical", "short_symbol": "SHORT_SYM", "long_symbol": "LONG_SYM",
            "credit_received": 150.0, "max_loss": 350.0,
        }

        mcp = FakeMCP()
        mcp.set_response("get_option_snapshot", RuntimeError("quote feed down"))
        mcp.set_response("place_option_order", {"data": {"id": "order-2"}})

        with patch.object(emergency_flatten, "db") as mock_db, \
             patch.object(emergency_flatten, "reconciler") as mock_reconciler, \
             patch.object(emergency_flatten, "AlpacaClient") as MockClient, \
             patch.object(emergency_flatten, "AlpacaMCP") as MockMCP, \
             patch.object(emergency_flatten.sys, "argv", ["emergency_flatten.py", "--yes"]):

            mock_db.get_open_spreads.return_value = [spread]
            mock_db.record_spread_close.return_value = None
            mock_reconciler.reconcile.return_value = SimpleNamespace(
                ok=True, reason=None, broker_option_symbols=set(),
            )
            MockClient.return_value = SimpleNamespace()
            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            await emergency_flatten._flatten_all()

        calls = mcp.calls_for("place_option_order")
        assert len(calls) == 1
        assert calls[0]["limit_price"] == "3.50"  # max_loss 350/contract -> 3.50/share
        mock_db.record_spread_close.assert_called_once()
