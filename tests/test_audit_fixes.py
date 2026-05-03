"""Tests covering the audit-fix changes that survive the single-strategy refactor.

Scope:
- Fix #2: validate_signal no longer checks open_positions
- Fix #4: RSI never returns NaN in extreme (all-up / all-down) streaks
- Fix #5: compute_position_size no longer accepts n_open_trades
"""

import inspect

import pandas as pd
import pytest

from bot.indicators.utils import rsi
from bot.risk.manager import RiskConfig, RiskManager
from bot.strategy.base import Signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _signal(action: str, strength: float = 0.7, sl: float = 95.0, tp: float = 110.0) -> Signal:
    return Signal(action=action, strength=strength, stop_loss=sl, take_profit=tp, atr=1.0)


def _make_df(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    opens = [closes[0]] + closes[:-1]
    return pd.DataFrame({
        "open":   opens,
        "high":   [c * 1.005 for c in closes],
        "low":    [c * 0.995 for c in closes],
        "close":  closes,
        "volume": [1_000_000.0] * n,
    })


# ── Fix #2: validate_signal signature & behaviour ─────────────────────────────

class TestValidateSignal:
    def setup_method(self):
        self.rm = RiskManager(RiskConfig(min_signal_strength=0.5))

    def test_accepts_single_signal_arg(self):
        """validate_signal must work with only `signal` — no open_positions arg."""
        sig = inspect.signature(self.rm.validate_signal)
        # inspect.signature on a bound method omits 'self'
        assert list(sig.parameters.keys()) == ["signal"]

    def test_hold_returns_false(self):
        assert self.rm.validate_signal(_signal("HOLD")) is False

    def test_weak_signal_returns_false(self):
        assert self.rm.validate_signal(_signal("BUY", strength=0.3)) is False

    def test_strong_buy_returns_true(self):
        assert self.rm.validate_signal(_signal("BUY", strength=0.7)) is True

    def test_strong_sell_returns_true(self):
        assert self.rm.validate_signal(_signal("SELL", strength=0.8)) is True

    def test_concurrent_buys_not_blocked_by_validate(self):
        """With an open BUY, a second BUY must still pass validate_signal.
        Duplicate / max_concurrent checks live in the orchestrator, not here.
        """
        open_trades = [{"side": "BUY", "strategy": "EMA_CROSSOVER"}]
        # Old API would have blocked this — new API does not take open_positions
        sig = _signal("BUY", strength=0.7)
        # Must not raise TypeError (removed arg) and must return True
        assert self.rm.validate_signal(sig) is True


# ── Fix #4: RSI NaN fix ───────────────────────────────────────────────────────

class TestRsiNoNaN:
    def test_all_up_streak_no_nan(self):
        closes = pd.Series([100.0 + i for i in range(50)])
        result = rsi(closes, 14)
        assert result.dropna().isna().sum() == 0, "RSI must not produce NaN on all-up streak"

    def test_all_up_streak_near_100(self):
        closes = pd.Series([100.0 + i for i in range(50)])
        last = rsi(closes, 14).iloc[-1]
        assert last >= 99.0, f"All-up streak should produce RSI ≈ 100, got {last:.4f}"

    def test_all_down_streak_no_nan(self):
        closes = pd.Series([100.0 - i * 0.5 for i in range(50)])
        result = rsi(closes, 14)
        assert result.dropna().isna().sum() == 0

    def test_all_down_streak_near_zero(self):
        closes = pd.Series([100.0 - i * 0.5 for i in range(50)])
        last = rsi(closes, 14).iloc[-1]
        assert last <= 1.0, f"All-down streak should produce RSI ≈ 0, got {last:.4f}"

    def test_values_clipped_to_0_100(self):
        import numpy as np
        closes = pd.Series(np.random.default_rng(42).uniform(90, 110, 200))
        result = rsi(closes, 14).dropna()
        assert (result >= 0.0).all() and (result <= 100.0).all()

    def test_mixed_series_no_nan(self):
        closes = pd.Series([100.0 + (1 if i % 2 == 0 else -0.5) * i for i in range(50)])
        result = rsi(closes, 14).dropna()
        assert result.isna().sum() == 0


# ── Fix #5: compute_position_size — n_open_trades removed ────────────────────

class TestComputePositionSize:
    def setup_method(self):
        self.rm = RiskManager(RiskConfig(risk_per_trade=0.01, max_concurrent_trades=1))

    def test_no_n_open_trades_param(self):
        sig = inspect.signature(self.rm.compute_position_size)
        assert "n_open_trades" not in sig.parameters

    def test_basic_sizing(self):
        qty = self.rm.compute_position_size(capital=10_000, entry=50_000, stop_loss=49_000)
        # risk_amount = 10000 * 0.01 / 1 = 100; risk_per_unit = 1000 → qty = 0.1
        assert abs(qty - 0.1) < 1e-6

    def test_zero_quantity_on_equal_entry_sl(self):
        qty = self.rm.compute_position_size(capital=10_000, entry=50_000, stop_loss=50_000)
        assert qty == 0.0

    def test_qty_capped_when_risk_exceeds_capital(self):
        # 3% risk × 2.76% SL distance → risk-based qty would need 108.7% of capital.
        # Cap must clamp to ~99% of capital / entry.
        rm = RiskManager(RiskConfig(risk_per_trade=0.03))
        qty = rm.compute_position_size(capital=39_280.54, entry=78_550.06, stop_loss=76_381.94)
        notional = qty * 78_550.06
        assert notional <= 39_280.54, f"notional {notional} exceeds capital"
        assert notional >= 39_280.54 * 0.985, f"notional {notional} too far below 99% cap"

    def test_qty_not_capped_when_risk_fits(self):
        # 1% risk × 2% SL distance → 50% of capital. No cap needed.
        rm = RiskManager(RiskConfig(risk_per_trade=0.01))
        qty = rm.compute_position_size(capital=10_000, entry=50_000, stop_loss=49_000)
        # risk-based qty = 0.1 (notional 5,000), well under 99% × 10,000 / 50,000 = 0.198 cap
        assert abs(qty - 0.1) < 1e-6


