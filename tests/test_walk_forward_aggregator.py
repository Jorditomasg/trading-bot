"""Tests for bot.audit.walk_forward.aggregate_metrics()."""
import math
from datetime import datetime

import numpy as np
import pytest

from bot.audit.walk_forward import Window, WindowResult, aggregate_metrics


def _wr(pf: float, calmar: float = 1.5, max_dd: float = 10.0, n: int = 10,
        config_name: str = "C1") -> WindowResult:
    w = Window(
        index=0,
        train_start=datetime(2022,1,1), train_end=datetime(2023,1,1),
        test_start=datetime(2023,1,1),  test_end=datetime(2023,4,1),
    )
    return WindowResult(
        window=w, config_name=config_name,
        pf=pf, calmar=calmar, sharpe=1.0, win_rate_pct=40.0,
        max_drawdown_pct=max_dd, total_trades=n, final_pnl_pct=5.0,
    )


def test_mean_and_median_of_pf() -> None:
    results = [_wr(pf=1.0), _wr(pf=1.5), _wr(pf=2.0)]
    agg = aggregate_metrics(results)
    assert agg["pf"]["mean"]   == pytest.approx(1.5, abs=1e-6)
    assert agg["pf"]["median"] == pytest.approx(1.5, abs=1e-6)
    assert agg["pf"]["std"]    == pytest.approx(np.std([1.0, 1.5, 2.0], ddof=1), abs=1e-6)


def test_hit_rate_pf_above_one() -> None:
    """Hit rate = % of windows with PF > 1.0."""
    results = [_wr(pf=0.8), _wr(pf=1.0), _wr(pf=1.2), _wr(pf=1.5)]
    agg = aggregate_metrics(results)
    # 2 out of 4 strictly > 1.0 (PF=1.0 is NOT a hit)
    assert agg["pf"]["hit_rate"] == pytest.approx(0.5, abs=1e-6)


def test_bootstrap_ci_returns_two_floats() -> None:
    """95% bootstrap CI for mean PF on a clearly-positive sample."""
    results = [_wr(pf=p) for p in [1.2, 1.4, 1.5, 1.3, 1.6, 1.45]]
    agg = aggregate_metrics(results, bootstrap_seed=42, n_bootstrap=2000)
    ci_lo, ci_hi = agg["pf"]["ci95"]
    assert isinstance(ci_lo, float) and isinstance(ci_hi, float)
    assert ci_lo < agg["pf"]["mean"] < ci_hi


def test_worst_window_metrics() -> None:
    results = [
        _wr(pf=1.0, max_dd=5.0),
        _wr(pf=0.5, max_dd=25.0),   # worst PF AND worst DD
        _wr(pf=1.5, max_dd=8.0),
    ]
    agg = aggregate_metrics(results)
    assert agg["pf"]["worst"]              == pytest.approx(0.5, abs=1e-6)
    assert agg["max_drawdown_pct"]["worst"] == pytest.approx(25.0, abs=1e-6)


def test_sparsity_count_windows_under_5_trades() -> None:
    results = [_wr(pf=1.0, n=2), _wr(pf=1.0, n=4), _wr(pf=1.0, n=8), _wr(pf=1.0, n=10)]
    agg = aggregate_metrics(results)
    assert agg["sparsity"]["windows_lt_5_trades"] == 2
    assert agg["sparsity"]["pct"]                == pytest.approx(0.5, abs=1e-6)


def test_inf_calmar_ignored_in_mean() -> None:
    """inf Calmar (zero DD windows) should not poison the mean."""
    results = [
        _wr(pf=1.0, calmar=2.0),
        _wr(pf=1.0, calmar=float("inf")),
        _wr(pf=1.0, calmar=3.0),
    ]
    agg = aggregate_metrics(results)
    assert math.isfinite(agg["calmar"]["mean"])
    assert agg["calmar"]["mean"] == pytest.approx(2.5, abs=1e-6)


def test_empty_input_returns_empty_dict() -> None:
    assert aggregate_metrics([]) == {}
