"""Tests for bot.audit.verdict.evaluate_verdict()."""
import pytest

from bot.audit.verdict import VerdictThresholds, evaluate_verdict


def _agg(pf_mean: float, pf_hit: float, calmar_mean: float,
         worst_dd: float, sparsity_pct: float, n: int = 8) -> dict:
    return {
        "pf":               {"mean": pf_mean, "hit_rate": pf_hit},
        "calmar":           {"mean": calmar_mean},
        "max_drawdown_pct": {"worst": worst_dd},
        "sparsity":         {"pct": sparsity_pct},
        "n_windows":        n,
    }


def test_go_when_all_thresholds_met() -> None:
    v = evaluate_verdict(_agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
                              worst_dd=20.0, sparsity_pct=0.10))
    assert v["bucket"] == "GO"
    assert all(c["passed"] for c in v["checks"])


def test_no_go_when_pf_well_below_threshold() -> None:
    v = evaluate_verdict(_agg(pf_mean=0.95, pf_hit=0.40, calmar_mean=0.8,
                              worst_dd=42.0, sparsity_pct=0.30))
    assert v["bucket"] == "NO-GO"


def test_watch_when_marginal_failure() -> None:
    """PF mean = 1.10 is 8.3% below threshold 1.20 → WATCH (within 10%)."""
    v = evaluate_verdict(_agg(pf_mean=1.10, pf_hit=0.70, calmar_mean=1.6,
                              worst_dd=30.0, sparsity_pct=0.10))
    assert v["bucket"] == "WATCH"


def test_pf_exactly_at_threshold_is_go() -> None:
    """PF = 1.20 exact triggers GO (the threshold is inclusive)."""
    v = evaluate_verdict(_agg(pf_mean=1.20, pf_hit=0.70, calmar_mean=1.5,
                              worst_dd=20.0, sparsity_pct=0.10))
    assert v["bucket"] == "GO"


def test_dd_above_35_is_hard_fail() -> None:
    """Even with great PF, max DD > 35% fails."""
    v = evaluate_verdict(_agg(pf_mean=1.50, pf_hit=0.90, calmar_mean=2.5,
                              worst_dd=40.0, sparsity_pct=0.05))
    # 40 is 14% above 35 threshold → > 10% margin → NO-GO
    assert v["bucket"] == "NO-GO"


def test_checks_array_lists_each_threshold() -> None:
    v = evaluate_verdict(_agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
                              worst_dd=20.0, sparsity_pct=0.10))
    check_names = {c["name"] for c in v["checks"]}
    assert check_names == {
        "pf_mean", "pf_hit_rate", "calmar_mean", "max_dd_within_limit",
        "sparsity_within_limit",
    }


def test_custom_thresholds_override_defaults() -> None:
    """Caller can pass a custom VerdictThresholds for sub-projects that want different bars."""
    strict = VerdictThresholds(pf_mean=1.5, pf_hit_rate=0.80, calmar_mean=2.0,
                                max_dd_pct=25.0, sparsity_pct=0.10)
    v = evaluate_verdict(
        _agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
             worst_dd=20.0, sparsity_pct=0.10),
        thresholds=strict,
    )
    assert v["bucket"] != "GO"
