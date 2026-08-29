#!/usr/bin/env python3
"""Offline test for fact-provenance citation validation in llm_reasoner.decide().

Stubs the HTTP call (requests.post) to return canned LLM responses, then
verifies that:
- Valid fact IDs in the reasoning are extracted and returned
- Unknown fact IDs trigger a warning but do NOT block the decision
- Zero citations trigger a warning but do NOT block the decision
- `selected` is always preserved regardless of citation quality
"""
from __future__ import annotations

import json
import logging

import llm_reasoner


# --- canned candidates (mimics what find_candidates would build) ---

VERTICAL_CANDIDATE = {
    "ticker": "AAPL",
    "strategy": "vertical",
    "direction": "bull_put",
    "strength": 0.82,
    "signal_reasoning": "AAPL in strong uptrend, ADX above threshold",
    "credit_estimate": 66.52,
    "max_loss": 433.48,
    "expiration": "2026-09-05",
    "fact_ids": {
        "AAPL_SIGNAL_STRENGTH": 0.82,
        "AAPL_CREDIT_EST": 66.52,
        "AAPL_MAX_LOSS": 433.48,
        "AAPL_DTE": 7,
    },
}

IRON_CONDOR_CANDIDATE = {
    "ticker": "MSFT",
    "strategy": "iron_condor",
    "direction": None,
    "strength": None,
    "signal_reasoning": None,
    "credit_estimate": 42.00,
    "max_loss": 458.00,
    "expiration": "2026-09-05",
    "fact_ids": {
        "MSFT_CREDIT_EST": 42.00,
        "MSFT_MAX_LOSS": 458.00,
        "MSFT_DTE": 7,
    },
}

CANDIDATES = [VERTICAL_CANDIDATE, IRON_CONDOR_CANDIDATE]


# --- stub for requests.post ---

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _make_canned_response(reasoning: str, selected: list[str]) -> _FakeResponse:
    return _FakeResponse({
        "choices": [{"message": {"content": json.dumps({"selected": selected, "reasoning": reasoning})}}],
    })


# --- helpers ---

def _capture_warnings(cap_handler: list[str]):
    """Return a logging handler that appends formatted messages to cap_handler."""
    class _ListHandler(logging.Handler):
        def emit(self, record):
            cap_handler.append(self.format(record))
    h = _ListHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


# --- test cases ---

def test_valid_citations():
    """A response citing real fact IDs should return them in cited_fact_ids."""
    warnings: list[str] = []
    handler = _capture_warnings(warnings)
    llm_reasoner.logger.addHandler(handler)
    try:
        reasoning = "AAPL credit $66.52 [AAPL_CREDIT_EST] vs max loss $433.48 [AAPL_MAX_LOSS]"
        original_post = __import__("requests").post
        __import__("requests").post = lambda *a, **kw: _make_canned_response(reasoning, ["AAPL"])
        try:
            result = llm_reasoner.decide(CANDIDATES, 2)
        finally:
            __import__("requests").post = original_post

        assert result["selected"] == ["AAPL"], f"selected should be preserved, got {result['selected']}"
        assert "AAPL_CREDIT_EST" in result["cited_fact_ids"], f"expected AAPL_CREDIT_EST in cited_fact_ids, got {result['cited_fact_ids']}"
        assert "AAPL_MAX_LOSS" in result["cited_fact_ids"], f"expected AAPL_MAX_LOSS in cited_fact_ids, got {result['cited_fact_ids']}"
        assert result["uncited_ratio"] < 1.0, f"uncited_ratio should be < 1.0 when some facts are cited"
        assert not warnings, f"no warnings expected for valid citations, got: {warnings}"
        print("  PASS: valid citations extracted correctly")
    finally:
        llm_reasoner.logger.removeHandler(handler)


def test_unknown_citation():
    """A response citing an unknown fact ID should warn but not block."""
    warnings: list[str] = []
    handler = _capture_warnings(warnings)
    llm_reasoner.logger.addHandler(handler)
    try:
        reasoning = "AAPL credit $66.52 [AAPL_CREDIT_EST] and volatility [FAKE_FACT_999]"
        original_post = __import__("requests").post
        __import__("requests").post = lambda *a, **kw: _make_canned_response(reasoning, ["AAPL"])
        try:
            result = llm_reasoner.decide(CANDIDATES, 2)
        finally:
            __import__("requests").post = original_post

        assert result["selected"] == ["AAPL"], f"selected must not change, got {result['selected']}"
        assert "FAKE_FACT_999" not in result["cited_fact_ids"], "unknown IDs must not appear in cited_fact_ids"
        assert any("FAKE_FACT_999" in w for w in warnings), f"expected warning about unknown ID, got: {warnings}"
        print("  PASS: unknown citation warned but did not block")
    finally:
        llm_reasoner.logger.removeHandler(handler)


def test_zero_citations():
    """A response with no citations should warn but not block."""
    warnings: list[str] = []
    handler = _capture_warnings(warnings)
    llm_reasoner.logger.addHandler(handler)
    try:
        reasoning = "Both candidates look fine, select AAPL."
        original_post = __import__("requests").post
        __import__("requests").post = lambda *a, **kw: _make_canned_response(reasoning, ["AAPL"])
        try:
            result = llm_reasoner.decide(CANDIDATES, 2)
        finally:
            __import__("requests").post = original_post

        assert result["selected"] == ["AAPL"], f"selected must not change, got {result['selected']}"
        assert result["cited_fact_ids"] == [], f"cited_fact_ids should be empty, got {result['cited_fact_ids']}"
        assert result["uncited_ratio"] == 1.0, f"uncited_ratio should be 1.0 with zero citations"
        assert any("ZERO" in w for w in warnings), f"expected zero-citations warning, got: {warnings}"
        print("  PASS: zero citations warned but did not block")
    finally:
        llm_reasoner.logger.removeHandler(handler)


def test_empty_candidates():
    """Empty candidate list should return empty result without hitting the API."""
    result = llm_reasoner.decide([], 2)
    assert result["selected"] == [], f"expected empty selected, got {result['selected']}"
    assert result["cited_fact_ids"] == [], f"expected empty cited_fact_ids, got {result['cited_fact_ids']}"
    print("  PASS: empty candidates handled without API call")


def main():
    print("test_fact_provenance.py")
    print("=" * 40)
    test_empty_candidates()
    test_valid_citations()
    test_unknown_citation()
    test_zero_citations()
    print("=" * 40)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
