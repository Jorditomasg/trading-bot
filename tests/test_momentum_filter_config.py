"""T05 — Failing tests for MomentumFilterConfig dataclass.

TDD: these tests must FAIL before T06 implementation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from bot.momentum.filter import MomentumFilter, MomentumFilterConfig, MomentumState


def _weekly(closes: list) -> pd.DataFrame:
    return pd.DataFrame({"close": closes})


class TestMomentumFilterConfig:
    def test_default_neutral_band_is_0_08(self) -> None:
        """MomentumFilterConfig() must default neutral_band to 0.08."""
        cfg = MomentumFilterConfig()
        assert cfg.neutral_band == 0.08

    def test_custom_neutral_band(self) -> None:
        """MomentumFilterConfig(neutral_band=0.05) must store 0.05."""
        cfg = MomentumFilterConfig(neutral_band=0.05)
        assert cfg.neutral_band == 0.05

    def test_neutral_band_config_param(self) -> None:
        """get_state with MomentumFilterConfig(neutral_band=0.05) uses 0.05 band.

        SMA = 100.0; price = 107 is within 7% of SMA.
        With band=0.05: 107 > 100*1.05=105 → BULLISH
        With band=0.08: 107 < 100*1.08=108 → NEUTRAL  (old behaviour)
        """
        df = _weekly([100.0] * 21)

        # Using explicit 0.05 config — price 107 is above the 5% upper band
        state_05 = MomentumFilter.get_state(df, 107.0, config=MomentumFilterConfig(neutral_band=0.05))
        assert state_05 == MomentumState.BULLISH, (
            f"With 0.05 band, price=107 should be BULLISH (above 105), got {state_05}"
        )

        # Using default 0.08 config — price 107 is within the 8% band
        state_08 = MomentumFilter.get_state(df, 107.0, config=MomentumFilterConfig(neutral_band=0.08))
        assert state_08 == MomentumState.NEUTRAL, (
            f"With 0.08 band, price=107 should be NEUTRAL (within ±8%), got {state_08}"
        )

    def test_old_positional_signature_still_works(self) -> None:
        """Old callers with only (df_weekly, current_price) must still work (no config kwarg)."""
        df = _weekly([100.0] * 21)
        # Must not raise — backward compatible
        state = MomentumFilter.get_state(df, 109.0)
        assert state == MomentumState.BULLISH
