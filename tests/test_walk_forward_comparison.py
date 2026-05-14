"""Tests for bot.audit.comparison.compare_configs()."""
import numpy as np
import pytest

from bot.audit.comparison import (
    cohens_d_paired,
    paired_t_test,
    compare_configs,
)


def test_paired_t_test_zero_difference_yields_zero_t() -> None:
    a = [1.0, 1.2, 1.4, 1.6, 1.5]
    b = list(a)
    result = paired_t_test(a, b)
    assert result["t"]   == pytest.approx(0.0, abs=1e-9)
    assert result["p"]   == pytest.approx(1.0, abs=1e-6)


def test_paired_t_test_large_difference_yields_significant_p() -> None:
    a = [2.0, 2.1, 2.2, 2.3, 2.4]
    b = [1.0, 1.1, 1.2, 1.3, 1.4]
    result = paired_t_test(a, b)
    assert result["t"] > 0
    assert result["p"] < 0.01


def test_cohens_d_paired_zero_when_identical() -> None:
    assert cohens_d_paired([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 0.0


def test_cohens_d_paired_positive_when_a_higher() -> None:
    """A is higher than B → positive d."""
    a = [1.5, 1.6, 1.4, 1.7]
    b = [1.0, 1.1, 0.9, 1.2]
    assert cohens_d_paired(a, b) > 0


def test_cohens_d_paired_negative_when_b_higher() -> None:
    assert cohens_d_paired([1.0, 1.1], [1.5, 1.6]) < 0


def test_compare_configs_returns_full_summary() -> None:
    pf_c1 = [1.2, 1.4, 1.3, 1.5, 1.45]
    pf_c2 = [1.5, 1.6, 1.55, 1.7, 1.65]
    result = compare_configs(pf_c1, pf_c2, metric_name="pf")
    assert "t" in result and "p" in result
    assert "cohens_d" in result
    assert "mean_a" in result and "mean_b" in result
    assert "delta_mean" in result
    assert result["delta_mean"] == pytest.approx(np.mean(pf_c2) - np.mean(pf_c1), abs=1e-9)


def test_compare_configs_requires_equal_length() -> None:
    with pytest.raises(ValueError):
        compare_configs([1.0, 2.0], [1.0, 2.0, 3.0])
