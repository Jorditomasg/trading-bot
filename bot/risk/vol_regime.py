"""Volatility-regime filter — gates entries when realized vol is in the bottom
percentile of its trailing distribution.

Hypothesis (validated on 2024-05 → 2026-05 BTC 4h data):
    Entries placed when realized vol is in the bottom of its trailing distribution
    have lower win rate and PF. The strategy needs vol to recover before momentum
    plays out — TP=4.5 ATR rarely hits in low-vol regimes.

Math:
    realized_vol = std(log_returns over last vol_lookback bars) × sqrt(bars/year)
    reference    = rolling distribution over last percentile_window bars
    LOW_VOL  → current_vol below the percentile_threshold of reference
    NORMAL   → otherwise

Two response modes:
    block  → no new entries during LOW_VOL state (default)
    reduce → trades allowed but position size scaled by reduce_factor

Fail-open by design: when there's not enough history to compute the percentile
(early bars), the filter returns NORMAL — never silently blocks all signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class VolRegime(str, Enum):
    NORMAL  = "NORMAL"
    LOW_VOL = "LOW_VOL"


_BARS_PER_YEAR = {
    "1h":  24 * 365,
    "2h":  12 * 365,
    "4h":   6 * 365,
    "8h":   3 * 365,
    "1d":       365,
}


@dataclass
class VolRegimeConfig:
    enabled: bool = False
    timeframe: str = "4h"
    vol_lookback_bars: int = 50          # window for current realized vol
    percentile_window_bars: int = 180    # trailing reference distribution (~30d on 4h)
    percentile_threshold: float = 30.0   # below P30 of reference → LOW_VOL
    action: str = "block"                # block | reduce
    reduce_factor: float = 0.5           # if action=reduce, multiply size by this


class VolRegimeFilter:
    """Stateless filter — `get_state(df)` is pure given df and config."""

    def __init__(self, config: VolRegimeConfig = VolRegimeConfig()) -> None:
        self.config = config

    def _bars_per_year(self) -> float:
        return _BARS_PER_YEAR.get(self.config.timeframe, 6 * 365)

    def _realized_vol_pct(self, closes: np.ndarray, end_idx: int) -> float:
        """Annualized realized vol % using closes[end_idx-N : end_idx+1].

        Returns NaN if insufficient data.
        """
        N = self.config.vol_lookback_bars
        if end_idx < N:
            return float("nan")
        window = closes[end_idx - N : end_idx + 1]
        log_ret = np.diff(np.log(window))
        if len(log_ret) == 0:
            return 0.0
        return float(log_ret.std() * np.sqrt(self._bars_per_year()) * 100)

    def get_state(self, df: pd.DataFrame) -> VolRegime:
        """Classify the regime at the LAST bar of df.

        Fail-open: insufficient history → NORMAL (allow trade).
        """
        if not self.config.enabled:
            return VolRegime.NORMAL

        N = self.config.vol_lookback_bars
        M = self.config.percentile_window_bars
        # Need M bars of distribution + N bars to compute first vol point in window
        required = N + M
        if len(df) < required:
            return VolRegime.NORMAL

        closes = df["close"].values.astype(float)
        end = len(closes) - 1

        current_vol = self._realized_vol_pct(closes, end)
        if np.isnan(current_vol):
            return VolRegime.NORMAL

        # Build reference distribution: vol at each of the last M bars
        ref_vols = []
        for i in range(end - M, end):
            v = self._realized_vol_pct(closes, i)
            if not np.isnan(v):
                ref_vols.append(v)

        if len(ref_vols) < 30:
            return VolRegime.NORMAL

        threshold = float(np.percentile(ref_vols, self.config.percentile_threshold))

        if current_vol < threshold:
            logger.info(
                "VolRegimeFilter: LOW_VOL (current=%.2f%% < P%.0f=%.2f%%)",
                current_vol, self.config.percentile_threshold, threshold,
            )
            return VolRegime.LOW_VOL

        logger.debug(
            "VolRegimeFilter: NORMAL (current=%.2f%% >= P%.0f=%.2f%%)",
            current_vol, self.config.percentile_threshold, threshold,
        )
        return VolRegime.NORMAL

    def allows_signal(self, state: VolRegime) -> bool:
        """Whether a new entry is permitted under the given state."""
        if not self.config.enabled:
            return True
        if state == VolRegime.NORMAL:
            return True
        # LOW_VOL: only block when action=block
        return self.config.action != "block"

    def size_factor(self, state: VolRegime) -> float:
        """Multiplier to apply to position size — 1.0 unless reduce action active."""
        if not self.config.enabled:
            return 1.0
        if state == VolRegime.NORMAL:
            return 1.0
        if self.config.action == "reduce":
            return float(self.config.reduce_factor)
        return 1.0  # block returns 1.0 here; allows_signal is False so size never used
