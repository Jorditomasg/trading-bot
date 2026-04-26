import pytest
from bot.risk.kelly import compute_kelly_fraction, kelly_risk_fraction


class TestComputeKellyFraction:
    def test_positive_edge_half_kelly(self):
        # b = 0.02/0.01 = 2.0, q = 0.45
        # f* = 0.55 - 0.45/2 = 0.325 → half = 0.1625
        result = compute_kelly_fraction(0.55, 0.02, 0.01, half=True)
        assert abs(result - 0.1625) < 1e-6

    def test_positive_edge_full_kelly(self):
        result = compute_kelly_fraction(0.55, 0.02, 0.01, half=False)
        assert abs(result - 0.325) < 1e-6

    def test_no_edge_returns_zero(self):
        # win_rate=0.33, b=1.0 → f* = 0.33 - 0.67 = -0.34 → floored 0
        result = compute_kelly_fraction(0.33, 0.01, 0.01, half=True)
        assert result == 0.0

    def test_breakeven_returns_zero(self):
        # win_rate=0.5, b=1.0 → f* = 0.5 - 0.5 = 0.0
        result = compute_kelly_fraction(0.50, 0.01, 0.01, half=True)
        assert result == 0.0

    def test_zero_avg_loss_returns_zero(self):
        result = compute_kelly_fraction(0.6, 0.02, 0.0, half=True)
        assert result == 0.0

    def test_zero_avg_win_returns_zero(self):
        result = compute_kelly_fraction(0.6, 0.0, 0.01, half=True)
        assert result == 0.0

    def test_negative_edge_floored_at_zero(self):
        # Very bad strategy: win_rate=0.2, b=0.5
        # f* = 0.2 - 0.8/0.5 = 0.2 - 1.6 = -1.4 → 0
        result = compute_kelly_fraction(0.20, 0.005, 0.01, half=True)
        assert result == 0.0


class TestKellyRiskFraction:
    def test_strong_edge_caps_at_max(self):
        # kelly_f=0.10, base=0.01, strength=1.0
        # mult = 0.10/0.01 * 1.0 = 10 → capped at max_mult=2.0 → 0.01*2=0.02
        result = kelly_risk_fraction(0.10, 1.0, 0.01, max_mult=2.0, min_mult=0.25)
        assert abs(result - 0.02) < 1e-9

    def test_weak_edge_floors_at_min(self):
        # kelly_f=0.001, base=0.01, strength=0.5
        # mult = 0.001/0.01 * 0.5 = 0.05 → floored at 0.25 → 0.01*0.25=0.0025
        result = kelly_risk_fraction(0.001, 0.5, 0.01, max_mult=2.0, min_mult=0.25)
        assert abs(result - 0.0025) < 1e-9

    def test_zero_kelly_floors_at_min(self):
        result = kelly_risk_fraction(0.0, 0.8, 0.01, max_mult=2.0, min_mult=0.25)
        assert abs(result - 0.0025) < 1e-9

    def test_neutral_kelly_equals_base_with_full_strength(self):
        # kelly_f = base → mult = 1.0, strength=1.0 → result = base
        result = kelly_risk_fraction(0.01, 1.0, 0.01, max_mult=2.0, min_mult=0.25)
        assert abs(result - 0.01) < 1e-9

    def test_signal_strength_scales_within_range(self):
        # kelly_f=0.015, base=0.01, strength=0.8
        # mult = 0.015/0.01 * 0.8 = 1.2 → 0.01 * 1.2 = 0.012
        result = kelly_risk_fraction(0.015, 0.8, 0.01, max_mult=2.0, min_mult=0.25)
        assert abs(result - 0.012) < 1e-9

    def test_zero_base_risk_returns_zero(self):
        result = kelly_risk_fraction(0.05, 0.8, 0.0)
        assert result == 0.0
