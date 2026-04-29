from __future__ import annotations

from enum import Enum

import pandas as pd

SMA_PERIOD   = 20
NEUTRAL_BAND = 0.08   # ±8% around SMA — OOS-validated band (research/round4)


class MomentumState(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"


class MomentumFilter:
    @staticmethod
    def get_state(df_weekly: pd.DataFrame | None, current_price: float) -> MomentumState:
        """Return momentum state based on price vs weekly SMA.

        Fail-open: returns BULLISH when data is missing or insufficient (< 21 bars).
        This matches BacktestEngine behaviour and ensures a fetch failure never blocks trading.
        """
        if df_weekly is None or len(df_weekly) < SMA_PERIOD + 1:
            return MomentumState.BULLISH
        sma = float(df_weekly["close"].iloc[-SMA_PERIOD:].mean())
        if current_price > sma * (1.0 + NEUTRAL_BAND):
            return MomentumState.BULLISH
        if current_price < sma * (1.0 - NEUTRAL_BAND):
            return MomentumState.BEARISH
        return MomentumState.NEUTRAL
