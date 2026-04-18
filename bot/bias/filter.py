import logging
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from bot.strategy.base import Signal

logger = logging.getLogger(__name__)


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class BiasFilterConfig:
    fast_period: int = 9
    slow_period: int = 21
    neutral_threshold_pct: float = 0.001  # 0.1% of price
    enabled: bool = True


class BiasFilter:
    def __init__(self, config: BiasFilterConfig = BiasFilterConfig()) -> None:
        self.config = config

    def get_bias(self, df_4h: pd.DataFrame | None) -> Bias:
        if not self.config.enabled:
            return Bias.BULLISH  # sentinel: allows_signal will bypass filter entirely

        required = self.config.slow_period + 1
        if df_4h is None or len(df_4h) < required:
            logger.warning(
                "BiasFilter: insufficient 4h data (%s rows) — NEUTRAL (fail-closed)",
                len(df_4h) if df_4h is not None else "None",
            )
            return Bias.NEUTRAL

        close = df_4h["close"]
        ema_fast = close.ewm(span=self.config.fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=self.config.slow_period, adjust=False).mean()

        fast_val = ema_fast.iloc[-1]
        slow_val = ema_slow.iloc[-1]
        price    = close.iloc[-1]

        gap = abs(fast_val - slow_val) / price
        if gap < self.config.neutral_threshold_pct:
            logger.info("BiasFilter: EMA gap %.4f%% below threshold — NEUTRAL", gap * 100)
            return Bias.NEUTRAL

        if fast_val > slow_val:
            logger.info(
                "BiasFilter: BULLISH (EMA%d=%.2f > EMA%d=%.2f)",
                self.config.fast_period, fast_val, self.config.slow_period, slow_val,
            )
            return Bias.BULLISH

        logger.info(
            "BiasFilter: BEARISH (EMA%d=%.2f < EMA%d=%.2f)",
            self.config.fast_period, fast_val, self.config.slow_period, slow_val,
        )
        return Bias.BEARISH

    def allows_signal(self, signal: Signal, bias: Bias) -> bool:
        if not self.config.enabled:
            return True

        if signal.action == "HOLD":
            return True
        if signal.action == "BUY":
            return bias == Bias.BULLISH
        if signal.action == "SELL":
            return bias == Bias.BEARISH
        return False
