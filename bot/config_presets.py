"""Timeframe-aware configuration presets for regime detector and strategies.

Call get_regime_config(timeframe) and get_strategy_configs(timeframe) to obtain
calibrated parameter objects for the given candle interval.  Unknown timeframes
fall back to the "1h" defaults with a warning.
"""

import logging

from bot.constants import StrategyName
from bot.regime.detector import RegimeDetectorConfig
from bot.strategy.ema_crossover import EMACrossoverConfig

logger = logging.getLogger(__name__)

# ── Regime detector presets ───────────────────────────────────────────────────

_REGIME_PRESETS: dict[str, RegimeDetectorConfig] = {
    "1h": RegimeDetectorConfig(
        atr_period=14,
        atr_volatile_lookback=50,
        atr_volatile_multiplier=2.0,
        adx_period=14,
        adx_trending_threshold=25.0,
        hurst_lookback=100,
        hurst_trending_threshold=0.55,
        hurst_ranging_threshold=0.45,
    ),
    "2h": RegimeDetectorConfig(
        atr_period=14,
        atr_volatile_lookback=40,      # 40 × 2h ≈ 3.3 days
        atr_volatile_multiplier=2.3,
        adx_period=14,
        adx_trending_threshold=27.0,   # between 1h (25) and 4h (30)
        hurst_lookback=80,             # 80 × 2h ≈ 6.7 days
        hurst_trending_threshold=0.55,
        hurst_ranging_threshold=0.42,
    ),
    "4h": RegimeDetectorConfig(
        atr_period=14,
        atr_volatile_lookback=30,      # 30 × 4h = 5 days (same wall-clock as 50 × 1h)
        atr_volatile_multiplier=2.5,   # stricter volatile filter — only genuine explosions
        adx_period=14,
        adx_trending_threshold=30.0,   # require stronger trend (raised from 25)
        hurst_lookback=60,             # 60 × 4h = 10 days of R/S analysis
        hurst_trending_threshold=0.55,
        hurst_ranging_threshold=0.40,  # stricter ranging (raised from 0.45)
    ),
    "15m": RegimeDetectorConfig(
        atr_period=14,
        atr_volatile_lookback=200,   # 200 × 15 min ≈ 50 h  (same wall-clock as 1h default)
        atr_volatile_multiplier=2.5,  # noisier bars → raise threshold to avoid false VOLATILE
        adx_period=14,
        adx_trending_threshold=20.0,  # micro-trends form at lower ADX on 15m
        hurst_lookback=400,           # 400 × 15 min ≈ 100 h  (same wall-clock as 1h default)
        hurst_trending_threshold=0.55,
        hurst_ranging_threshold=0.45,
    ),
}

# ── Strategy config presets ───────────────────────────────────────────────────
# Each inner dict holds **kwargs to pass to the respective *Config dataclass.

_STRATEGY_PRESETS: dict[str, dict[StrategyName, dict]] = {
    "1h": {
        StrategyName.EMA_CROSSOVER: dict(
            fast_period=9,
            slow_period=21,
            atr_period=14,
            max_distance_atr=1.5,
            stop_atr_mult=1.5,
            tp_atr_mult=3.5,
            volume_period=20,
            volume_multiplier=1.3,
            min_atr_pct=0.0,
            require_bar_direction=False,
            require_ema_momentum=False,
            long_only=True,               # BTC upward bias: eliminates losing short trades
        ),
    },
    "4h": {
        StrategyName.EMA_CROSSOVER: dict(
            fast_period=9,
            slow_period=21,
            atr_period=14,
            max_distance_atr=1.0,          # 3yr optimal: captures momentum zone up to 1×ATR from EMA9
            stop_atr_mult=1.5,
            tp_atr_mult=4.5,               # 3yr optimal with dist=1.0: PF=1.551, Ann=22.5%, Sharpe=9.63
            volume_period=20,
            volume_multiplier=1.5,         # crossover needs 1.5× avg volume conviction
            min_atr_pct=0.005,             # skip if ATR < 0.5% of price (dead market)
            require_bar_direction=True,    # crossover bar must close in signal direction
            require_ema_momentum=True,     # continuation: EMA9 must be trending
            long_only=True,                # BTC has long-term upward bias; backtests prove long-only outperforms
        ),
    },
    "2h": {
        StrategyName.EMA_CROSSOVER: dict(
            fast_period=9,
            slow_period=21,
            atr_period=14,
            max_distance_atr=0.4,          # slightly looser than 4h (0.3) for 2h noise
            stop_atr_mult=1.5,
            tp_atr_mult=3.5,
            volume_period=20,
            volume_multiplier=1.3,         # 1.3× avg volume (less than 4h's 1.5)
            min_atr_pct=0.004,             # 0.4% min ATR
            require_bar_direction=True,
            require_ema_momentum=True,
        ),
    },
    "15m": {
        StrategyName.EMA_CROSSOVER: dict(
            fast_period=5,
            slow_period=13,
            atr_period=14,
            max_distance_atr=1.0,
            stop_atr_mult=1.5,
            tp_atr_mult=3.5,
        ),
    },
}

_FALLBACK = "1h"


def get_regime_config(timeframe: str) -> RegimeDetectorConfig:
    """Return a calibrated RegimeDetectorConfig for *timeframe*.

    Falls back to the "1h" preset for unrecognised values.
    """
    if timeframe not in _REGIME_PRESETS:
        logger.warning(
            "config_presets: unrecognised timeframe '%s' — fallback to '1h' defaults",
            timeframe,
        )
        return _REGIME_PRESETS[_FALLBACK]
    return _REGIME_PRESETS[timeframe]


def get_strategy_configs(timeframe: str) -> dict[StrategyName, dict]:
    """Return a dict of strategy kwargs dicts for *timeframe*.

    Keys are StrategyName enum values.  Falls back to "1h" for unrecognised values.
    """
    if timeframe not in _STRATEGY_PRESETS:
        logger.warning(
            "config_presets: unrecognised timeframe '%s' — fallback to '1h' strategy configs",
            timeframe,
        )
        return _STRATEGY_PRESETS[_FALLBACK]
    return _STRATEGY_PRESETS[timeframe]
