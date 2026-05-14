"""Tests for both-wicked tiebreaker parity between BacktestEngine and the live bot.

Live behavior (main.py): when the same 1m bar wicks both SL and TP:
  - BUY: close > entry → TAKE_PROFIT, exit_price = bar close
  - BUY: close <= entry → STOP_LOSS, exit_price = bar close
  - SELL: close < entry → TAKE_PROFIT, exit_price = bar close
  - SELL: close >= entry → STOP_LOSS, exit_price = bar close

Backtest legacy: SL always wins (conservative). This has been updated to match live.
"""

import pandas as pd
import pytest

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bar(open_: float, high: float, low: float, close: float) -> pd.Series:
    """Build a single OHLCV bar Series."""
    return pd.Series({
        "open": open_,
        "high": high,
        "low":  low,
        "close": close,
        "volume": 1_000_000.0,
    })


def _trade(side: str, entry: float, sl: float, tp: float) -> dict:
    """Build a minimal open-trade dict."""
    return {
        "side":       side,
        "entry_price": entry,
        "stop_loss":   sl,
        "take_profit": tp,
        "quantity":    1.0,
    }


def _engine() -> BacktestEngine:
    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        timeframe="1h",
        cost_per_side_pct=0.0,
        kelly_enabled=False,
    )
    return BacktestEngine(cfg)


# ── _check_exit unit tests — both-wicked tiebreaker ──────────────────────────

class TestBothWickedTiebreaker:
    """Tests for _check_exit when a bar wicks both SL and TP simultaneously."""

    def test_buy_both_wicked_close_above_entry_is_tp(self):
        """BUY: same bar wicks both SL (low) and TP (high), close > entry → TAKE_PROFIT."""
        engine = _engine()
        # entry=50000, SL=49000, TP=52000
        # bar: low=48000 (wicks SL), high=53000 (wicks TP), close=51000 (> entry)
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=53_000.0, low=48_000.0, close=51_000.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_TAKE_PROFIT
        assert exit_price == pytest.approx(51_000.0)   # bar close

    def test_buy_both_wicked_close_below_entry_is_sl(self):
        """BUY: same bar wicks both SL (low) and TP (high), close <= entry → STOP_LOSS."""
        engine = _engine()
        # entry=50000, SL=49000, TP=52000
        # bar: low=48000 (wicks SL), high=53000 (wicks TP), close=49500 (<= entry)
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=53_000.0, low=48_000.0, close=49_500.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(49_500.0)   # bar close

    def test_buy_both_wicked_close_exactly_at_entry_is_sl(self):
        """BUY: close == entry → STOP_LOSS (boundary: close <= entry)."""
        engine = _engine()
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=53_000.0, low=48_000.0, close=50_000.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(50_000.0)

    def test_sell_both_wicked_close_below_entry_is_tp(self):
        """SELL: same bar wicks both SL (high) and TP (low), close < entry → TAKE_PROFIT."""
        engine = _engine()
        # entry=50000, SL=51000, TP=48000
        # bar: high=52000 (wicks SL), low=47000 (wicks TP), close=49000 (< entry)
        trade = _trade("SELL", entry=50_000.0, sl=51_000.0, tp=48_000.0)
        bar   = _bar(open_=50_000.0, high=52_000.0, low=47_000.0, close=49_000.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_TAKE_PROFIT
        assert exit_price == pytest.approx(49_000.0)   # bar close

    def test_sell_both_wicked_close_above_entry_is_sl(self):
        """SELL: same bar wicks both SL (high) and TP (low), close >= entry → STOP_LOSS."""
        engine = _engine()
        # entry=50000, SL=51000, TP=48000
        # bar: high=52000 (wicks SL), low=47000 (wicks TP), close=50500 (>= entry)
        trade = _trade("SELL", entry=50_000.0, sl=51_000.0, tp=48_000.0)
        bar   = _bar(open_=50_000.0, high=52_000.0, low=47_000.0, close=50_500.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(50_500.0)   # bar close

    def test_sell_both_wicked_close_exactly_at_entry_is_sl(self):
        """SELL: close == entry → STOP_LOSS (boundary: close >= entry)."""
        engine = _engine()
        trade = _trade("SELL", entry=50_000.0, sl=51_000.0, tp=48_000.0)
        bar   = _bar(open_=50_000.0, high=52_000.0, low=47_000.0, close=50_000.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(50_000.0)


# ── Preserved behaviors: single-wick exits still use level price ──────────────

class TestSingleWickExitsPreserved:
    """When only SL or only TP is wicked, exit price = the SL/TP level (unchanged)."""

    def test_buy_only_sl_wicked_exits_at_sl_level(self):
        """BUY: only SL hit → exit_price = SL level (no change from legacy)."""
        engine = _engine()
        # entry=50000, SL=49000, TP=52000
        # bar: low=48500 (wicks SL), high=51500 (no TP wick)
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=51_500.0, low=48_500.0, close=50_200.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(49_000.0)   # SL level

    def test_buy_only_tp_wicked_exits_at_tp_level(self):
        """BUY: only TP hit → exit_price = TP level (no change from legacy)."""
        engine = _engine()
        # entry=50000, SL=49000, TP=52000
        # bar: low=49500 (no SL wick), high=53000 (wicks TP)
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=53_000.0, low=49_500.0, close=51_000.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_TAKE_PROFIT
        assert exit_price == pytest.approx(52_000.0)   # TP level

    def test_sell_only_sl_wicked_exits_at_sl_level(self):
        """SELL: only SL hit → exit_price = SL level."""
        engine = _engine()
        # entry=50000, SL=51000, TP=48000
        # bar: high=51500 (wicks SL), low=48500 (no TP wick)
        trade = _trade("SELL", entry=50_000.0, sl=51_000.0, tp=48_000.0)
        bar   = _bar(open_=50_000.0, high=51_500.0, low=48_500.0, close=49_800.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_STOP_LOSS
        assert exit_price == pytest.approx(51_000.0)   # SL level

    def test_sell_only_tp_wicked_exits_at_tp_level(self):
        """SELL: only TP hit → exit_price = TP level."""
        engine = _engine()
        # entry=50000, SL=51000, TP=48000
        # bar: high=50500 (no SL wick), low=47000 (wicks TP)
        trade = _trade("SELL", entry=50_000.0, sl=51_000.0, tp=48_000.0)
        bar   = _bar(open_=50_000.0, high=50_500.0, low=47_000.0, close=49_500.0)

        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, exit_price = result
        assert reason     == EXIT_TAKE_PROFIT
        assert exit_price == pytest.approx(48_000.0)   # TP level

    def test_no_wick_returns_none(self):
        """Bar that touches neither SL nor TP → None."""
        engine = _engine()
        # entry=50000, SL=49000, TP=52000
        # bar: low=49500 (no SL), high=51500 (no TP)
        trade = _trade("BUY", entry=50_000.0, sl=49_000.0, tp=52_000.0)
        bar   = _bar(open_=50_000.0, high=51_500.0, low=49_500.0, close=50_800.0)

        result = engine._check_exit(trade, bar)
        assert result is None
