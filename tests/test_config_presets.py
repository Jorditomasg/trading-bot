"""Tests for bot/config_presets.py — verifies 1h and 4h preset values.

Run with:
    python -m pytest tests/test_config_presets.py -v
"""

from bot.config_presets import get_strategy_configs
from bot.constants import StrategyName


class TestEMAPreset1h:
    """Verify 1h EMA Crossover preset values."""

    def setup_method(self):
        configs = get_strategy_configs("1h")
        self.cfg = configs[StrategyName.EMA_CROSSOVER]

    def test_max_distance_atr(self):
        assert self.cfg["max_distance_atr"] == 1.5

    def test_volume_multiplier(self):
        assert self.cfg["volume_multiplier"] == 1.3

    def test_require_bar_direction(self):
        assert self.cfg["require_bar_direction"] is False

    def test_require_ema_momentum(self):
        assert self.cfg["require_ema_momentum"] is False

    def test_min_atr_pct(self):
        assert self.cfg["min_atr_pct"] == 0.0

    def test_long_only(self):
        assert self.cfg["long_only"] is True


class TestEMAPreset4hRegression:
    """Ensure the 4h EMA Crossover preset was NOT touched."""

    def setup_method(self):
        configs = get_strategy_configs("4h")
        self.cfg = configs[StrategyName.EMA_CROSSOVER]

    def test_max_distance_atr_unchanged(self):
        assert self.cfg["max_distance_atr"] == 1.0  # 3yr optimal (updated from 0.3)

    def test_volume_multiplier_unchanged(self):
        assert self.cfg["volume_multiplier"] == 1.5

    def test_min_atr_pct_unchanged(self):
        assert self.cfg["min_atr_pct"] == 0.005

    def test_require_bar_direction_unchanged(self):
        assert self.cfg["require_bar_direction"] is True

    def test_require_ema_momentum_unchanged(self):
        assert self.cfg["require_ema_momentum"] is True

    def test_stop_atr_mult_unchanged(self):
        assert self.cfg["stop_atr_mult"] == 1.5

    def test_tp_atr_mult_unchanged(self):
        assert self.cfg["tp_atr_mult"] == 4.5  # 3yr optimal (updated from 3.5)
