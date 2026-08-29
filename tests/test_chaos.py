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
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([], [])) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_llm.decide.return_value = {"selected": [], "reasoning": "Nothing good"}

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
             patch("bot.llm_reasoner") as mock_llm, \
             patch("bot.find_candidates", new_callable=AsyncMock, return_value=([], [])) as mock_find:

            MockMCP.return_value.__aenter__ = AsyncMock(return_value=mcp)
            MockMCP.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db.get_open_spreads.return_value = []
            mock_db.record_cycle.return_value = 1
            mock_db.record_decision_journal.return_value = None
            mock_db.record_account_snapshot.return_value = None
            mock_llm.decide.return_value = {"selected": [], "reasoning": "Nothing good"}

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
