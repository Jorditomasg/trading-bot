"""Tests for bot/optimizer/auto_optimizer.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from bot.optimizer.auto_optimizer import LAST_RUN_KEY, OPTIMIZER_INTERVAL_DAYS, run_and_apply, should_run


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_db(runtime_cfg: dict | None = None) -> MagicMock:
    db = MagicMock()
    db.get_runtime_config.return_value = runtime_cfg or {}
    db.get_best_pending_optimizer_run.return_value = None
    return db


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── should_run ────────────────────────────────────────────────────────────────

def test_should_run_no_last_run():
    """Returns True when no previous run timestamp is stored."""
    db = _mock_db({})
    assert should_run(db) is True


def test_should_run_recent():
    """Returns False when last run was less than interval_days ago."""
    recent = datetime.now(tz=timezone.utc) - timedelta(days=3)
    db = _mock_db({LAST_RUN_KEY: _iso(recent)})
    assert should_run(db, interval_days=7) is False


def test_should_run_overdue():
    """Returns True when last run was more than interval_days ago."""
    old = datetime.now(tz=timezone.utc) - timedelta(days=8)
    db = _mock_db({LAST_RUN_KEY: _iso(old)})
    assert should_run(db, interval_days=7) is True


def test_should_run_exactly_on_boundary():
    """Returns True when elapsed time equals exactly interval_days."""
    exact = datetime.now(tz=timezone.utc) - timedelta(days=OPTIMIZER_INTERVAL_DAYS)
    db = _mock_db({LAST_RUN_KEY: _iso(exact)})
    assert should_run(db) is True


def test_should_run_invalid_timestamp():
    """Returns True when stored timestamp is not parseable."""
    db = _mock_db({LAST_RUN_KEY: "not-a-date"})
    assert should_run(db) is True


def test_should_run_naive_timestamp():
    """Treats naive (no tz) timestamps as UTC."""
    recent_naive = (datetime.now(tz=timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    db = _mock_db({LAST_RUN_KEY: recent_naive.isoformat()})
    assert should_run(db, interval_days=7) is False


# ── run_and_apply ─────────────────────────────────────────────────────────────

def test_run_and_apply_no_viable_returns_none():
    """Returns None when grid search finds no viable configs."""
    db = _mock_db({"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"})
    non_viable = [{"viable": False, "stop_mult": 1.0, "tp_mult": 2.5,
                   "profit_factor": 0.8, "sharpe_ratio": 0.1,
                   "win_rate": 30.0, "max_drawdown": 25.0, "total_trades": 5}]

    with patch("bot.optimizer.auto_optimizer.run_grid_search", return_value=non_viable):
        result = run_and_apply(db, "BTCUSDT", "4h")

    assert result is None
    db.set_runtime_config.assert_called()  # timestamp still written


def test_run_and_apply_same_config_returns_none():
    """Returns None when best viable config equals current config."""
    db = _mock_db({"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"})
    viable = [{"viable": True, "stop_mult": 1.5, "tp_mult": 3.5,
               "profit_factor": 1.2, "sharpe_ratio": 0.8,
               "win_rate": 55.0, "max_drawdown": 10.0, "total_trades": 20}]

    with patch("bot.optimizer.auto_optimizer.run_grid_search", return_value=viable):
        result = run_and_apply(db, "BTCUSDT", "4h")

    assert result is None


def test_run_and_apply_new_config_calls_on_applied():
    """Calls on_applied callback when a new config is applied."""
    db = _mock_db({"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"})
    viable = [{"viable": True, "stop_mult": 1.75, "tp_mult": 4.0,
               "profit_factor": 1.4, "sharpe_ratio": 1.0,
               "win_rate": 60.0, "max_drawdown": 8.0, "total_trades": 25}]

    callback = MagicMock()
    with patch("bot.optimizer.auto_optimizer.run_grid_search", return_value=viable):
        result = run_and_apply(db, "BTCUSDT", "4h", on_applied=callback)

    assert result is not None
    old_params, new_params = result
    assert old_params["ema_stop_mult"] == 1.5
    assert old_params["ema_tp_mult"] == 3.5
    assert new_params["ema_stop_mult"] == 1.75
    assert new_params["ema_tp_mult"] == 4.0
    callback.assert_called_once_with(old_params, new_params)


def test_run_and_apply_on_applied_not_called_when_no_change():
    """Does NOT call on_applied callback when config is unchanged."""
    db = _mock_db({"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"})
    viable = [{"viable": True, "stop_mult": 1.5, "tp_mult": 3.5,
               "profit_factor": 1.2, "sharpe_ratio": 0.8,
               "win_rate": 55.0, "max_drawdown": 10.0, "total_trades": 20}]

    callback = MagicMock()
    with patch("bot.optimizer.auto_optimizer.run_grid_search", return_value=viable):
        run_and_apply(db, "BTCUSDT", "4h", on_applied=callback)

    callback.assert_not_called()


def test_run_and_apply_concurrent_skip():
    """Second concurrent call is skipped — lock prevents double execution."""
    import threading
    from bot.optimizer import auto_optimizer

    db = _mock_db({})
    results = []

    # Acquire the lock to simulate a running optimizer
    auto_optimizer._lock.acquire()
    try:
        result = run_and_apply(db, "BTCUSDT", "4h")
        results.append(result)
    finally:
        auto_optimizer._lock.release()

    assert results == [None]
    db.get_runtime_config.assert_not_called()
