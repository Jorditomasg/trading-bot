import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)

@dataclass
class EMACrossoverConfig:
    fast_period:      int   = 9
    slow_period:      int   = 21
    atr_period:       int   = 14
    max_distance_atr: float = 1.5
    stop_atr_mult:    float = 1.5   # SL distance in ATR units
    tp_atr_mult:      float = 3.5   # TP distance in ATR units


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
        fast  = close.ewm(span=self.config.fast_period, adjust=False).mean()
        slow  = close.ewm(span=self.config.slow_period, adjust=False).mean()
        atr   = compute_atr(df, self.config.atr_period)

        current_atr   = atr.iloc[-1]
        current_price = float(close.iloc[-1])

        crossed_up   = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        crossed_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]

        dist_atr     = abs(current_price - float(fast.iloc[-1])) / current_atr if current_atr > 0 else float("inf")
        in_trend_buy  = float(fast.iloc[-1]) > float(slow.iloc[-1]) and dist_atr < self.config.max_distance_atr
        in_trend_sell = float(fast.iloc[-1]) < float(slow.iloc[-1]) and dist_atr < self.config.max_distance_atr

        action = "HOLD"
        if crossed_up or in_trend_buy:
            action = "BUY"
        elif crossed_down or in_trend_sell:
            action = "SELL"

        if action == "BUY":
            if crossed_up:
                fast_slope = fast.iloc[-1] - fast.iloc[-2]
                strength = max(min(abs(fast_slope) / current_atr * 5, 1.0), 0.6) if current_atr > 0 else 0.6
            else:
                strength = max(min(0.5 * (1 - dist_atr / self.config.max_distance_atr) + 0.4, 0.8), 0.4)
            sl, tp = calculate_levels("BUY", current_price, current_atr, self.config.stop_atr_mult, self.config.tp_atr_mult)
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        if action == "SELL":
            if crossed_down:
                fast_slope = fast.iloc[-1] - fast.iloc[-2]
                strength = max(min(abs(fast_slope) / current_atr * 5, 1.0), 0.6) if current_atr > 0 else 0.6
            else:
                strength = max(min(0.5 * (1 - dist_atr / self.config.max_distance_atr) + 0.4, 0.8), 0.4)
            sl, tp = calculate_levels("SELL", current_price, current_atr, self.config.stop_atr_mult, self.config.tp_atr_mult)
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
