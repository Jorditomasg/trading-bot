import logging
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


@dataclass
class RegimeDetectorConfig:
    atr_period: int = 14
    atr_volatile_lookback: int = 50
    atr_volatile_multiplier: float = 2.0
    adx_period: int = 14
    adx_trending_threshold: float = 25.0
    hurst_lookback: int = 100
    hurst_trending_threshold: float = 0.55
    hurst_ranging_threshold: float = 0.45


class RegimeDetector:
    def __init__(self, config: RegimeDetectorConfig = RegimeDetectorConfig()) -> None:
        self.config = config

    def detect(self, df: pd.DataFrame) -> MarketRegime:
        required = max(
            self.config.atr_period + self.config.atr_volatile_lookback,
            self.config.adx_period * 2,
            self.config.hurst_lookback,
        )
        if len(df) < required:
            logger.warning(
                "Not enough data (%d rows, need %d) — defaulting to RANGING", len(df), required
            )
            return MarketRegime.RANGING

        atr_series = self._atr(df, self.config.atr_period)
        current_atr = atr_series.iloc[-1]
        mean_atr = atr_series.iloc[-self.config.atr_volatile_lookback :].mean()

        if current_atr > self.config.atr_volatile_multiplier * mean_atr:
            logger.info(
                "Regime=VOLATILE (ATR %.4f > %.1fx mean %.4f)",
                current_atr, self.config.atr_volatile_multiplier, mean_atr,
            )
            return MarketRegime.VOLATILE

        adx = self._adx(df, self.config.adx_period)
        if adx >= self.config.adx_trending_threshold:
            logger.info("Regime=TRENDING (ADX=%.2f >= %.1f)", adx, self.config.adx_trending_threshold)
            return MarketRegime.TRENDING

        hurst = self._hurst_exponent(df["close"].values[-self.config.hurst_lookback :])
        logger.debug("Hurst exponent H=%.4f", hurst)

        if hurst > self.config.hurst_trending_threshold:
            logger.info("Regime=TRENDING (Hurst=%.4f > %.2f)", hurst, self.config.hurst_trending_threshold)
            return MarketRegime.TRENDING
        if hurst < self.config.hurst_ranging_threshold:
            logger.info("Regime=RANGING (Hurst=%.4f < %.2f)", hurst, self.config.hurst_ranging_threshold)
            return MarketRegime.RANGING

        logger.info("Regime=RANGING (default, Hurst=%.4f ADX=%.2f)", hurst, adx)
        return MarketRegime.RANGING

    def _atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        return tr.rolling(period).mean()

    def _adx(self, df: pd.DataFrame, period: int) -> float:
        high = df["high"]
        low = df["low"]
        prev_high = high.shift(1)
        prev_low = low.shift(1)

        plus_dm = (high - prev_high).clip(lower=0)
        minus_dm = (prev_low - low).clip(lower=0)
        # Zero out where the opposite move is larger
        plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
        minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)

        atr = self._atr(df, period)
        smoothed_plus = plus_dm.rolling(period).mean()
        smoothed_minus = minus_dm.rolling(period).mean()

        plus_di = 100 * smoothed_plus / atr
        minus_di = 100 * smoothed_minus / atr

        dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
        adx = dx.rolling(period).mean()
        return float(adx.iloc[-1])

    def _hurst_exponent(self, prices: np.ndarray) -> float:
        """Hurst exponent via Rescaled Range (R/S) analysis."""
        n = len(prices)
        if n < 20:
            return 0.5  # Indeterminate

        log_returns = np.diff(np.log(prices))
        lags = [4, 8, 16, 32, 64]
        lags = [l for l in lags if l < n // 2]
        if len(lags) < 3:
            return 0.5

        rs_values = []
        for lag in lags:
            chunks = [log_returns[i : i + lag] for i in range(0, len(log_returns) - lag + 1, lag)]
            rs_chunk = []
            for chunk in chunks:
                mean = chunk.mean()
                deviations = (chunk - mean).cumsum()
                r = deviations.max() - deviations.min()
                s = chunk.std(ddof=1)
                if s > 0:
                    rs_chunk.append(r / s)
            if rs_chunk:
                rs_values.append(np.mean(rs_chunk))
            else:
                rs_values.append(np.nan)

        valid = [(np.log(lag), np.log(rs)) for lag, rs in zip(lags, rs_values) if not np.isnan(rs)]
        if len(valid) < 2:
            return 0.5

        x = np.array([v[0] for v in valid])
        y = np.array([v[1] for v in valid])
        hurst = float(np.polyfit(x, y, 1)[0])
        return max(0.0, min(1.0, hurst))
