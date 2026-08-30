"""Report-only (--dry-run) mode for overnight_evolution.py (2026-08-30,
built for an unattended nightly cron during the judged week). The one
property every test here protects: dry_run must NEVER call
write_evolved_params or _auto_revert_to's real state changes, no matter
what the analysis decides -- only evolution_history gets a row, under a
'would_*' decision label that can never collide with a real one.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import overnight_evolution as oe
from evolution_config import StrategyParams


def _fake_result(pnl: float, drawdown: float = 0.0, passed: int = 5, opened: int = 3) -> dict:
    return {
        "candidates_passed": passed, "would_have_opened": opened,
        "simulated_pnl": pnl, "max_drawdown": drawdown, "trades": [],
    }


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Points overnight_evolution's BASE_DIR at a throwaway directory so
    tests can never touch the real state/ dir, and mocks db entirely so
    nothing here can reach the real Supabase."""
    monkeypatch.setattr(oe, "BASE_DIR", tmp_path)
    mock_db = MagicMock()
    monkeypatch.setattr(oe, "db", mock_db)
    return tmp_path, mock_db


def test_dry_run_never_writes_evolved_params_even_when_it_would_promote(isolated_state):
    tmp_path, mock_db = isolated_state
    mock_db.get_evolution_history.return_value = []

    with patch.object(oe, "collect_today_data", return_value={"candidates": [{"ticker": "AAPL", "direction": "long"}], "journal": [{}]}), \
         patch.object(oe, "_incumbent_params", return_value=StrategyParams()), \
         patch.object(oe, "generate_variants", return_value=[StrategyParams()]), \
         patch.object(oe, "replay_variants", return_value=(_fake_result(100.0), [_fake_result(150.0)])), \
         patch.object(oe, "write_evolved_params") as mock_write_params:
        oe.run_evolution(dry_run=True)

    mock_write_params.assert_not_called()
    assert not (tmp_path / "state" / "evolved_params.json").exists()

    history_call = mock_db.record_evolution_history.call_args
    assert history_call.kwargs["decision"] == "would_promote"
    assert "[DRY RUN]" in history_call.kwargs["reason"]

    # Report goes to the dry-run-specific file, never the real one.
    assert (tmp_path / "state" / "evolution_report_dryrun.md").exists()
    assert not (tmp_path / "state" / "evolution_report.md").exists()


def test_dry_run_decision_label_when_nothing_would_promote(isolated_state):
    tmp_path, mock_db = isolated_state
    mock_db.get_evolution_history.return_value = []

    with patch.object(oe, "collect_today_data", return_value={"candidates": [{"ticker": "AAPL", "direction": "long"}], "journal": [{}]}), \
         patch.object(oe, "_incumbent_params", return_value=StrategyParams()), \
         patch.object(oe, "generate_variants", return_value=[StrategyParams()]), \
         patch.object(oe, "replay_variants", return_value=(_fake_result(100.0), [_fake_result(50.0)])), \
         patch.object(oe, "write_evolved_params") as mock_write_params:
        oe.run_evolution(dry_run=True)

    mock_write_params.assert_not_called()
    assert mock_db.record_evolution_history.call_args.kwargs["decision"] == "would_hold"


def test_real_run_unaffected_still_writes_evolved_params_on_promotion(isolated_state):
    """Regression guard: adding dry_run must not change default (real)
    behavior — this is the exact scenario that already ran for real on
    2026-08-28."""
    tmp_path, mock_db = isolated_state
    mock_db.get_evolution_history.return_value = []

    with patch.object(oe, "collect_today_data", return_value={"candidates": [{"ticker": "AAPL", "direction": "long"}], "journal": [{}]}), \
         patch.object(oe, "_incumbent_params", return_value=StrategyParams()), \
         patch.object(oe, "generate_variants", return_value=[StrategyParams()]), \
         patch.object(oe, "replay_variants", return_value=(_fake_result(100.0), [_fake_result(150.0)])):
        oe.run_evolution(dry_run=False)

    assert (tmp_path / "state" / "evolved_params.json").exists()
    assert mock_db.record_evolution_history.call_args.kwargs["decision"] == "promoted"
    assert (tmp_path / "state" / "evolution_report.md").exists()
    assert not (tmp_path / "state" / "evolution_report_dryrun.md").exists()


def test_auto_revert_dry_run_never_calls_the_real_revert(isolated_state):
    tmp_path, mock_db = isolated_state
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "evolved_params.json").write_text('{"generation": 2}')

    mock_db.get_realized_pnl_by_generation.side_effect = lambda gen: (
        {"closed_trades": 5, "total_pnl": -50.0, "avg_pnl": -10.0} if gen == 2
        else {"closed_trades": 5, "total_pnl": 50.0, "avg_pnl": 10.0}
    )

    with patch.object(oe, "_auto_revert_to") as mock_revert:
        oe.check_and_maybe_auto_revert(dry_run=True)

    mock_revert.assert_not_called()
    history_call = mock_db.record_evolution_history.call_args
    assert history_call.kwargs["decision"] == "would_auto_revert"
    # The real state file must be untouched -- still generation 2, not reverted.
    import json
    assert json.loads((state_dir / "evolved_params.json").read_text())["generation"] == 2


def test_auto_revert_real_mode_still_calls_the_real_revert(isolated_state):
    """Regression guard for the non-dry-run path, same trigger condition."""
    tmp_path, mock_db = isolated_state
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "evolved_params.json").write_text('{"generation": 2}')

    mock_db.get_realized_pnl_by_generation.side_effect = lambda gen: (
        {"closed_trades": 5, "total_pnl": -50.0, "avg_pnl": -10.0} if gen == 2
        else {"closed_trades": 5, "total_pnl": 50.0, "avg_pnl": 10.0}
    )

    with patch.object(oe, "_auto_revert_to") as mock_revert:
        oe.check_and_maybe_auto_revert(dry_run=False)

    mock_revert.assert_called_once()
