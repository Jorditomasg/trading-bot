"""MACD crossover strategy.

MACD = EMA(close, fast) - EMA(close, slow)
Signal = EMA(MACD, signal_period)
Histogram = MACD - Signal

Entry signals:
- BUY  : MACD crosses above Signal AND Histogram is positive (zero-line confirm).
- SELL : MACD crosses below Signal AND Histogram is negative.

Strength: scaled by |histogram| / ATR (more momentum = stronger signal).
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
class MACDConfig:
    fast_period:   int   = 12
    slow_period:   int   = 26
    signal_period: int   = 9
    atr_period:    int   = 14
    stop_atr_mult: float = 1.5
    tp_atr_mult:   float = 4.5
    long_only:     bool  = False


class MACDStrategy(BaseStrategy):
    def __init__(self, config: MACDConfig = MACDConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "MACD"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        cfg = self.config
        required = cfg.slow_period + cfg.signal_period + cfg.atr_period + 4
        if len(df) < required:
            return hold_signal(atr=0.0)

        close = df["close"]
        ema_fast = close.ewm(span=cfg.fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=cfg.slow_period, adjust=False).mean()
        macd     = ema_fast - ema_slow
        signal   = macd.ewm(span=cfg.signal_period, adjust=False).mean()
        hist     = macd - signal

        atr_s         = compute_atr(df, cfg.atr_period)
        current_atr   = float(atr_s.iloc[-1])
        current_price = float(close.iloc[-1])

        if current_atr <= 0:
            return hold_signal(atr=0.0)

        crossed_up   = macd.iloc[-2] <= signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]
        crossed_down = macd.iloc[-2] >= signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]

        # Zero-line confirmation: histogram must have the right sign
        if crossed_up and hist.iloc[-1] > 0:
            strength = min(abs(hist.iloc[-1]) / current_atr * 2.0 + 0.5, 1.0)
            sl, tp = calculate_levels("BUY", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        if crossed_down and hist.iloc[-1] < 0 and not cfg.long_only:
            strength = min(abs(hist.iloc[-1]) / current_atr * 2.0 + 0.5, 1.0)
            sl, tp = calculate_levels("SELL", current_price, current_atr, cfg.stop_atr_mult, cfg.tp_atr_mult)
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
