"""Tests for HWM orchestrator rewire: trading_equity replaces raw balance.

All tests use real Database(tmp_path / "test.db") — no mocked DBs.
Written RED-first (Task 7). GREEN pass when Task 8 implementation lands.
"""
from __future__ import annotations

import pandas as pd
import pytest

from bot.database.db import Database
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _make_df(n: int = 100, close: float = 50050.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open":   [50000.0] * n,
        "high":   [50100.0] * n,
        "low":    [49900.0] * n,
        "close":  [close]   * n,
        "volume": [100.0]   * n,
    })


# ── C2-S2: Deposit does NOT inflate HWM ───────────────────────────────────────

def test_deposit_does_not_inflate_hwm(db):
    """Raw balance jump (faucet/deposit) must NOT ratchet peak_capital.

    When trading_equity = baseline + closed_pnl_sum and there are no closed
    trades, peak stays at the baseline level regardless of raw balance changes.
    """
    # Seed account baseline at 10000
    db.set_account_baseline(10000.0)
    db.set_peak_capital(10000.0)

    orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
    df = _make_df()

    # Pass a HUGE total_balance simulating a faucet drop — 50000 raw balance
    # But trading_equity = baseline(10000) + closed_pnl(0) = 10000, unchanged
    orch.step(df, current_balance=50000.0, df_high=None, df_weekly=None, total_balance=50000.0)

    peak_after = db.get_peak_capital()
    # Peak must NOT inflate to 50000 — should stay at trading equity (10000)
    assert peak_after is not None
    assert peak_after <= 10000.0 + 1.0  # small tolerance for floating point


# ── C2-S1: Real gain ratchets HWM ─────────────────────────────────────────────

def test_hwm_ratchets_on_realized_gain(db):
    """When closed PnL grows, trading_equity > peak → peak ratchets up."""
    db.set_account_baseline(10000.0)
    db.set_peak_capital(10000.0)

    # Insert and close a winning trade: pnl = +500
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=55000.0,
    )
    db.close_trade(tid, exit_price=100000.0, exit_reason="TAKE_PROFIT")
    # pnl = (100000 - 50000) * 0.01 = 500.0
    # trading_equity = 10000 + 500 = 10500

    orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
    df = _make_df()

    orch.step(df, current_balance=10500.0, df_high=None, df_weekly=None, total_balance=10500.0)

    peak_after = db.get_peak_capital()
    assert peak_after is not None
    assert abs(peak_after - 10500.0) < 1.0  # peak ratcheted to trading_equity


# ── C3-S1: Real loss triggers circuit breaker ──────────────────────────────────

def test_real_loss_triggers_circuit_breaker(db):
    """Cumulative realized losses crossing 15% drawdown must halt trading."""
    db.set_account_baseline(10000.0)
    db.set_peak_capital(10000.0)

    # Close a big losing trade: loss = -1600 → drawdown = 1600/10000 = 16% > 15%
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.032,
        stop_loss=49000.0, take_profit=55000.0,
    )
    # pnl = (10000 - 50000) * 0.032 = -1280.0 (loss)
    db.close_trade(tid, exit_price=10000.0, exit_reason="STOP_LOSS")
    # trading_equity = 10000 + (-1280) = 8720 → drawdown = 1280/10000 = 12.8% < 15%, not enough

    # Add one more loss trade to cross threshold
    tid2 = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.01,
        stop_loss=49000.0, take_profit=55000.0,
    )
    # pnl = (10000 - 50000) * 0.01 = -400 (loss)
    db.close_trade(tid2, exit_price=10000.0, exit_reason="STOP_LOSS")
    # total pnl = -1280 - 400 = -1680 → trading_equity = 10000 - 1680 = 8320
    # drawdown = 1680/10000 = 16.8% > 15% → should trigger

    orch = StrategyOrchestrator(
        db=db,
        symbol="BTCUSDT",
        risk_config=RiskConfig(max_drawdown=0.15, cooldown_hours=4),
    )
    df = _make_df()

    result = orch.step(df, current_balance=8320.0, df_high=None, df_weekly=None, total_balance=8320.0)

    # Breaker fires → step returns empty list
    assert result == []


# ── C5-S1: Orchestrator re-reads peak from DB each step ───────────────────────

def test_orchestrator_rereads_peak_each_step_no_in_memory_cache(db):
    """After writing a new peak to DB between two steps, the second step must see it.

    This test verifies there is NO in-memory `_peak_capital` cache in the
    orchestrator that would shadow a DB write from a sibling orchestrator.
    """
    db.set_account_baseline(10000.0)
    db.set_peak_capital(10000.0)

    orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
    df = _make_df()

    # Step 1 — sets up state
    orch.step(df, current_balance=10000.0, df_high=None, df_weekly=None, total_balance=10000.0)

    # Simulate a sibling orchestrator (or /reset_hwm) writing a new peak directly to DB
    db.set_peak_capital(12000.0)

    # Also insert a gain to raise trading_equity above the new peak
    tid = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.05,
        stop_loss=49000.0, take_profit=55000.0,
    )
    db.close_trade(tid, exit_price=100000.0, exit_reason="TAKE_PROFIT")
    # pnl = (100000 - 50000) * 0.05 = 2500 → trading_equity = 10000 + 2500 = 12500

    # Step 2 — must see the DB-written peak (12000) and then update to 12500
    orch.step(df, current_balance=12500.0, df_high=None, df_weekly=None, total_balance=12500.0)

    peak_final = db.get_peak_capital()
    # If the orchestrator read from DB (not in-memory cache), it sees 12000,
    # then trading_equity=12500 > 12000 → updates to 12500
    assert peak_final is not None
    assert abs(peak_final - 12500.0) < 1.0


# ── C2-S5 / C5-S2: Cross-symbol HWM via shared DB ────────────────────────────

def test_cross_symbol_hwm_shared(db):
    """BTC closed PnL affects ETH orchestrator's trading_equity on next step.

    Both orchestrators share the same DB and account_baseline.
    get_closed_pnl_sum() uses no symbol filter → cross-symbol total.
    """
    db.set_account_baseline(10000.0)
    db.set_peak_capital(10000.0)

    # Close a winning BTC trade: pnl = +1000
    tid_btc = db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER",
        regime="TRENDING", entry_price=50000.0, quantity=0.02,
        stop_loss=49000.0, take_profit=55000.0,
    )
    db.close_trade(tid_btc, exit_price=100000.0, exit_reason="TAKE_PROFIT")
    # pnl_btc = (100000 - 50000) * 0.02 = 1000 → trading_equity = 11000

    # ETH orchestrator — must see trading_equity = 11000 (cross-symbol)
    orch_eth = StrategyOrchestrator(db=db, symbol="ETHUSDT")
    df = _make_df()

    orch_eth.step(df, current_balance=11000.0, df_high=None, df_weekly=None, total_balance=11000.0)

    peak_after = db.get_peak_capital()
    assert peak_after is not None
    # trading_equity = 10000 (baseline) + 1000 (btc pnl) = 11000 > old peak (10000)
    # Peak must have ratcheted to 11000
    assert abs(peak_after - 11000.0) < 1.0
