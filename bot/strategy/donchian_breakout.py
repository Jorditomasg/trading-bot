"""Donchian channel breakout (classic Turtle Traders).

The Donchian channel is the high/low envelope over the last N bars. Entry on
breakout of the previous channel (always shifted by 1 to avoid look-ahead).

Entry signals:
- BUY  : close > N-bar high (excluding current bar).
- SELL : close < N-bar low  (excluding current bar).

Strength scales with how far past the channel the breakout extends, normalized
by ATR (clamped to [0.55, 1.0]).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.levels import calculate_levels
from bot.strategy.signal_factory import buy_signal, hold_signal, sell_signal

logger = logging.getLogger(__name__)


@dataclass
class DonchianConfig:
    channel_period: int   = 20
    atr_period:     int   = 14
    stop_atr_mult:  float = 2.0
    tp_atr_mult:    float = 5.0
    long_only:      bool  = False


class DonchianBreakoutStrategy(BaseStrategy):
    def __init__(self, config: DonchianConfig = DonchianConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "DONCHIAN_BREAKOUT"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        cfg = self.config
        required = cfg.channel_period + cfg.atr_period + 4
        if len(df) < required:
            return hold_signal(atr=0.0)

        # Shift by 1 so the current bar isn't part of its own channel
        upper = df["high"].rolling(cfg.channel_period).max().shift(1)
        lower = df["low"].rolling(cfg.channel_period).min().shift(1)

        atr_s         = compute_atr(df, cfg.atr_period)
        current_atr   = float(atr_s.iloc[-1])
        current_price = float(df["close"].iloc[-1])
        current_high  = float(df["high"].iloc[-1])
        current_low   = float(df["low"].iloc[-1])

        if current_atr <= 0 or pd.isna(upper.iloc[-1]) or pd.isna(lower.iloc[-1]):
            return hold_signal(atr=current_atr)

        broke_up   = current_high > upper.iloc[-1] and current_price > upper.iloc[-1]
        broke_down = current_low  < lower.iloc[-1] and current_price < lower.iloc[-1]

        if broke_up:
            penetration = (current_price - upper.iloc[-1]) / current_atr
            strength    = max(min(penetration * 0.5 + 0.55, 1.0), 0.55)
            sl, tp = calculate_levels("BUY", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        if broke_down and not cfg.long_only:
            penetration = (lower.iloc[-1] - current_price) / current_atr
            strength    = max(min(penetration * 0.5 + 0.55, 1.0), 0.55)
            sl, tp = calculate_levels("SELL", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
