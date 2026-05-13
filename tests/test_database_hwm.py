"""Unit tests for HWM-related Database methods.

All tests use a real Database(tmp_path / "test.db") — no mocks.
Written RED-first (Task 1). GREEN pass when Task 2-4 implementations land.
"""
from __future__ import annotations

import pytest

from bot.database.db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


# ── Capability 1: account_baseline ────────────────────────────────────────────

def test_get_account_baseline_returns_none_when_unset(db):
    """Before any seed, get_account_baseline() must return None."""
    assert db.get_account_baseline() is None


def test_set_get_account_baseline_roundtrip(db):
    """set_account_baseline stores and get_account_baseline retrieves the float."""
    db.set_account_baseline(18625.0)
    result = db.get_account_baseline()
    assert result is not None
    assert abs(result - 18625.0) < 0.0001


# ── Capability 3 (DB side): get_closed_pnl_sum ────────────────────────────────

def test_get_closed_pnl_sum_excludes_open_trades(db):
    """Open trades (exit_price IS NULL) must NOT appear in the PnL sum."""
    # Insert one open trade (no close call)
    db.insert_trade(
        symbol="BTCUSDT",
        side="BUY",
        strategy="EMA_CROSSOVER",
        regime="TRENDING",
        entry_price=50000.0,
        quantity=0.01,
        stop_loss=49000.0,
        take_profit=53000.0,
    )
    assert db.get_closed_pnl_sum() == 0.0


def test_get_closed_pnl_sum_aggregates_across_symbols(db):
    """Cross-symbol sum when symbol=None."""
    trade_id_btc = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=53000.0,
    )
    db.close_trade(trade_id_btc, exit_price=51000.0, exit_reason="TAKE_PROFIT")

    trade_id_eth = db.insert_trade(
        symbol="ETHUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=3000.0, quantity=0.1,
        stop_loss=2900.0, take_profit=3300.0,
    )
    db.close_trade(trade_id_eth, exit_price=3200.0, exit_reason="TAKE_PROFIT")

    total = db.get_closed_pnl_sum()
    # BTC pnl = (51000 - 50000) * 0.01 = 10.0
    # ETH pnl = (3200 - 3000) * 0.1   = 20.0
    assert abs(total - 30.0) < 0.001


def test_get_closed_pnl_sum_filters_by_symbol_when_provided(db):
    """symbol argument restricts sum to that symbol only."""
    trade_btc = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=53000.0,
    )
    db.close_trade(trade_btc, exit_price=51000.0, exit_reason="TAKE_PROFIT")

    trade_eth = db.insert_trade(
        symbol="ETHUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=3000.0, quantity=0.1,
        stop_loss=2900.0, take_profit=3300.0,
    )
    db.close_trade(trade_eth, exit_price=3200.0, exit_reason="TAKE_PROFIT")

    btc_only = db.get_closed_pnl_sum(symbol="BTCUSDT")
    assert abs(btc_only - 10.0) < 0.001

    eth_only = db.get_closed_pnl_sum(symbol="ETHUSDT")
    assert abs(eth_only - 20.0) < 0.001


# ── Capability 4: reset_peak_capital ──────────────────────────────────────────

def test_reset_peak_capital_with_value_sets_explicit_peak(db):
    """reset_peak_capital(value=X) sets peak_capital to X exactly."""
    db.set_peak_capital(39277.0)  # simulate polluted old peak
    old, new = db.reset_peak_capital(value=18625.0, clear_breaker=False)
    assert abs(new - 18625.0) < 0.0001
    assert abs(db.get_peak_capital() - 18625.0) < 0.0001


def test_reset_peak_capital_with_none_resets_to_current_trading_equity(db):
    """reset_peak_capital(value=None) sets peak = baseline + closed_pnl_sum."""
    db.set_account_baseline(10000.0)
    # Insert and close a trade with +500 pnl
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=53000.0,
    )
    db.close_trade(tid, exit_price=100000.0, exit_reason="TAKE_PROFIT")
    # pnl = (100000 - 50000) * 0.01 = 500.0

    old, new = db.reset_peak_capital(value=None, clear_breaker=False)
    # trading_equity = 10000 + 500 = 10500
    assert abs(new - 10500.0) < 0.001
    assert abs(db.get_peak_capital() - 10500.0) < 0.001


def test_reset_peak_capital_returns_old_and_new_peaks(db):
    """reset_peak_capital returns (old_peak, new_peak) tuple."""
    db.set_peak_capital(39277.0)
    old, new = db.reset_peak_capital(value=18625.0, clear_breaker=False)
    assert abs(old - 39277.0) < 0.0001
    assert abs(new - 18625.0) < 0.0001


def test_reset_peak_capital_clears_breaker_rows_when_flag_set(db):
    """clear_breaker=True removes all breaker_triggered_at_* keys from bot_config."""
    # Simulate active breaker timestamps for two symbols
    db.set_config("breaker_triggered_at_BTCUSDT", "2026-05-13T10:00:00")
    db.set_config("breaker_triggered_at_ETHUSDT", "2026-05-13T10:00:00")
    # A non-breaker key that must NOT be deleted
    db.set_config("some_other_key", "should_stay")

    db.reset_peak_capital(value=18625.0, clear_breaker=True)

    assert db.get_config("breaker_triggered_at_BTCUSDT") in (None, "")
    assert db.get_config("breaker_triggered_at_ETHUSDT") in (None, "")
    assert db.get_config("some_other_key") == "should_stay"


def test_reset_peak_capital_preserves_baseline(db):
    """reset_peak_capital must NOT touch account_baseline."""
    db.set_account_baseline(20000.0)
    db.reset_peak_capital(value=18625.0, clear_breaker=True)
    assert abs(db.get_account_baseline() - 20000.0) < 0.0001
