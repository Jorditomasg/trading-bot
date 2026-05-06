"""Tests for VolRegimeFilter."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from bot.risk.vol_regime import VolRegime, VolRegimeConfig, VolRegimeFilter


def _df_constant(n: int, price: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open":   [price] * n,
        "high":   [price * 1.0001] * n,
        "low":    [price * 0.9999] * n,
        "close":  [price] * n,
        "volume": [1.0] * n,
    })


def _df_low_vol(n: int = 250, base: float = 100.0, eps: float = 0.0005) -> pd.DataFrame:
    """Closes drift slowly — very low realized vol."""
    rng = np.random.default_rng(42)
    rets = rng.normal(0, eps, n)
    closes = base * np.exp(np.cumsum(rets))
    return pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.001,
        "low":    closes * 0.999,
        "close":  closes,
        "volume": np.ones(n),
    })


def _df_high_vol_after_low(n_low: int = 200, n_high: int = 60, base: float = 100.0) -> pd.DataFrame:
    """First n_low bars: low vol. Last n_high bars: high vol."""
    rng = np.random.default_rng(7)
    low_rets  = rng.normal(0, 0.0005, n_low)
    high_rets = rng.normal(0, 0.02,   n_high)
    rets = np.concatenate([low_rets, high_rets])
    closes = base * np.exp(np.cumsum(rets))
    return pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.005,
        "low":    closes * 0.995,
        "close":  closes,
        "volume": np.ones(len(rets)),
    })


def _df_low_vol_after_high(n_high: int = 200, n_low: int = 60, base: float = 100.0) -> pd.DataFrame:
    """First n_high bars: high vol. Last n_low bars: very low vol."""
    rng = np.random.default_rng(11)
    high_rets = rng.normal(0, 0.02,    n_high)
    low_rets  = rng.normal(0, 0.0005,  n_low)
    rets = np.concatenate([high_rets, low_rets])
    closes = base * np.exp(np.cumsum(rets))
    return pd.DataFrame({
        "open":   closes,
        "high":   closes * 1.005,
        "low":    closes * 0.995,
        "close":  closes,
        "volume": np.ones(len(rets)),
    })


class TestDisabled:
    def test_disabled_always_normal(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=False))
        assert f.get_state(_df_low_vol(300)) == VolRegime.NORMAL

    def test_disabled_always_allows(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=False))
        assert f.allows_signal(VolRegime.LOW_VOL) is True

    def test_disabled_size_factor_one(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=False, action="reduce", reduce_factor=0.5))
        assert f.size_factor(VolRegime.LOW_VOL) == 1.0


class TestFailOpen:
    def test_insufficient_data_returns_normal(self):
        cfg = VolRegimeConfig(enabled=True, vol_lookback_bars=50, percentile_window_bars=180)
        f = VolRegimeFilter(cfg)
        df = _df_constant(50)  # not enough for 50 + 180 = 230 required
        assert f.get_state(df) == VolRegime.NORMAL

    def test_constant_prices_returns_normal(self):
        # Zero vol everywhere → percentile threshold equals 0 → current vol not strictly less
        cfg = VolRegimeConfig(enabled=True, vol_lookback_bars=50, percentile_window_bars=180)
        f = VolRegimeFilter(cfg)
        df = _df_constant(300)
        # Current vol is 0, threshold is 0, current < threshold is False → NORMAL
        assert f.get_state(df) == VolRegime.NORMAL


class TestRegimeDetection:
    def test_low_vol_after_high_vol_detected(self):
        cfg = VolRegimeConfig(
            enabled=True,
            timeframe="4h",
            vol_lookback_bars=50,
            percentile_window_bars=180,
            percentile_threshold=30.0,
        )
        f = VolRegimeFilter(cfg)
        df = _df_low_vol_after_high(n_high=200, n_low=60)
        # Last bar is in low-vol regime, reference distribution dominated by high-vol → LOW_VOL
        assert f.get_state(df) == VolRegime.LOW_VOL

    def test_high_vol_after_low_vol_returns_normal(self):
        cfg = VolRegimeConfig(
            enabled=True,
            timeframe="4h",
            vol_lookback_bars=50,
            percentile_window_bars=180,
            percentile_threshold=30.0,
        )
        f = VolRegimeFilter(cfg)
        df = _df_high_vol_after_low(n_low=200, n_high=60)
        # Last bar is high vol relative to mostly-low-vol reference → NORMAL
        assert f.get_state(df) == VolRegime.NORMAL


class TestActions:
    def test_block_action_blocks_low_vol(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=True, action="block"))
        assert f.allows_signal(VolRegime.LOW_VOL) is False
        assert f.allows_signal(VolRegime.NORMAL) is True

    def test_reduce_action_allows_with_reduced_size(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=True, action="reduce", reduce_factor=0.5))
        assert f.allows_signal(VolRegime.LOW_VOL) is True
        assert f.size_factor(VolRegime.LOW_VOL) == 0.5
        assert f.size_factor(VolRegime.NORMAL) == 1.0

    def test_reduce_factor_can_be_any_value(self):
        f = VolRegimeFilter(VolRegimeConfig(enabled=True, action="reduce", reduce_factor=0.25))
        assert f.size_factor(VolRegime.LOW_VOL) == 0.25
