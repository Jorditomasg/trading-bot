import pandas as pd
import pytest

from bot.bias.filter import Bias, BiasFilter, BiasFilterConfig
from bot.strategy.base import Signal


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({
        "open":   closes,
        "high":   [c * 1.01 for c in closes],
        "low":    [c * 0.99 for c in closes],
        "close":  closes,
        "volume": [1000.0] * len(closes),
    })


def _rising(n: int = 30, start: float = 100.0) -> pd.DataFrame:
    """Steadily rising closes → EMA9 > EMA21 → BULLISH."""
    return _make_df([start + i for i in range(n)])


def _falling(n: int = 30, start: float = 129.0) -> pd.DataFrame:
    """Steadily falling closes → EMA9 < EMA21 → BEARISH."""
    return _make_df([start - i for i in range(n)])


def _flat(n: int = 30, price: float = 100.0) -> pd.DataFrame:
    """Flat closes → EMA9 == EMA21 → NEUTRAL."""
    return _make_df([price] * n)


def _signal(action: str, strength: float = 0.7) -> Signal:
    return Signal(action=action, strength=strength, stop_loss=0.0, take_profit=0.0, atr=1.0)


# ── import smoke ─────────────────────────────────────────────────────────────

def test_imports():
    assert BiasFilter is not None
    assert BiasFilterConfig is not None
    assert Bias.BULLISH == "BULLISH"
    assert Bias.BEARISH == "BEARISH"
    assert Bias.NEUTRAL == "NEUTRAL"


# ── get_bias ─────────────────────────────────────────────────────────────────

class TestGetBias:
    def test_bullish_on_rising_prices(self):
        f = BiasFilter()
        assert f.get_bias(_rising()) == Bias.BULLISH

    def test_bearish_on_falling_prices(self):
        f = BiasFilter()
        assert f.get_bias(_falling()) == Bias.BEARISH

    def test_neutral_on_flat_prices(self):
        f = BiasFilter()
        assert f.get_bias(_flat()) == Bias.NEUTRAL

    def test_neutral_when_df_is_none(self):
        f = BiasFilter()
        assert f.get_bias(None) == Bias.NEUTRAL

    def test_neutral_when_insufficient_bars(self):
        f = BiasFilter()
        # slow_period=21, need at least 22 bars
        df = _rising(n=15)
        assert f.get_bias(df) == Bias.NEUTRAL

    def test_disabled_returns_bullish_sentinel(self):
        # enabled=False bypasses all checks; returns BULLISH sentinel
        f = BiasFilter(BiasFilterConfig(enabled=False))
        assert f.get_bias(_falling()) == Bias.BULLISH


# ── allows_signal ─────────────────────────────────────────────────────────────

class TestAllowsSignal:
    def setup_method(self):
        self.f = BiasFilter()

    def test_bullish_allows_buy(self):
        assert self.f.allows_signal(_signal("BUY"), Bias.BULLISH) is True

    def test_bullish_blocks_sell(self):
        assert self.f.allows_signal(_signal("SELL"), Bias.BULLISH) is False

    def test_bearish_allows_sell(self):
        assert self.f.allows_signal(_signal("SELL"), Bias.BEARISH) is True

    def test_bearish_blocks_buy(self):
        assert self.f.allows_signal(_signal("BUY"), Bias.BEARISH) is False

    def test_neutral_allows_buy_with_passthrough(self):
        # neutral_passthrough=True (default): NEUTRAL imposes no directional gate
        assert self.f.allows_signal(_signal("BUY"), Bias.NEUTRAL) is True

    def test_neutral_allows_sell_with_passthrough(self):
        assert self.f.allows_signal(_signal("SELL"), Bias.NEUTRAL) is True

    def test_neutral_blocks_buy_when_passthrough_disabled(self):
        f = BiasFilter(BiasFilterConfig(neutral_passthrough=False))
        assert f.allows_signal(_signal("BUY"), Bias.NEUTRAL) is False

    def test_neutral_blocks_sell_when_passthrough_disabled(self):
        f = BiasFilter(BiasFilterConfig(neutral_passthrough=False))
        assert f.allows_signal(_signal("SELL"), Bias.NEUTRAL) is False

    def test_hold_always_passes_bullish(self):
        assert self.f.allows_signal(_signal("HOLD"), Bias.BULLISH) is True

    def test_hold_always_passes_bearish(self):
        assert self.f.allows_signal(_signal("HOLD"), Bias.BEARISH) is True

    def test_hold_always_passes_neutral(self):
        assert self.f.allows_signal(_signal("HOLD"), Bias.NEUTRAL) is True

    def test_disabled_allows_all_directions(self):
        f = BiasFilter(BiasFilterConfig(enabled=False))
        assert f.allows_signal(_signal("BUY"),  Bias.BEARISH) is True
        assert f.allows_signal(_signal("SELL"), Bias.BULLISH) is True
        assert f.allows_signal(_signal("BUY"),  Bias.NEUTRAL) is True


# ── block_on_data_failure ──────────────────────────────────────────────────────

class TestBlockOnDataFailure:
    """block_on_data_failure=True must block non-HOLD signals when df is None."""

    def test_blocks_buy_when_df_none(self):
        f = BiasFilter(BiasFilterConfig(block_on_data_failure=True))
        f.get_bias(None)  # sets _last_bias_was_data_failure=True
        assert f.allows_signal(_signal("BUY"), Bias.NEUTRAL) is False

    def test_blocks_sell_when_df_none(self):
        f = BiasFilter(BiasFilterConfig(block_on_data_failure=True))
        f.get_bias(None)
        assert f.allows_signal(_signal("SELL"), Bias.NEUTRAL) is False

    def test_hold_always_passes_even_on_failure(self):
        f = BiasFilter(BiasFilterConfig(block_on_data_failure=True))
        f.get_bias(None)
        assert f.allows_signal(_signal("HOLD"), Bias.NEUTRAL) is True

    def test_default_false_still_allows_on_none(self):
        """Default block_on_data_failure=False preserves existing fail-open behaviour."""
        f = BiasFilter(BiasFilterConfig(neutral_passthrough=True))
        f.get_bias(None)
        assert f.allows_signal(_signal("BUY"), Bias.NEUTRAL) is True

    def test_flag_resets_after_valid_data(self):
        """After a successful get_bias call, block_on_data_failure no longer triggers."""
        f = BiasFilter(BiasFilterConfig(block_on_data_failure=True))
        f.get_bias(None)                   # failure → flag set
        f.get_bias(_rising())              # success → flag cleared
        assert f.allows_signal(_signal("BUY"), Bias.BULLISH) is True

    def test_genuine_neutral_market_not_blocked(self):
        """A genuine NEUTRAL (low EMA gap, data available) must not trigger the block."""
        f = BiasFilter(BiasFilterConfig(block_on_data_failure=True, neutral_passthrough=True))
        bias = f.get_bias(_flat())          # NEUTRAL from real data, not a failure
        assert bias == Bias.NEUTRAL
        assert f._last_bias_was_data_failure is False
        assert f.allows_signal(_signal("BUY"), bias) is True
