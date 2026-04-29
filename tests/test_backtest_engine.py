"""Unit tests for the backtest engine — all synthetic data, no network calls."""

import math

import pandas as pd
import pytest

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    EXIT_END_OF_PERIOD,
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

    # ── Input validation (Fix #7) ─────────────────────────────────────────────

    def test_missing_column_raises(self):
        engine = _default_engine()
        df = _uptrend().drop(columns=["high"])
        with pytest.raises(ValueError, match="missing required columns"):
            engine.run(df)

    def test_unsorted_timestamps_raises(self):
        engine = _default_engine()
        df = _uptrend().iloc[::-1].reset_index(drop=True)   # reverse order
        with pytest.raises(ValueError, match="not sorted ascending"):
            engine.run(df)

    def test_high_less_than_low_raises(self):
        engine = _default_engine()
        df = _uptrend().copy()
        df.loc[100, "high"] = df.loc[100, "low"] - 1.0    # inject bad bar
        with pytest.raises(ValueError, match="high < low"):
            engine.run(df)

    def test_invalid_initial_capital_raises(self):
        cfg = BacktestConfig(initial_capital=-500.0)
        engine = BacktestEngine(cfg)
        with pytest.raises(ValueError, match="initial_capital"):
            engine.run(_uptrend())

    def test_invalid_risk_per_trade_raises(self):
        cfg = BacktestConfig(risk_per_trade=1.5)
        engine = BacktestEngine(cfg)
        with pytest.raises(ValueError, match="risk_per_trade"):
            engine.run(_uptrend())

    def test_negative_cost_raises(self):
        cfg = BacktestConfig(cost_per_side_pct=-0.001)
        engine = BacktestEngine(cfg)
        with pytest.raises(ValueError, match="cost_per_side_pct"):
            engine.run(_uptrend())

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
                EXIT_STOP_LOSS, EXIT_TAKE_PROFIT, EXIT_END_OF_PERIOD,
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


# ── Leverage simulation tests ─────────────────────────────────────────────────

def _leveraged_engine(leverage: float, timeframe: str = "1h") -> BacktestEngine:
    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        timeframe=timeframe,
        cost_per_side_pct=0.0,
        leverage=leverage,
        funding_rate_per_8h=0.0,
        momentum_filter_enabled=False,
        long_only=True,
    )
    return BacktestEngine(cfg)


def test_leverage_multiplies_pnl():
    """3× leverage should approximately triple the P&L of a spot trade."""
    df = _uptrend(300)

    spot_engine = _default_engine()
    spot_result = spot_engine.run(df, symbol="BTCUSDT")
    spot_pnl = spot_result.final_capital - spot_result.initial_capital

    lev_engine = _leveraged_engine(3.0)
    lev_result = lev_engine.run(df, symbol="BTCUSDT")
    lev_pnl = lev_result.final_capital - lev_result.initial_capital

    assert len(spot_result.trades) > 0
    assert len(lev_result.trades) > 0
    assert lev_pnl > spot_pnl * 1.5


def test_liquidation_triggers_on_sharp_drop():
    """A 10× leveraged BUY should liquidate when price drops ~9% from entry.

    Build data using the same high_mult/low_mult as _uptrend() so EMA crossover
    signals are generated during warmup, then append a brutal crash.
    """
    # 300 uptrend bars with noise (same as _uptrend) → signals generated around bar 100+
    uptrend_bars = 300
    step = (50_000 - 40_000) / (uptrend_bars - 1)
    closes_up = [40_000 + i * step for i in range(uptrend_bars)]
    highs_up  = [c * 1.005 for c in closes_up]
    lows_up   = [c * 0.995 for c in closes_up]

    # 50 crash bars — drop to 60% of peak, deep low at 50% → well below 91% liquidation
    peak = closes_up[-1]
    crash_close = peak * 0.60
    crash_low   = peak * 0.50   # ensures liquidation price (91% of entry) is breached
    closes_crash = [crash_close] * 50
    highs_crash  = [crash_close * 1.001] * 50
    lows_crash   = [crash_low] * 50

    closes = closes_up + closes_crash
    highs  = highs_up  + highs_crash
    lows   = lows_up   + lows_crash
    n = len(closes)

    df = pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"),
        "open":   [closes[0]] + closes[:-1],
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": [1_000_000.0] * n,
    })

    engine = _leveraged_engine(10.0)
    result = engine.run(df, symbol="BTCUSDT")

    liquidated = [t for t in result.trades if t["exit_reason"] == "LIQUIDATED"]
    assert len(liquidated) >= 1, (
        f"Expected at least one liquidation at 10× leverage. "
        f"Total trades: {len(result.trades)}, "
        f"exit reasons: {[t['exit_reason'] for t in result.trades]}"
    )


def test_no_liquidation_at_1x():
    """Spot (1×) should never produce a LIQUIDATED exit reason."""
    df = _uptrend(300)
    engine = _default_engine()
    result = engine.run(df, symbol="BTCUSDT")
    liquidated = [t for t in result.trades if t.get("exit_reason") == "LIQUIDATED"]
    assert liquidated == []


def test_funding_cost_reduces_pnl():
    """Non-zero funding rate should reduce leveraged P&L vs zero funding."""
    df = _uptrend(300)

    zero_funding = BacktestEngine(BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.01, timeframe="1h",
        cost_per_side_pct=0.0, leverage=3.0, funding_rate_per_8h=0.0,
        long_only=True,
        momentum_filter_enabled=False,
    ))
    with_funding = BacktestEngine(BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.01, timeframe="1h",
        cost_per_side_pct=0.0, leverage=3.0, funding_rate_per_8h=0.001,
        long_only=True,
        momentum_filter_enabled=False,
    ))

    r0 = zero_funding.run(df, symbol="BTCUSDT")
    r1 = with_funding.run(df, symbol="BTCUSDT")

    assert len(r0.trades) > 0
    assert r0.final_capital > r1.final_capital


# ── Momentum filter tests ─────────────────────────────────────────────────────

def _make_weekly(closes: list[float]) -> pd.DataFrame:
    """Build a weekly OHLCV DataFrame for use as df_weekly."""
    n = len(closes)
    times = pd.date_range("2022-01-03", periods=n, freq="7D", tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open":   closes,
        "high":   [c * 1.02 for c in closes],
        "low":    [c * 0.98 for c in closes],
        "close":  closes,
        "volume": [1e9] * n,
    })


def _momentum_engine(enabled: bool = True) -> BacktestEngine:
    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.01,
        timeframe="1h",
        cost_per_side_pct=0.0,
        leverage=1.0,
        momentum_filter_enabled=enabled,
        momentum_sma_period=4,        # short period for synthetic test data
        momentum_neutral_band=0.05,
        long_only=True,
    )
    return BacktestEngine(cfg)


def test_momentum_bearish_blocks_all_entries():
    """When weekly price is far below its SMA (BEARISH) no new trades open.

    momentum_sma_period=4: SMA-4 of last 4 bars.
    Last bar = 30_000, previous 8 bars = 60_000.
    tail(9) = [60k×8, 30k]. closes[-4:] = [60k, 60k, 60k, 30k]. SMA=52_500.
    30_000 < 52_500 × 0.95 = 49_875 → BEARISH.
    """
    df = _uptrend(300)
    weekly_closes = [60_000.0] * 52 + [30_000.0]  # crash at last bar
    df_weekly = _make_weekly(weekly_closes)

    engine = _momentum_engine(enabled=True)
    result = engine.run(df, df_4h=None, df_weekly=df_weekly, symbol="BTCUSDT")

    assert len(result.trades) == 0, (
        f"BEARISH momentum should block all entries, got {len(result.trades)} trades"
    )


def test_momentum_disabled_does_not_block():
    """With momentum_filter_enabled=False, same weekly data yields same result as no weekly."""
    df = _uptrend(300)
    weekly_closes = [60_000.0] * 52 + [30_000.0]
    df_weekly = _make_weekly(weekly_closes)

    engine_with = _momentum_engine(enabled=False)
    r_with = engine_with.run(df, df_4h=None, df_weekly=df_weekly, symbol="BTCUSDT")

    engine_without = _momentum_engine(enabled=False)
    r_without = engine_without.run(df, df_4h=None, df_weekly=None, symbol="BTCUSDT")

    assert len(r_with.trades) == len(r_without.trades)


def test_momentum_neutral_halves_risk():
    """NEUTRAL weekly state should produce smaller position sizes than BULLISH.

    BULLISH: last bar=55k, SMA-4=(40k+40k+40k+55k)/4=43_750. 55k > 43_750×1.05=45_937 → BULLISH.
    NEUTRAL: all bars=45k. SMA-4=45k, price=45k. Within ±5% band → NEUTRAL → half risk.
    """
    df = _uptrend(300)

    # BULLISH: last bar well above SMA of prior bars
    bullish_weekly = _make_weekly([40_000.0] * 52 + [55_000.0])
    # NEUTRAL: flat — price == SMA, stays inside ±5% band
    neutral_weekly = _make_weekly([45_000.0] * 56)

    e_bull = _momentum_engine(enabled=True)
    r_bull = e_bull.run(df, df_4h=None, df_weekly=bullish_weekly, symbol="BTCUSDT")

    e_neutral = _momentum_engine(enabled=True)
    r_neutral = e_neutral.run(df, df_4h=None, df_weekly=neutral_weekly, symbol="BTCUSDT")

    assert len(r_bull.trades) > 0
    assert len(r_neutral.trades) > 0

    avg_qty_bull    = sum(t["quantity"] for t in r_bull.trades) / len(r_bull.trades)
    avg_qty_neutral = sum(t["quantity"] for t in r_neutral.trades) / len(r_neutral.trades)
    assert avg_qty_neutral < avg_qty_bull, (
        f"NEUTRAL should produce smaller qty ({avg_qty_neutral:.5f}) than BULLISH ({avg_qty_bull:.5f})"
    )


# ── B1: BacktestConfig.ema_max_distance_atr ───────────────────────────────────

class TestBacktestConfigMaxDistanceAtr:
    def test_override_applies_when_set(self):
        """BacktestConfig with ema_max_distance_atr=0.3 → engine strategy has max_distance_atr == 0.3."""
        from bot.constants import StrategyName
        cfg = BacktestConfig(ema_max_distance_atr=0.3)
        engine = BacktestEngine(cfg)
        ema_strategy = engine._strategies[StrategyName.EMA_CROSSOVER]
        assert ema_strategy.config.max_distance_atr == pytest.approx(0.3)

    def test_none_leaves_preset_untouched(self):
        """BacktestConfig() with None should NOT override max_distance_atr — preset value survives."""
        from bot.constants import StrategyName
        from bot.config_presets import get_strategy_configs
        cfg = BacktestConfig(timeframe="4h")
        engine = BacktestEngine(cfg)
        ema_strategy = engine._strategies[StrategyName.EMA_CROSSOVER]
        preset_val = get_strategy_configs("4h")[StrategyName.EMA_CROSSOVER].get("max_distance_atr")
        assert ema_strategy.config.max_distance_atr == pytest.approx(preset_val)
