import pandas as pd
import pytest

from bot.momentum.filter import MomentumFilter, MomentumState


def _weekly(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestMomentumFilterStates:
    def test_bullish_when_price_above_upper_band(self):
        # SMA of last 20 from 21 bars = 100.0; 109 > 100*1.08 = 108 → BULLISH
        df = _weekly([100.0] * 21)
        assert MomentumFilter.get_state(df, 109.0) == MomentumState.BULLISH

    def test_bearish_when_price_below_lower_band(self):
        # SMA = 100.0; 91 < 100*0.92 = 92 → BEARISH
        df = _weekly([100.0] * 21)
        assert MomentumFilter.get_state(df, 91.0) == MomentumState.BEARISH

    def test_neutral_when_price_within_band(self):
        # SMA = 100.0; 105 is within ±8%
        df = _weekly([100.0] * 21)
        assert MomentumFilter.get_state(df, 105.0) == MomentumState.NEUTRAL

    def test_failopen_returns_bullish_when_df_is_none(self):
        assert MomentumFilter.get_state(None, 50000.0) == MomentumState.BULLISH

    def test_failopen_returns_bullish_when_insufficient_bars(self):
        # 20 bars = SMA_PERIOD, but threshold requires SMA_PERIOD + 1 = 21
        df = _weekly([100.0] * 20)
        assert MomentumFilter.get_state(df, 100.0) == MomentumState.BULLISH

    def test_sma_uses_last_20_closes(self):
        # First bar is 200, rest are 100. SMA of last 20 = 100.
        # Price 109 > 100*1.08=108 → BULLISH (not affected by outlier)
        closes = [200.0] + [100.0] * 20
        df = _weekly(closes)
        assert MomentumFilter.get_state(df, 109.0) == MomentumState.BULLISH


class TestSignalMomentumColumn:
    def test_insert_signal_stores_momentum(self, tmp_path):
        from bot.database.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.insert_signal(
            symbol="BTCUSDT", strategy="EMA_CROSSOVER", regime="TRENDING",
            action="BUY", strength=0.8, momentum="NEUTRAL",
        )
        sigs = db.get_recent_signals(1)
        assert sigs[0]["momentum"] == "NEUTRAL"

    def test_insert_signal_momentum_defaults_to_none(self, tmp_path):
        from bot.database.db import Database
        db = Database(str(tmp_path / "test.db"))
        db.insert_signal(
            symbol="BTCUSDT", strategy="EMA_CROSSOVER", regime="TRENDING",
            action="BUY", strength=0.8,
        )
        sigs = db.get_recent_signals(1)
        assert sigs[0]["momentum"] is None
