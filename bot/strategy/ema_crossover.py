import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)

STOP_ATR_MULT = 1.5
TP_ATR_MULT = 2.5


@dataclass
class EMACrossoverConfig:
    fast_period: int = 9
    slow_period: int = 21
    atr_period: int = 14


class EMACrossoverStrategy(BaseStrategy):
    def __init__(self, config: EMACrossoverConfig = EMACrossoverConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "EMA_CROSSOVER"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = self.config.slow_period + self.config.atr_period + 2
        if len(df) < required:
            logger.warning("EMACrossover: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        close = df["close"]
        fast = close.ewm(span=self.config.fast_period, adjust=False).mean()
        slow = close.ewm(span=self.config.slow_period, adjust=False).mean()
        atr = compute_atr(df, self.config.atr_period)

        current_atr = atr.iloc[-1]
        current_price = close.iloc[-1]

        # Crossover detection: compare current bar vs previous bar
        crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        crossed_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

        if crossed_up:
            distance = abs(fast.iloc[-1] - slow.iloc[-1])
            strength = min(distance / current_atr, 1.0) if current_atr > 0 else 0.5
            sl, tp = calculate_levels("BUY", current_price, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            signal = buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)
            logger.info("EMACrossover: BUY strength=%.2f price=%.2f", strength, current_price)
            return signal

        if crossed_down:
            distance = abs(fast.iloc[-1] - slow.iloc[-1])
            strength = min(distance / current_atr, 1.0) if current_atr > 0 else 0.5
            sl, tp = calculate_levels("SELL", current_price, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            signal = sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)
            logger.info("EMACrossover: SELL strength=%.2f price=%.2f", strength, current_price)
            return signal

        logger.debug("EMACrossover: HOLD fast=%.2f slow=%.2f", fast.iloc[-1], slow.iloc[-1])
        return hold_signal(atr=current_atr)
