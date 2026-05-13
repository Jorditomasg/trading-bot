"""Tests for _init_account_baseline in main.py.

All tests use a real Database(tmp_path / "test.db") — no mocks except
for BinanceClient.get_balance to avoid network calls.

Written RED-first (Task 5). GREEN pass when Task 6 implementation lands.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from bot.database.db import Database
from main import _init_account_baseline  # noqa: E402  — top-level import


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def mock_client():
    """BinanceClient mock — default balance 18625.0."""
    client = MagicMock()
    client.get_balance.return_value = 18625.0
    return client


# ── C1-S1 / C1-S2: first run back-computation ─────────────────────────────────

def test_seed_account_baseline_first_run_uses_back_computed_value(db, mock_client):
    """baseline = current_balance - pnl_sum on first run."""
    # Insert a closed trade with known PnL
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=53000.0,
    )
    # pnl = (52000 - 50000) * 0.01 = 20.0
    db.close_trade(tid, exit_price=52000.0, exit_reason="TAKE_PROFIT")

    mock_client.get_balance.return_value = 18645.0  # balance includes PnL

    _init_account_baseline(db, mock_client)

    baseline = db.get_account_baseline()
    assert baseline is not None
    # baseline = 18645.0 - 20.0 = 18625.0
    assert abs(baseline - 18625.0) < 0.001


def test_seed_account_baseline_idempotent_on_second_run(db, mock_client):
    """Calling _init_account_baseline twice must NOT overwrite the first value."""
    mock_client.get_balance.return_value = 10000.0

    _init_account_baseline(db, mock_client)
    first_baseline = db.get_account_baseline()
    assert first_baseline is not None

    # Change the mock balance — if idempotent, the second call must be a no-op
    mock_client.get_balance.return_value = 99999.0
    _init_account_baseline(db, mock_client)

    assert db.get_account_baseline() == first_baseline
    # get_balance called only once (second call exits early)
    assert mock_client.get_balance.call_count == 1


# ── C1-S4: negative back-computed baseline ────────────────────────────────────

def test_seed_account_baseline_falls_back_to_current_balance_when_negative(
    db, mock_client, caplog
):
    """When pnl_sum > current_balance, back-computed baseline is negative.
    Should fall back to current_balance and log a WARNING.
    """
    # Close a trade with large loss so pnl_sum is very negative relative to balance
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=1.0,
        stop_loss=49000.0, take_profit=53000.0,
    )
    # Huge loss: pnl = (10000 - 50000) * 1.0 = -40000
    db.close_trade(tid, exit_price=10000.0, exit_reason="STOP_LOSS")

    # current_balance = 5000, pnl_sum = -40000 → back-computed = 5000 - (-40000) = +45000
    # That is POSITIVE so it won't trigger the warning.
    # To trigger a negative back-computed we need pnl_sum > balance:
    # pnl_sum is POSITIVE and > current_balance — means lots of wins but balance somehow small?
    # Per spec: if pnl_sum > current_balance → baseline would be negative → fallback.
    # Let's use positive pnl that exceeds balance to trigger the edge case.
    tid2 = db.insert_trade(
        symbol="ETHUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=100.0, quantity=1000.0,
        stop_loss=90.0, take_profit=200.0,
    )
    # pnl = (200 - 100) * 1000 = 100000 (positive, large)
    db.close_trade(tid2, exit_price=200.0, exit_reason="TAKE_PROFIT")

    # Total pnl_sum = -40000 + 100000 = +60000
    # current_balance = 5000; baseline = 5000 - 60000 = -55000 → NEGATIVE → fallback
    mock_client.get_balance.return_value = 5000.0

    with caplog.at_level(logging.WARNING):
        _init_account_baseline(db, mock_client)

    baseline = db.get_account_baseline()
    assert baseline is not None
    # Must fall back to current_balance
    assert abs(baseline - 5000.0) < 0.001
    # Must have logged a warning
    assert any("negative" in r.message.lower() or "WARNING" in r.levelname for r in caplog.records
               if r.levelno >= logging.WARNING)


# ── C6-S5: balance fetch failure ──────────────────────────────────────────────

def test_seed_account_baseline_skips_on_balance_fetch_failure(db, caplog):
    """If get_balance raises, baseline stays None and ERROR is logged."""
    client = MagicMock()
    client.get_balance.side_effect = RuntimeError("network error")

    with caplog.at_level(logging.ERROR):
        _init_account_baseline(db, client)

    assert db.get_account_baseline() is None
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
