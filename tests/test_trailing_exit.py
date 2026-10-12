"""Chandelier trailing exit (2026-10-12) — replaces the fixed 5×ATR take-profit.

Re-validated without look-ahead (gotcha #49): SL 2×ATR + trail 4×ATR from the
peak, no TP, roughly doubles Sharpe in-sample AND out-of-sample on both
BTC+ETH (2018→) and BTC+ETH+SOL (2020→). Report:
docs/audits/trailing_exit_2026-10-12.md.
"""
import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.levels import calculate_levels, trail_stop
from tests.conftest import uptrend


# ── Pure rule ────────────────────────────────────────────────────────────────

def test_trail_raises_long_stop_to_peak_minus_k_atr():
    assert trail_stop("BUY", current_stop=90.0, peak=120.0, atr=5.0, trail_mult=4.0) == pytest.approx(100.0)


def test_trail_never_lowers_a_long_stop():
    assert trail_stop("BUY", current_stop=110.0, peak=120.0, atr=5.0, trail_mult=4.0) == 110.0


def test_trail_disabled_keeps_stop():
    assert trail_stop("BUY", current_stop=90.0, peak=200.0, atr=5.0, trail_mult=0.0) == 90.0


def test_trail_ignores_invalid_atr():
    assert trail_stop("BUY", current_stop=90.0, peak=200.0, atr=float("nan"), trail_mult=4.0) == 90.0


def test_trail_short_side_mirrors():
    assert trail_stop("SELL", current_stop=130.0, peak=100.0, atr=5.0, trail_mult=4.0) == pytest.approx(120.0)
    assert trail_stop("SELL", current_stop=110.0, peak=100.0, atr=5.0, trail_mult=4.0) == 110.0


# ── No take-profit ───────────────────────────────────────────────────────────

def test_zero_tp_mult_means_no_take_profit_level():
    _, tp = calculate_levels("BUY", 100.0, 2.0, 2.0, 0.0)
    assert tp == 0.0


def test_ema_strategy_emits_zero_tp_when_disabled():
    strat = EMACrossoverStrategy(EMACrossoverConfig(tp_atr_mult=0.0, max_distance_atr=50.0))
    sig = strat.generate_signal(uptrend())
    assert sig.action == "BUY"
    assert sig.take_profit == 0.0
    assert sig.stop_loss > 0


def test_engine_does_not_exit_on_a_zero_take_profit():
    eng = BacktestEngine(BacktestConfig(timeframe="4h"))
    trade = {"side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0}
    bar = pd.Series({"high": 500.0, "low": 95.0, "close": 400.0})
    assert eng._check_exit(trade, bar) is None


def test_engine_still_stops_out_with_zero_take_profit():
    eng = BacktestEngine(BacktestConfig(timeframe="4h"))
    trade = {"side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0}
    bar = pd.Series({"high": 101.0, "low": 85.0, "close": 88.0})
    assert eng._check_exit(trade, bar) == ("STOP_LOSS", 90.0)


# ── Engine ratchet ───────────────────────────────────────────────────────────

def test_engine_ratchet_tracks_peak_and_raises_stop():
    eng = BacktestEngine(BacktestConfig(timeframe="4h", ema_trail_mult=4.0))
    trade = {"side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0, "peak_price": 100.0}
    eng._ratchet_trailing_stop(trade, bar_high=130.0, bar_low=120.0, atr=5.0)
    assert trade["peak_price"] == 130.0
    assert trade["stop_loss"] == pytest.approx(110.0)
    eng._ratchet_trailing_stop(trade, bar_high=125.0, bar_low=111.0, atr=5.0)   # lower high: no change
    assert trade["stop_loss"] == pytest.approx(110.0)


def test_engine_ratchet_is_noop_when_trailing_disabled():
    eng = BacktestEngine(BacktestConfig(timeframe="4h", ema_trail_mult=0.0))
    trade = {"side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0, "peak_price": 100.0}
    eng._ratchet_trailing_stop(trade, bar_high=300.0, bar_low=250.0, atr=5.0)
    assert trade["stop_loss"] == 90.0


def _trend_then_crash() -> pd.DataFrame:
    """Steady rally then a collapse: a trailed long must exit near the top, far above its initial stop."""
    up = [100.0 * (1.01 ** i) for i in range(260)]
    down = [up[-1] * (0.97 ** i) for i in range(1, 40)]
    c = up + down
    t = pd.date_range("2024-01-01", periods=len(c), freq="4h", tz="UTC")
    return pd.DataFrame({"open_time": t, "open": [x * 0.999 for x in c], "high": [x * 1.004 for x in c],
                         "low": [x * 0.994 for x in c], "close": c, "volume": [100.0] * len(c)})


def test_portfolio_engine_trailing_exits_in_profit_on_a_reversal():
    from bot.backtest.portfolio_engine import PortfolioBacktestEngine
    from unittest.mock import patch
    from bot.regime.detector import MarketRegime
    from bot.strategy.signal_factory import buy_signal, hold_signal

    df = _trend_then_crash()
    cfg = BacktestConfig(timeframe="4h", ema_tp_mult=0.0, ema_stop_mult=2.0, ema_trail_mult=4.0,
                         long_only=True, cost_per_side_pct=0.0)
    eng = PortfolioBacktestEngine(config=cfg)
    calls = {"n": 0}

    def one_buy(self, window, window_4h):
        calls["n"] += 1
        if calls["n"] == 1:
            px = float(window["close"].iloc[-1])
            return MarketRegime.TRENDING, buy_signal(strength=0.9, stop_loss=px * 0.95, take_profit=0.0, atr=px * 0.01)
        return MarketRegime.TRENDING, hold_signal(atr=0.0)

    with patch.object(BacktestEngine, "_generate_signal", one_buy):
        pr = eng.run_portfolio(dfs={"BTCUSDT": df}, dfs_4h={"BTCUSDT": None}, dfs_weekly={"BTCUSDT": None})
    trades = pr.per_symbol_trades["BTCUSDT"]
    first = trades[0]
    assert first["exit_reason"] == "STOP_LOSS"
    assert first["pnl"] > 0, "trailing stop should have locked in the rally"
    assert first["exit_price"] > first["entry_price"] * 3
