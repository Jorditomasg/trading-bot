"""Heikin-Ashi continuation strategy.

Heikin-Ashi (HA) candles smooth out price noise. We enter on N consecutive HA
candles in the same direction with no opposite wick (textbook "strong trend"
HA candle).

Entry signals:
- BUY  : N consecutive bullish HA candles, where each has no lower wick.
- SELL : N consecutive bearish HA candles, where each has no upper wick.

Strength: N more consecutive candles → stronger signal, capped at 1.0.
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
class HeikinAshiConfig:
    consecutive_candles: int   = 3      # how many HA candles in a row required
    atr_period:          int   = 14
    stop_atr_mult:       float = 1.5
    tp_atr_mult:         float = 4.5
    long_only:           bool  = False


def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Convert OHLC to Heikin-Ashi candles."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    n        = len(df)
    ha_open  = np.zeros(n)
    ha_open[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_open_s  = pd.Series(ha_open, index=df.index)
    ha_high    = pd.concat([df["high"], ha_open_s, ha_close], axis=1).max(axis=1)
    ha_low     = pd.concat([df["low"],  ha_open_s, ha_close], axis=1).min(axis=1)

    return pd.DataFrame({
        "ha_open":  ha_open_s,
        "ha_close": ha_close,
        "ha_high":  ha_high,
        "ha_low":   ha_low,
    })


class HeikinAshiStrategy(BaseStrategy):
    def __init__(self, config: HeikinAshiConfig = HeikinAshiConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "HEIKIN_ASHI"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        cfg = self.config
        required = cfg.consecutive_candles + cfg.atr_period + 4
        if len(df) < required:
            return hold_signal(atr=0.0)

        ha = _heikin_ashi(df)
        atr_s         = compute_atr(df, cfg.atr_period)
        current_atr   = float(atr_s.iloc[-1])
        current_price = float(df["close"].iloc[-1])

        if current_atr <= 0:
            return hold_signal(atr=0.0)

        n = cfg.consecutive_candles
        last_n = ha.tail(n)

        # Bullish: ha_close > ha_open AND ha_low == ha_open (no lower wick)
        is_bull = (last_n["ha_close"] > last_n["ha_open"]) & (
            np.isclose(last_n["ha_low"], last_n["ha_open"], rtol=1e-4)
        )
        # Bearish: ha_close < ha_open AND ha_high == ha_open (no upper wick)
        is_bear = (last_n["ha_close"] < last_n["ha_open"]) & (
            np.isclose(last_n["ha_high"], last_n["ha_open"], rtol=1e-4)
        )

        if is_bull.all():
            # Strength: scaled by total HA body size of last N candles, normalized by ATR
            body_total = float((last_n["ha_close"] - last_n["ha_open"]).sum())
            strength   = max(min(0.5 + body_total / (n * current_atr) * 0.5, 1.0), 0.5)
            sl, tp = calculate_levels("BUY", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        if is_bear.all() and not cfg.long_only:
            body_total = float((last_n["ha_open"] - last_n["ha_close"]).sum())
            strength   = max(min(0.5 + body_total / (n * current_atr) * 0.5, 1.0), 0.5)
            sl, tp = calculate_levels("SELL", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
