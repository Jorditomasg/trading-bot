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
    """100 flat + 30-bar downtrend + 1 bar that bounces just below the fast EMA.

    After 30 bars of -1/bar decline: fast EMA ≈ 75, slow EMA ≈ 80.
    Bounce bar at 74 is 0.8 units below fast EMA (< 1.5 ATR) → valid SELL entry.
    No EMA crossover occurs because fast was already below slow before the bounce.
    """
    flat   = [100.0] * 100
    trend  = [100.0 - i for i in range(1, 31)]   # 99 → 70
    bounce = [74.0]                                # near fast EMA, within 1.5 ATR
    return _make_df(flat + trend + bounce)


def _uptrend_spike_down_no_cross() -> pd.DataFrame:
    """100 flat + 30-bar uptrend + spike that stays above the EMA crossover threshold.

    After the uptrend: fast EMA ≈ 126, slow EMA ≈ 120 (gap = 6).
    Spike to 90 moves fast EMA to ~119 and slow EMA to ~117 — fast stays above slow,
    so no EMA crossover occurs. But price is ~5 ATR from fast EMA → HOLD.

    Without the abs() fix: (90 - 119) / ATR = -5.4 < 1.5 → spurious BUY.
    With    the abs() fix: abs(90 - 119) / ATR = 5.4 > 1.5 → correctly HOLD.
    """
    flat  = [100.0] * 100
    trend = [100.0 + i for i in range(1, 31)]   # 101 → 130
    spike = [90.0]
    return _make_df(flat + trend + spike)


def _uptrend_with_shallow_pullback() -> pd.DataFrame:
    """100 flat + 30-bar uptrend + 1 bar pulling back to near fast EMA (within 1.5 ATR)."""
    flat     = [100.0] * 100
    trend    = [100.0 + i for i in range(1, 31)]   # 101 → 130
    pullback = [125.0]                              # just below fast EMA (~126)
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
        """In-trend SELL: price bounces back near fast EMA during a downtrend."""
        s = EMACrossoverStrategy()
        assert s.generate_signal(_downtrend_with_bounce()).action == "SELL"

    def test_in_trend_strength_within_bounds(self):
        s = EMACrossoverStrategy()
        signal = s.generate_signal(_uptrend())
        assert 0.4 <= signal.strength <= 0.8

    def test_hold_when_overextended_above_fast_ema(self):
        """Price far above fast EMA → overextended upside → HOLD."""
        df = _uptrend()
        df.loc[df.index[-1], "close"] = 300.0
        df.loc[df.index[-1], "high"]  = 303.0
        df.loc[df.index[-1], "low"]   = 297.0
        s = EMACrossoverStrategy()
        assert s.generate_signal(df).action == "HOLD"

    def test_hold_when_overextended_below_fast_ema(self):
        """Price >> 1.5 ATR below fast EMA while uptrend intact → HOLD.

        Regression for the abs() fix. Without it, a large negative distance
        (current_price - fast_ema) passes the '< max_distance_atr' check and
        generates a spurious BUY on deep downward spikes.
        """
        s = EMACrossoverStrategy()
        assert s.generate_signal(_uptrend_spike_down_no_cross()).action == "HOLD"

    def test_buy_on_shallow_pullback_below_fast_ema(self):
        """Price slightly below fast EMA (< 1.5 ATR) → valid pullback entry → BUY."""
        s = EMACrossoverStrategy()
        assert s.generate_signal(_uptrend_with_shallow_pullback()).action == "BUY"


# ── edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_hold_on_insufficient_data(self):
        s = EMACrossoverStrategy()
        assert s.generate_signal(_make_df([100.0] * 5)).action == "HOLD"

    def test_custom_max_distance_atr_tighter(self):
        """A tighter max_distance_atr rejects entries the default config would allow."""
        df = _uptrend()
        default_signal = EMACrossoverStrategy().generate_signal(df)
        tight_signal   = EMACrossoverStrategy(EMACrossoverConfig(max_distance_atr=0.01)).generate_signal(df)
        assert default_signal.action == "BUY"
        assert tight_signal.action   == "HOLD"
