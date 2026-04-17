import logging
from dataclasses import dataclass

import pandas as pd

from bot.strategy.base import BaseStrategy, Signal

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
            return Signal(action="HOLD", strength=0.0, stop_loss=0.0, take_profit=0.0, atr=0.0)

        close = df["close"]
        sma = close.rolling(self.config.bb_period).mean()
        std = close.rolling(self.config.bb_period).std()
        upper_band = sma + self.config.bb_std * std
        lower_band = sma - self.config.bb_std * std

        rsi = self._rsi(close, self.config.rsi_period)
        atr = self._atr(df, self.config.atr_period)

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
            signal = Signal(
                action="BUY",
                strength=strength,
                stop_loss=current_price - STOP_ATR_MULT * current_atr,
                take_profit=current_mean,  # revert to mean
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
            signal = Signal(
                action="SELL",
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
        return Signal(action="HOLD", strength=0.0, stop_loss=0.0, take_profit=0.0, atr=current_atr)

    @staticmethod
    def _rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        return tr.rolling(period).mean()
