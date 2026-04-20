import pandas as pd
import pytest

from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "open":   closes,
        "high":   [c * 1.01 for c in closes],
        "low":    [c * 0.99 for c in closes],
        "close":  closes,
        "volume": [1000.0] * len(closes),
    })


def _crossover_up(n: int = 100) -> pd.DataFrame:
    """Flat then single jump → upward EMA crossover on last bar."""
    return _make_df([100.0] * (n - 1) + [115.0])


def _crossover_down(n: int = 100) -> pd.DataFrame:
    """Flat then single drop → downward EMA crossover on last bar."""
    return _make_df([100.0] * (n - 1) + [85.0])


def _uptrend(n: int = 60) -> pd.DataFrame:
    """Steadily rising prices — EMA9 tracks above EMA21, price near fast EMA."""
    return _make_df([100.0 + i for i in range(n)])


def _downtrend_with_bounce() -> pd.DataFrame:
    """100 flat + 30-bar downtrend + 1 bar that bounces just below the fast EMA."""
    flat   = [100.0] * 100
    trend  = [100.0 - i for i in range(1, 31)]
    bounce = [74.0]
    return _make_df(flat + trend + bounce)


def _uptrend_spike_down_no_cross() -> pd.DataFrame:
    """100 flat + 30-bar uptrend + spike down (fast stays above slow → no cross, overextended → HOLD)."""
    flat  = [100.0] * 100
    trend = [100.0 + i for i in range(1, 31)]
    spike = [90.0]
    return _make_df(flat + trend + spike)


def _uptrend_with_shallow_pullback() -> pd.DataFrame:
    """100 flat + 30-bar uptrend + 1 bar pulling back within max_distance_atr → BUY."""
    flat     = [100.0] * 100
    trend    = [100.0 + i for i in range(1, 31)]
    pullback = [125.0]
    return _make_df(flat + trend + pullback)


# ── crossover signals ─────────────────────────────────────────────────────────

class TestCrossover:
    def test_buy_on_crossover_up(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_crossover_up()).action == "BUY"

    def test_sell_on_crossover_down(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_crossover_down()).action == "SELL"

    def test_crossover_strength_at_least_min(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_crossover_up()).strength >= 0.6


# ── trend continuation ────────────────────────────────────────────────────────

class TestTrendContinuation:
    def test_buy_during_uptrend(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_uptrend()).action == "BUY"

    def test_sell_during_downtrend_bounce(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_downtrend_with_bounce()).action == "SELL"

    def test_in_trend_strength_within_bounds(self):
        s = EMACrossoverStrategy()
        signal = s.generate_signal(_uptrend())
        assert 0.4 <= signal.strength <= 0.8

    def test_hold_when_overextended_above_fast_ema(self):
        df = _uptrend()
        df.loc[df.index[-1], "close"] = 300.0
        df.loc[df.index[-1], "high"]  = 303.0
        df.loc[df.index[-1], "low"]   = 297.0
        s = EMACrossoverStrategy()
        assert s.generate_signal(df).action == "HOLD"

    def test_hold_when_overextended_below_fast_ema(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_uptrend_spike_down_no_cross()).action == "HOLD"

    def test_buy_on_shallow_pullback_below_fast_ema(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_uptrend_with_shallow_pullback()).action == "BUY"


# ── edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_hold_on_insufficient_data(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_make_df([100.0] * 5)).action == "HOLD"

    def test_custom_max_distance_atr_tighter(self):
        df = _uptrend()
        default_signal = EMACrossoverStrategy().generate_signal(df)
        tight_signal   = EMACrossoverStrategy(EMACrossoverConfig(max_distance_atr=0.01)).generate_signal(df)
        assert default_signal.action == "BUY"
        assert tight_signal.action   == "HOLD"
