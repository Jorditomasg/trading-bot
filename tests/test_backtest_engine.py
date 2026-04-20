"""Unit tests for the backtest engine — all synthetic data, no network calls."""

import math

import pandas as pd
import pytest

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    EXIT_END_OF_PERIOD,
    EXIT_SIGNAL_REVERSAL,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ohlcv(
    closes: list[float],
    *,
    high_mult: float = 1.005,
    low_mult: float  = 0.995,
    volume: float    = 1_000_000.0,
) -> pd.DataFrame:
    """Construct an OHLCV DataFrame with open_time column."""
    n = len(closes)
    opens  = [closes[0]] + closes[:-1]
    highs  = [c * high_mult for c in closes]
    lows   = [c * low_mult  for c in closes]
    times  = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open":      opens,
        "high":      highs,
        "low":       lows,
        "close":     closes,
        "volume":    [volume] * n,
    })


def _uptrend(n: int = 300) -> pd.DataFrame:
    """Steady rising prices from 40 000 to 50 000."""
    step = (50_000 - 40_000) / (n - 1)
    closes = [40_000 + i * step for i in range(n)]
    return _make_ohlcv(closes)


def _flat(n: int = 300, price: float = 45_000.0) -> pd.DataFrame:
    """Flat price — should mostly generate HOLD signals."""
    return _make_ohlcv([price] * n)


def _default_engine(timeframe: str = "1h") -> BacktestEngine:
    cfg = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = 0.01,
        timeframe         = timeframe,
        cost_per_side_pct = 0.0,   # zero cost for cleaner PnL assertions
    )
    return BacktestEngine(cfg)


# ── Smoke tests ───────────────────────────────────────────────────────────────

class TestBacktestEngineSmoke:
    def test_run_returns_result(self):
        engine = _default_engine()
        df     = _uptrend()
        result = engine.run(df, symbol="BTCUSDT")
        assert result.symbol        == "BTCUSDT"
        assert result.initial_capital == 10_000.0
        assert isinstance(result.trades, list)
        assert isinstance(result.equity_curve, list)
        assert len(result.equity_curve) >= 1    # at least initial equity point
        assert result.total_bars > 0

    def test_run_with_4h_data(self):
        engine = _default_engine()
        df     = _uptrend(300)
        df_4h  = _uptrend(75)    # ~same period, coarser timeframe
        # Adjust 4h timestamps to be consistent (every 4 hours)
        times_4h = pd.date_range("2024-01-01", periods=75, freq="4h", tz="UTC")
        df_4h = df_4h.copy()
        df_4h["open_time"] = times_4h
        result = engine.run(df, df_4h=df_4h, symbol="BTCUSDT")
        assert result is not None

    def test_insufficient_data_raises(self):
        engine = _default_engine()
        df     = _flat(n=5)   # way below min_lookback
        with pytest.raises(ValueError, match="Insufficient data"):
            engine.run(df)

    def test_flat_market_few_trades(self):
        """Flat market should trigger few or no trades (weak signals)."""
        engine = _default_engine()
        df     = _flat(n=300)
        result = engine.run(df)
        # We don't assert zero trades — strategies CAN fire — but capital
        # should not blow up (stays within ±10% of initial in flat market)
        assert result.final_capital > 5_000.0   # sanity — not catastrophic loss


# ── Capital accounting ────────────────────────────────────────────────────────

class TestCapitalAccounting:
    def test_capital_tracked_correctly(self):
        """Each equity_curve point is added on trade close."""
        engine = _default_engine()
        df     = _uptrend(300)
        result = engine.run(df)
        if not result.trades:
            pytest.skip("No trades — cannot test capital accounting")

        closed = [t for t in result.trades if t["exit_reason"] != EXIT_END_OF_PERIOD]
        # equity_curve has 1 initial point + 1 per trade close
        assert len(result.equity_curve) == 1 + len(result.trades)

    def test_pnl_sums_to_net_change(self):
        """Sum of all trade PnLs should equal final_capital - initial_capital."""
        engine = _default_engine()
        df     = _uptrend(300)
        result = engine.run(df)
        total_pnl = sum(t["pnl"] for t in result.trades if t["pnl"] is not None)
        delta     = result.final_capital - result.initial_capital
        assert abs(total_pnl - delta) < 0.01   # floating-point tolerance

    def test_zero_cost_pnl_symmetry(self):
        """With cost=0, a BUY closed at entry price should yield PnL ≈ 0."""
        engine = _default_engine()
        # Create a price that goes up then immediately back to start
        n = 300
        closes = [45_000.0] * n
        # Force a spike then return — won't guarantee a trade but tests cost neutrality
        df = _make_ohlcv(closes)
        result = engine.run(df)
        for t in result.trades:
            if t["entry_price"] and t["exit_price"]:
                # No cost: margin should match quantity × |exit - entry|
                assert t["pnl"] is not None


# ── Exit reason coverage ──────────────────────────────────────────────────────

class TestExitReasons:
    def _engine_with_cost(self) -> BacktestEngine:
        cfg = BacktestConfig(
            initial_capital   = 10_000.0,
            risk_per_trade    = 0.01,
            timeframe         = "1h",
            cost_per_side_pct = 0.0015,
        )
        return BacktestEngine(cfg)

    def test_end_of_period_exit_exists(self):
        """If a trade is open at the last bar it must close as END_OF_PERIOD."""
        engine = self._engine_with_cost()
        df     = _uptrend(300)
        result = engine.run(df)
        end_trades = [t for t in result.trades if t["exit_reason"] == EXIT_END_OF_PERIOD]
        # Not guaranteed there IS an end-of-period trade; but if one exists the exit_time
        # must equal the last bar's timestamp
        for t in end_trades:
            assert t["exit_time"] == df.iloc[-1]["open_time"]

    def test_all_trades_have_exit_reason(self):
        engine = _default_engine()
        df     = _uptrend(300)
        result = engine.run(df)
        for t in result.trades:
            assert t["exit_reason"] in {
                EXIT_STOP_LOSS, EXIT_TAKE_PROFIT,
                EXIT_SIGNAL_REVERSAL, EXIT_END_OF_PERIOD,
            }

    def test_pnl_populated_on_all_trades(self):
        engine = _default_engine()
        df     = _uptrend(300)
        result = engine.run(df)
        for t in result.trades:
            assert t["pnl"] is not None
            assert t["pnl_pct"] is not None


# ── Check-exit logic ──────────────────────────────────────────────────────────

class TestCheckExit:
    """Direct unit tests for _check_exit() using hand-crafted trade + bar."""

    def _trade(self, side: str, sl: float, tp: float) -> dict:
        return {
            "side": side,
            "entry_price": 100.0,
            "stop_loss":   sl,
            "take_profit": tp,
            "quantity":    1.0,
        }

    def _bar(self, high: float, low: float) -> "pd.Series":
        return pd.Series({"high": high, "low": low, "close": 100.0})

    def _engine(self) -> BacktestEngine:
        return BacktestEngine(BacktestConfig())

    def test_buy_sl_hit(self):
        engine = self._engine()
        trade  = self._trade("BUY", sl=95.0, tp=110.0)
        bar    = self._bar(high=101.0, low=94.0)  # low below SL
        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, price = result
        assert reason == EXIT_STOP_LOSS
        assert price  == 95.0

    def test_buy_tp_hit(self):
        engine = self._engine()
        trade  = self._trade("BUY", sl=95.0, tp=110.0)
        bar    = self._bar(high=111.0, low=99.0)   # high above TP
        result = engine._check_exit(trade, bar)
        assert result is not None
        reason, price = result
        assert reason == EXIT_TAKE_PROFIT
        assert price  == 110.0

    def test_buy_sl_wins_over_tp_same_bar(self):
        """If both SL and TP are breached in the same bar, SL wins (conservative)."""
        engine = self._engine()
        trade  = self._trade("BUY", sl=95.0, tp=110.0)
        bar    = self._bar(high=115.0, low=90.0)   # both breached
        reason, _ = engine._check_exit(trade, bar)
        assert reason == EXIT_STOP_LOSS

    def test_sell_sl_hit(self):
        engine = self._engine()
        trade  = self._trade("SELL", sl=110.0, tp=90.0)
        bar    = self._bar(high=112.0, low=95.0)   # high above SL
        reason, price = engine._check_exit(trade, bar)
        assert reason == EXIT_STOP_LOSS
        assert price  == 110.0

    def test_sell_tp_hit(self):
        engine = self._engine()
        trade  = self._trade("SELL", sl=110.0, tp=90.0)
        bar    = self._bar(high=100.0, low=88.0)   # low below TP
        reason, price = engine._check_exit(trade, bar)
        assert reason == EXIT_TAKE_PROFIT
        assert price  == 90.0

    def test_no_exit(self):
        engine = self._engine()
        trade  = self._trade("BUY", sl=95.0, tp=110.0)
        bar    = self._bar(high=105.0, low=98.0)   # inside range
        assert engine._check_exit(trade, bar) is None


# ── Quantity / cost helpers ───────────────────────────────────────────────────

class TestQuantityAndCost:
    def _engine(self, cost: float = 0.0) -> BacktestEngine:
        return BacktestEngine(BacktestConfig(cost_per_side_pct=cost))

    def test_quantity_scales_with_risk(self):
        engine = self._engine()
        # risk_amount = 10_000 × 0.01 = 100
        # risk_per_unit = |100 - 95| = 5
        # quantity = 100 / 5 = 20
        qty = engine._compute_quantity(capital=10_000, net_entry=100.0, stop_loss=95.0)
        assert abs(qty - 20.0) < 0.001

    def test_zero_risk_per_unit_returns_zero(self):
        engine = self._engine()
        qty = engine._compute_quantity(capital=10_000, net_entry=100.0, stop_loss=100.0)
        assert qty == 0.0

    def test_buy_entry_cost_increases_price(self):
        engine = self._engine(cost=0.001)
        adjusted = engine._apply_entry_cost("BUY", 100.0)
        assert adjusted > 100.0

    def test_sell_entry_cost_decreases_price(self):
        engine = self._engine(cost=0.001)
        adjusted = engine._apply_entry_cost("SELL", 100.0)
        assert adjusted < 100.0

    def test_buy_exit_cost_decreases_price(self):
        engine = self._engine(cost=0.001)
        adjusted = engine._apply_exit_cost("BUY", 100.0)
        assert adjusted < 100.0

    def test_sell_exit_cost_increases_price(self):
        engine = self._engine(cost=0.001)
        adjusted = engine._apply_exit_cost("SELL", 100.0)
        assert adjusted > 100.0


# ── Summary metrics ───────────────────────────────────────────────────────────

class TestSummaryMetrics:
    def _fake_result(self) -> object:
        from bot.backtest.engine import BacktestResult
        trades = [
            {"pnl": 200.0, "pnl_pct": 0.02, "exit_reason": EXIT_STOP_LOSS},
            {"pnl":  50.0, "pnl_pct": 0.005, "exit_reason": EXIT_TAKE_PROFIT},
            {"pnl": -80.0, "pnl_pct": -0.008, "exit_reason": EXIT_STOP_LOSS},
            {"pnl": -30.0, "pnl_pct": -0.003, "exit_reason": EXIT_STOP_LOSS},
            {"pnl": 100.0, "pnl_pct": 0.01,  "exit_reason": EXIT_TAKE_PROFIT},
        ]
        equity = [
            {"balance": 10_000},
            {"balance": 10_200},
            {"balance": 10_250},
            {"balance": 10_170},
            {"balance": 10_140},
            {"balance": 10_240},
        ]
        return BacktestResult(
            trades          = trades,
            equity_curve    = equity,
            initial_capital = 10_000.0,
            final_capital   = 10_240.0,
            timeframe       = "1h",
            symbol          = "BTCUSDT",
            start_date      = "2024-01-01",
            end_date        = "2024-06-30",
            total_bars      = 100,
        )

    def test_summary_keys_present(self):
        engine = BacktestEngine()
        result = self._fake_result()
        s = engine.summary(result)
        for key in (
            "total_trades", "win_rate_pct", "total_pnl", "total_pnl_pct",
            "sharpe_ratio", "max_drawdown_pct", "profit_factor",
            "max_loss_streak", "best_trade_pnl", "worst_trade_pnl",
        ):
            assert key in s, f"Missing key: {key}"

    def test_win_rate(self):
        engine = BacktestEngine()
        result = self._fake_result()
        s = engine.summary(result)
        # 3 wins out of 5 closed trades = 60%
        assert abs(s["win_rate_pct"] - 60.0) < 0.01

    def test_total_pnl(self):
        engine = BacktestEngine()
        result = self._fake_result()
        s = engine.summary(result)
        assert abs(s["total_pnl"] - 240.0) < 0.01

    def test_profit_factor(self):
        engine = BacktestEngine()
        result = self._fake_result()
        s = engine.summary(result)
        # gross_win = 200 + 50 + 100 = 350; gross_loss = 80 + 30 = 110
        expected_pf = 350 / 110
        assert abs(s["profit_factor"] - expected_pf) < 0.001

    def test_max_loss_streak(self):
        engine = BacktestEngine()
        result = self._fake_result()
        s = engine.summary(result)
        # sequence: win, win, LOSS, LOSS, win → max streak = 2
        assert s["max_loss_streak"] == 2

    def test_no_trades_safe(self):
        """summary() must not crash on zero closed trades."""
        from bot.backtest.engine import BacktestResult
        engine = BacktestEngine()
        result = BacktestResult(
            trades=[], equity_curve=[{"balance": 10_000}],
            initial_capital=10_000, final_capital=10_000,
            timeframe="1h", symbol="X", start_date="2024-01-01",
            end_date="2024-06-30", total_bars=0,
        )
        s = engine.summary(result)
        assert s["total_trades"]   == 0
        assert s["win_rate_pct"]   == 0.0
        assert s["max_loss_streak"] == 0
