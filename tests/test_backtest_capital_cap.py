"""Tests for spot capital cap parity between BacktestEngine and live RiskManager.

The live bot caps quantity at min(qty_by_risk, qty_by_capital) where:
    qty_by_capital = (capital * 0.99) / entry

The backtest engine must apply the same cap. These tests verify the parity.
"""

import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig, BacktestEngine


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ohlcv(closes: list[float], *, high_mult: float = 1.005, low_mult: float = 0.995) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame with UTC timestamps."""
    n = len(closes)
    times = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open":      closes,
        "high":      [c * high_mult for c in closes],
        "low":       [c * low_mult  for c in closes],
        "close":     closes,
        "volume":    [1_000_000.0] * n,
    })


# ── Unit tests for _compute_quantity_with_risk (method-level) ─────────────────

class TestComputeQuantityCapUnit:
    """Test the sizing method directly — no full run() needed."""

    def _engine(self, capital: float = 10_000.0, risk: float = 0.01) -> BacktestEngine:
        cfg = BacktestConfig(
            initial_capital=capital,
            risk_per_trade=risk,
            timeframe="1h",
            cost_per_side_pct=0.0,
            kelly_enabled=False,  # isolate capital cap, disable Kelly
        )
        return BacktestEngine(cfg)

    def test_normal_sizing_no_cap(self):
        """When risk-based qty fits in capital, cap does not activate.

        capital=10000, risk=1%, entry=50000, SL=49500 → risk_dist=500
        qty_by_risk = (10000 * 0.01) / 500 = 0.20
        qty_by_capital = (10000 * 0.99) / 50000 = 0.198
        min(0.20, 0.198) = 0.198  ← cap activates here, but let's pick params where it doesn't
        """
        # Use a small entry price so qty_by_capital is large
        engine = self._engine(capital=10_000.0, risk=0.01)
        # entry=1000, SL=990  → risk_dist=10
        # qty_by_risk = (10000*0.01)/10 = 10
        # qty_by_capital = (10000*0.99)/1000 = 9.9  → cap: 9.9
        # This case actually caps, so let's make risk small enough not to cap
        # entry=1000, SL=500 → risk_dist=500
        # qty_by_risk = (10000*0.01)/500 = 0.2
        # qty_by_capital = (10000*0.99)/1000 = 9.9 → no cap
        qty = engine._compute_quantity_with_risk(
            capital=10_000.0, net_entry=1_000.0, stop_loss=500.0, risk_per_trade=0.01
        )
        qty_by_risk = (10_000.0 * 0.01) / abs(1_000.0 - 500.0)
        assert qty == pytest.approx(qty_by_risk, rel=1e-5)

    def test_aggressive_risk_tight_sl_triggers_cap(self):
        """When risk × tight SL would need > 100% of capital, cap applies.

        capital=10000, risk=5%, entry=50000, SL=49750 → risk_dist=250
        qty_by_risk = (10000*0.05)/250 = 2.0  → notional=100000 → 10x capital
        qty_by_capital = (10000*0.99)/50000 = 0.198
        min(2.0, 0.198) = 0.198 (cap activates)
        """
        engine = self._engine(capital=10_000.0, risk=0.05)
        qty = engine._compute_quantity_with_risk(
            capital=10_000.0, net_entry=50_000.0, stop_loss=49_750.0, risk_per_trade=0.05
        )
        qty_by_capital = (10_000.0 * 0.99) / 50_000.0
        assert qty == pytest.approx(qty_by_capital, rel=1e-5)

    def test_boundary_risk_exactly_fills_capital(self):
        """When risk-based notional = exactly capital (boundary), cap still applies (99% rule).

        capital=10000, entry=1000, SL=0 (extreme) → risk covers full price
        entry=10000, SL=0: qty_by_risk = (10000*1.0)/10000 = 1.0
        qty_by_capital = (10000*0.99)/10000 = 0.99
        min(1.0, 0.99) = 0.99 (cap activates at 99%)
        """
        engine = self._engine(capital=10_000.0, risk=1.0)  # 100% risk (extreme)
        qty = engine._compute_quantity_with_risk(
            capital=10_000.0, net_entry=10_000.0, stop_loss=0.0, risk_per_trade=1.0
        )
        qty_by_capital = (10_000.0 * 0.99) / 10_000.0
        assert qty == pytest.approx(qty_by_capital, rel=1e-5)


# ── Integration tests — verify cap survives in full run() ─────────────────────

class TestCapitalCapIntegration:
    """Run the engine end-to-end and verify sizing respects the cap."""

    def _engine(self, capital: float = 10_000.0, risk: float = 0.01) -> BacktestEngine:
        cfg = BacktestConfig(
            initial_capital=capital,
            risk_per_trade=risk,
            timeframe="1h",
            cost_per_side_pct=0.0,
            kelly_enabled=False,
        )
        return BacktestEngine(cfg)

    def _rising_then_flat(self, n: int = 300) -> pd.DataFrame:
        """Strong rising trend to generate BUY signals."""
        step = (50_000 - 40_000) / (n - 1)
        closes = [40_000 + i * step for i in range(n)]
        return _ohlcv(closes)

    def test_no_trade_notional_exceeds_10x_initial(self):
        """With the cap, no trade can notional >10x initial capital.

        Without the cap, an aggressive risk=50% on BTC ~45k with a tight ATR-based
        SL would compute qty_by_risk = (10000*0.50)/small_sl = very large number,
        causing notional to be many times capital. The cap constrains it to
        qty_by_capital = (capital * 0.99) / entry → notional ≤ capital * 0.99.
        Since capital can grow 2-3x in a bull run, we use a 5x guard to detect
        if the cap is completely absent.
        """
        cfg = BacktestConfig(
            initial_capital=10_000.0,
            risk_per_trade=0.50,       # very aggressive: 50%
            timeframe="1h",
            cost_per_side_pct=0.0,
            long_only=True,
            kelly_enabled=False,
        )
        engine = BacktestEngine(cfg)
        df = self._rising_then_flat()
        result = engine.run(df)

        for trade in result.trades:
            notional = trade["entry_price"] * trade["quantity"]
            # Cap in place: qty ≤ (capital*0.99)/entry → notional ≤ capital*0.99.
            # Capital can grow over time but not 5x in a normal backtest.
            assert notional <= cfg.initial_capital * 5, (
                f"Trade notional {notional:.2f} is unreasonably large "
                f"(entry={trade['entry_price']}, qty={trade['quantity']}). "
                f"Capital cap missing?"
            )

    def test_qty_by_risk_vs_qty_by_capital_capped(self):
        """Directly verify that sizing is capped: qty <= (capital*0.99)/entry.

        We pick parameters where risk-based qty would far exceed capital-based qty,
        then assert the returned qty equals the capital-based cap.
        """
        engine = self._engine(capital=10_000.0, risk=0.50)
        # entry=45000, SL=44500 → risk_dist=500
        # qty_by_risk = (10000 * 0.50) / 500 = 10  → notional = 450000 (45x capital!)
        # qty_by_capital = (10000 * 0.99) / 45000 = 0.22
        qty = engine._compute_quantity_with_risk(
            capital=10_000.0, net_entry=45_000.0, stop_loss=44_500.0, risk_per_trade=0.50
        )
        qty_by_capital = (10_000.0 * 0.99) / 45_000.0
        assert qty == pytest.approx(qty_by_capital, rel=1e-4), (
            f"Expected cap to apply: {qty:.5f} should == {qty_by_capital:.5f}"
        )

    def test_portfolio_engine_no_trade_exceeds_10x_initial(self):
        """Same cap must apply in PortfolioBacktestEngine."""
        from bot.backtest.portfolio_engine import PortfolioBacktestEngine

        cfg = BacktestConfig(
            initial_capital=10_000.0,
            risk_per_trade=0.50,
            timeframe="1h",
            cost_per_side_pct=0.0,
            long_only=True,
            kelly_enabled=False,
        )
        engine = PortfolioBacktestEngine(cfg)
        step = (50_000 - 40_000) / 299
        closes = [40_000 + i * step for i in range(300)]
        df = _ohlcv(closes)
        result = engine.run_portfolio({"BTCUSDT": df})

        all_trades = [t for trades in result.per_symbol_trades.values() for t in trades]
        for trade in all_trades:
            notional = trade["entry_price"] * trade["quantity"]
            assert notional <= cfg.initial_capital * 5, (
                f"Portfolio trade notional {notional:.2f} is unreasonably large. "
                f"Capital cap missing in portfolio engine?"
            )
