from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

SMA_PERIOD   = 20
NEUTRAL_BAND = 0.08   # ±8% around SMA — OOS-validated band (research/round4)


class MomentumState(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"


@dataclass(frozen=True)
class MomentumFilterConfig:
    """Configuration for the momentum filter.

    Promoted from module constant (NEUTRAL_BAND) to dataclass in Phase 1
    (win-rate-uplift-2026-05) so audit configs can pin different band values
    (C1/C2 use 0.05; live bot uses 0.08).
    """
    sma_period:   int   = SMA_PERIOD
    neutral_band: float = NEUTRAL_BAND  # 0.08 = live default (OOS-validated)


class MomentumFilter:
    @staticmethod
    def get_state(
        df_weekly: pd.DataFrame | None,
        current_price: float,
        config: MomentumFilterConfig = MomentumFilterConfig(),
    ) -> MomentumState:
        """Return momentum state based on price vs weekly SMA.

        Fail-open: returns BULLISH when data is missing or insufficient.
        This matches BacktestEngine behaviour and ensures a fetch failure never blocks trading.

        Args:
            df_weekly: weekly OHLCV DataFrame (or None for fail-open).
            current_price: current asset price.
            config: optional MomentumFilterConfig; defaults to live config (0.08 band).
                    Audit configs may pass MomentumFilterConfig(neutral_band=0.05)
                    to reproduce spec-locked C1/C2 results.
        """
        if df_weekly is None or len(df_weekly) < config.sma_period + 1:
            return MomentumState.BULLISH
        sma = float(df_weekly["close"].iloc[-config.sma_period:].mean())
        if current_price > sma * (1.0 + config.neutral_band):
            return MomentumState.BULLISH
        if current_price < sma * (1.0 - config.neutral_band):
            return MomentumState.BEARISH
        return MomentumState.NEUTRAL
