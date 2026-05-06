"""Tests for drawdown-aware risk scaler."""

from __future__ import annotations

import pytest

from bot.risk.drawdown_scaler import DrawdownRiskConfig, drawdown_multiplier


class TestConfigValidation:
    def test_default_is_disabled(self):
        cfg = DrawdownRiskConfig()
        assert cfg.enabled is False

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError, match="must have the same length"):
            DrawdownRiskConfig(thresholds=[0.05, 0.10], multipliers=[0.5])

    def test_threshold_out_of_range_raises(self):
        with pytest.raises(ValueError, match="must be in"):
            DrawdownRiskConfig(thresholds=[0.0], multipliers=[0.5])
        with pytest.raises(ValueError, match="must be in"):
            DrawdownRiskConfig(thresholds=[1.5], multipliers=[0.5])

    def test_thresholds_must_be_strictly_increasing(self):
        with pytest.raises(ValueError, match="strictly increasing"):
            DrawdownRiskConfig(thresholds=[0.10, 0.05], multipliers=[0.5, 0.25])
        with pytest.raises(ValueError, match="strictly increasing"):
            DrawdownRiskConfig(thresholds=[0.05, 0.05], multipliers=[0.5, 0.25])

    def test_multiplier_out_of_range_raises(self):
        with pytest.raises(ValueError, match="must be in"):
            DrawdownRiskConfig(thresholds=[0.05], multipliers=[0.0])
        with pytest.raises(ValueError, match="must be in"):
            DrawdownRiskConfig(thresholds=[0.05], multipliers=[1.5])


class TestDisabled:
    def test_disabled_returns_one_at_any_dd(self):
        cfg = DrawdownRiskConfig(enabled=False, thresholds=[0.05], multipliers=[0.5])
        assert drawdown_multiplier(8000.0, 10000.0, cfg) == 1.0  # 20% DD


class TestColdStart:
    def test_zero_peak_returns_one(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        assert drawdown_multiplier(10000.0, 0.0, cfg) == 1.0

    def test_negative_peak_returns_one(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        assert drawdown_multiplier(10000.0, -1.0, cfg) == 1.0


class TestAtOrAbovePeak:
    def test_at_peak_returns_one(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        assert drawdown_multiplier(10000.0, 10000.0, cfg) == 1.0

    def test_above_peak_returns_one(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        # current > peak shouldn't really happen (peak should track), but test fail-safe
        assert drawdown_multiplier(11000.0, 10000.0, cfg) == 1.0


class TestSingleTier:
    def test_below_threshold_full_risk(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        # 3% DD < 5% threshold
        assert drawdown_multiplier(9700.0, 10000.0, cfg) == 1.0

    def test_at_threshold_engages(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        # exactly 5% DD
        assert drawdown_multiplier(9500.0, 10000.0, cfg) == 0.5

    def test_above_threshold_engages(self):
        cfg = DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])
        assert drawdown_multiplier(9000.0, 10000.0, cfg) == 0.5  # 10% DD


class TestTwoTiers:
    def setup_method(self):
        self.cfg = DrawdownRiskConfig(
            enabled=True,
            thresholds=[0.05, 0.10],
            multipliers=[0.5, 0.25],
        )

    def test_below_first(self):
        assert drawdown_multiplier(9800.0, 10000.0, self.cfg) == 1.0

    def test_at_first(self):
        assert drawdown_multiplier(9500.0, 10000.0, self.cfg) == 0.5

    def test_between_first_and_second(self):
        assert drawdown_multiplier(9200.0, 10000.0, self.cfg) == 0.5  # 8% DD

    def test_at_second(self):
        assert drawdown_multiplier(9000.0, 10000.0, self.cfg) == 0.25  # 10% DD

    def test_above_second(self):
        assert drawdown_multiplier(8000.0, 10000.0, self.cfg) == 0.25  # 20% DD


class TestThreeTiers:
    def test_progressive_reduction(self):
        cfg = DrawdownRiskConfig(
            enabled=True,
            thresholds=[0.03, 0.07, 0.12],
            multipliers=[0.75, 0.50, 0.25],
        )
        assert drawdown_multiplier(9800.0, 10000.0, cfg) == 1.0    # 2% DD
        assert drawdown_multiplier(9650.0, 10000.0, cfg) == 0.75   # 3.5% DD
        assert drawdown_multiplier(9200.0, 10000.0, cfg) == 0.50   # 8% DD
        assert drawdown_multiplier(8500.0, 10000.0, cfg) == 0.25   # 15% DD
