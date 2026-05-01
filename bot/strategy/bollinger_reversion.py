"""Bollinger Band mean-reversion strategy.

Classic mean-reversion: Bollinger Bands (20, 2σ) + RSI confirmation.

Entry signals:
- BUY  : close <= lower band AND RSI < oversold_level
- SELL : close >= upper band AND RSI > overbought_level

Take-profit at the SMA midline (typical for BB reversion). SL at ATR multiple
beyond the touched band.

Strength combines band penetration depth and RSI extremity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.indicators.utils import rsi as compute_rsi
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import buy_signal, hold_signal, sell_signal

logger = logging.getLogger(__name__)


@dataclass
class BollingerReversionConfig:
    bb_period:        int   = 20
    bb_std:           float = 2.0
    rsi_period:       int   = 14
    oversold_level:   float = 30.0
    overbought_level: float = 70.0
    atr_period:       int   = 14
    stop_atr_mult:    float = 1.5
    long_only:        bool  = False


class BollingerReversionStrategy(BaseStrategy):
    def __init__(self, config: BollingerReversionConfig = BollingerReversionConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "BOLLINGER_REVERSION"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        cfg = self.config
        required = max(cfg.bb_period, cfg.rsi_period) + cfg.atr_period + 4
        if len(df) < required:
            return hold_signal(atr=0.0)

        close         = df["close"]
        sma           = close.rolling(cfg.bb_period).mean()
        std           = close.rolling(cfg.bb_period).std()
        upper_band    = sma + cfg.bb_std * std
        lower_band    = sma - cfg.bb_std * std
        rsi_series    = compute_rsi(close, cfg.rsi_period)
        atr_s         = compute_atr(df, cfg.atr_period)

        current_close = float(close.iloc[-1])
        current_mid   = float(sma.iloc[-1])
        current_lower = float(lower_band.iloc[-1])
        current_upper = float(upper_band.iloc[-1])
        current_rsi   = float(rsi_series.iloc[-1])
        current_atr   = float(atr_s.iloc[-1])

        if current_atr <= 0 or pd.isna(current_mid):
            return hold_signal(atr=current_atr)

        band_width = current_upper - current_lower
        if band_width <= 0:
            return hold_signal(atr=current_atr)

        # BUY: oversold touch
        if current_close <= current_lower and current_rsi < cfg.oversold_level:
            penetration = (current_lower - current_close) / band_width
            rsi_extr    = (cfg.oversold_level - current_rsi) / cfg.oversold_level
            strength    = max(min(0.5 + penetration + 0.3 * rsi_extr, 1.0), 0.5)
            sl = current_close - cfg.stop_atr_mult * current_atr
            tp = current_mid
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        # SELL: overbought touch
        if current_close >= current_upper and current_rsi > cfg.overbought_level and not cfg.long_only:
            penetration = (current_close - current_upper) / band_width
            rsi_extr    = (current_rsi - cfg.overbought_level) / (100.0 - cfg.overbought_level)
            strength    = max(min(0.5 + penetration + 0.3 * rsi_extr, 1.0), 0.5)
            sl = current_close + cfg.stop_atr_mult * current_atr
            tp = current_mid
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
