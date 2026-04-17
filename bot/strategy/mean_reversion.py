import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr, rsi as compute_rsi
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal

logger = logging.getLogger(__name__)

STOP_ATR_MULT = 1.5


@dataclass
class MeanReversionConfig:
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    atr_period: int = 14


class MeanReversionStrategy(BaseStrategy):
    def __init__(self, config: MeanReversionConfig = MeanReversionConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "MEAN_REVERSION"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = max(self.config.bb_period, self.config.rsi_period + 1, self.config.atr_period) + 2
        if len(df) < required:
            logger.warning("MeanReversion: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        close = df["close"]
        sma = close.rolling(self.config.bb_period).mean()
        std = close.rolling(self.config.bb_period).std()
        upper_band = sma + self.config.bb_std * std
        lower_band = sma - self.config.bb_std * std

        rsi = compute_rsi(close, self.config.rsi_period)
        atr = compute_atr(df, self.config.atr_period)

        current_price = close.iloc[-1]
        current_rsi = rsi.iloc[-1]
        current_atr = atr.iloc[-1]
        current_lower = lower_band.iloc[-1]
        current_upper = upper_band.iloc[-1]
        current_mean = sma.iloc[-1]

        at_lower = current_price <= current_lower
        at_upper = current_price >= current_upper

        if at_lower and current_rsi < self.config.rsi_oversold:
            # Strength: how far below the band relative to its width
            band_width = current_upper - current_lower
            penetration = (current_lower - current_price) / band_width if band_width > 0 else 0
            strength = min(0.5 + penetration + (self.config.rsi_oversold - current_rsi) / 100, 1.0)
            signal = buy_signal(
                strength=strength,
                stop_loss=current_price - STOP_ATR_MULT * current_atr,
                take_profit=current_mean,
                atr=current_atr,
            )
            logger.info(
                "MeanReversion: BUY strength=%.2f price=%.2f rsi=%.1f lower=%.2f",
                strength, current_price, current_rsi, current_lower,
            )
            return signal

        if at_upper and current_rsi > self.config.rsi_overbought:
            band_width = current_upper - current_lower
            penetration = (current_price - current_upper) / band_width if band_width > 0 else 0
            strength = min(0.5 + penetration + (current_rsi - self.config.rsi_overbought) / 100, 1.0)
            signal = sell_signal(
                strength=strength,
                stop_loss=current_price + STOP_ATR_MULT * current_atr,
                take_profit=current_mean,
                atr=current_atr,
            )
            logger.info(
                "MeanReversion: SELL strength=%.2f price=%.2f rsi=%.1f upper=%.2f",
                strength, current_price, current_rsi, current_upper,
            )
            return signal

        logger.debug(
            "MeanReversion: HOLD price=%.2f rsi=%.1f [%.2f – %.2f]",
            current_price, current_rsi, current_lower, current_upper,
        )
        return hold_signal(atr=current_atr)
