"""Supertrend strategy.

Supertrend is an ATR-based trailing-band trend indicator. The bands are placed
at ±multiplier × ATR from the median price. Direction flips when price closes
through the opposite band.

Entry signals:
- BUY  : direction flips from down to up (close > previous lower band crossover).
- SELL : direction flips from up to down.

This strategy enters on direction flips only — once per trend. Strength is fixed
at 0.7 because the supertrend flip is itself the conviction signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.levels import calculate_levels
from bot.strategy.signal_factory import buy_signal, hold_signal, sell_signal

logger = logging.getLogger(__name__)


@dataclass
class SupertrendConfig:
    atr_period:    int   = 10
    multiplier:    float = 3.0
    stop_atr_mult: float = 1.5
    tp_atr_mult:   float = 4.5
    long_only:     bool  = False


def _compute_supertrend(df: pd.DataFrame, period: int, mult: float) -> tuple[pd.Series, pd.Series]:
    """Return (supertrend, direction) where direction is +1 (up) or -1 (down).

    Standard Supertrend: bands ratchet (upper only descends, lower only ascends)
    while the trend holds. Direction flips when close crosses the active band.
    """
    hl2     = (df["high"] + df["low"]) / 2.0
    atr_s   = compute_atr(df, period)
    upper_b = hl2 + mult * atr_s
    lower_b = hl2 - mult * atr_s

    n          = len(df)
    final_up   = np.full(n, np.nan)
    final_low  = np.full(n, np.nan)
    direction  = np.zeros(n, dtype=int)
    supertrend = np.full(n, np.nan)
    close_arr  = df["close"].to_numpy(dtype=float, copy=True)
    upper_arr  = upper_b.to_numpy(dtype=float, copy=True)
    lower_arr  = lower_b.to_numpy(dtype=float, copy=True)

    # Find first bar with valid bands
    first_valid = -1
    for i in range(n):
        if not np.isnan(upper_arr[i]) and not np.isnan(lower_arr[i]):
            first_valid = i
            break
    if first_valid < 0:
        return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)

    final_up[first_valid]  = upper_arr[first_valid]
    final_low[first_valid] = lower_arr[first_valid]
    # Initial direction: use close position relative to mid
    direction[first_valid] = 1 if close_arr[first_valid] > (upper_arr[first_valid] + lower_arr[first_valid]) / 2 else -1

    for i in range(first_valid + 1, n):
        # Ratchet bands
        if upper_arr[i] < final_up[i - 1] or close_arr[i - 1] > final_up[i - 1]:
            final_up[i] = upper_arr[i]
        else:
            final_up[i] = final_up[i - 1]

        if lower_arr[i] > final_low[i - 1] or close_arr[i - 1] < final_low[i - 1]:
            final_low[i] = lower_arr[i]
        else:
            final_low[i] = final_low[i - 1]

        # Direction: flip when close crosses the active band
        prev_dir = direction[i - 1]
        if prev_dir == 1:
            direction[i] = -1 if close_arr[i] < final_low[i] else 1
        else:
            direction[i] =  1 if close_arr[i] > final_up[i] else -1

        supertrend[i] = final_low[i] if direction[i] == 1 else final_up[i]

    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)


class SupertrendStrategy(BaseStrategy):
    def __init__(self, config: SupertrendConfig = SupertrendConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "SUPERTREND"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        cfg = self.config
        required = cfg.atr_period * 2 + 4
        if len(df) < required:
            return hold_signal(atr=0.0)

        _st, direction = _compute_supertrend(df, cfg.atr_period, cfg.multiplier)
        atr_s          = compute_atr(df, cfg.atr_period)
        current_atr    = float(atr_s.iloc[-1])
        current_price  = float(df["close"].iloc[-1])

        if current_atr <= 0:
            return hold_signal(atr=0.0)

        flipped_up   = direction.iloc[-2] == -1 and direction.iloc[-1] == 1
        flipped_down = direction.iloc[-2] == 1  and direction.iloc[-1] == -1

        if flipped_up:
            sl, tp = calculate_levels("BUY", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return buy_signal(strength=0.75, stop_loss=sl, take_profit=tp, atr=current_atr)

        if flipped_down and not cfg.long_only:
            sl, tp = calculate_levels("SELL", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return sell_signal(strength=0.75, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
