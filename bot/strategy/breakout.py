import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)

STOP_ATR_MULT = 2.0
TP_ATR_MULT = 3.0


@dataclass
class BreakoutConfig:
    channel_period: int = 20
    volume_multiplier: float = 1.5
    atr_period: int = 14


class BreakoutStrategy(BaseStrategy):
    def __init__(self, config: BreakoutConfig = BreakoutConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "BREAKOUT"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = self.config.channel_period + self.config.atr_period + 2
        if len(df) < required:
            logger.warning("Breakout: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Donchian channel: use previous period to avoid look-ahead
        upper_channel = high.rolling(self.config.channel_period).max().shift(1)
        lower_channel = low.rolling(self.config.channel_period).min().shift(1)

        volume_ma = volume.rolling(self.config.channel_period).mean()
        atr = compute_atr(df, self.config.atr_period)

        current_close = close.iloc[-1]
        current_volume = volume.iloc[-1]
        avg_volume = volume_ma.iloc[-1]
        current_atr = atr.iloc[-1]
        current_upper = upper_channel.iloc[-1]
        current_lower = lower_channel.iloc[-1]

        volume_ok = current_volume > self.config.volume_multiplier * avg_volume

        if not volume_ok:
            logger.debug(
                "Breakout: volume filter failed vol=%.0f avg=%.0f ratio=%.2f",
                current_volume, avg_volume, current_volume / avg_volume if avg_volume > 0 else 0,
            )
            return hold_signal(atr=current_atr)

        if current_close > current_upper:
            vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
            strength = min((vol_ratio - self.config.volume_multiplier) / 2 + 0.5, 1.0)
            sl, tp = calculate_levels("BUY", current_close, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            signal = buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)
            logger.info(
                "Breakout: BUY strength=%.2f price=%.2f > upper=%.2f (vol ratio=%.2f)",
                strength, current_close, current_upper, vol_ratio,
            )
            return signal

        if current_close < current_lower:
            vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
            strength = min((vol_ratio - self.config.volume_multiplier) / 2 + 0.5, 1.0)
            sl, tp = calculate_levels("SELL", current_close, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            signal = sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)
            logger.info(
                "Breakout: SELL strength=%.2f price=%.2f < lower=%.2f (vol ratio=%.2f)",
                strength, current_close, current_lower, vol_ratio,
            )
            return signal

        logger.debug(
            "Breakout: HOLD price=%.2f [%.2f – %.2f]",
            current_close, current_lower, current_upper,
        )
        return hold_signal(atr=current_atr)
