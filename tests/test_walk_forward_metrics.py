"""Tests for bot.audit.walk_forward.extract_window_metrics()."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.audit.walk_forward import Window, WindowResult, extract_window_metrics


def _window() -> Window:
    return Window(
        index=0,
        train_start=datetime(2022, 4, 1),
        train_end=datetime(2023, 10, 1),
        test_start=datetime(2023, 10, 1),
        test_end=datetime(2024, 1, 1),
    )


def _portfolio_result(global_summary: dict, per_symbol: dict) -> SimpleNamespace:
    """Build a minimal stub matching the shape of PortfolioBacktestResult.

    PortfolioBacktestResult has `portfolio_summary` (dict) and
    `per_symbol_summary` (dict[symbol, dict]).
    """
    return SimpleNamespace(
        portfolio_summary=global_summary,
        per_symbol_summary=per_symbol,
    )


def test_extracts_global_metrics() -> None:
    pr = _portfolio_result(
        global_summary={
            "profit_factor":      1.40,
            "sharpe_ratio":       1.20,
            "win_rate_pct":       42.0,
            "max_drawdown_pct":   12.5,
            "total_trades":       18,
            "total_pnl_pct":      8.0,
        },
        per_symbol={
            "BTCUSDT": {"profit_factor": 1.8, "total_trades": 10, "total_pnl_pct": 6.0},
            "ETHUSDT": {"profit_factor": 0.9, "total_trades": 8,  "total_pnl_pct": 2.0},
        },
    )
    result = extract_window_metrics(
        window=_window(),
        portfolio_result=pr,
        config_name="C1",
    )
    assert isinstance(result, WindowResult)
    assert result.config_name == "C1"
    assert result.pf == 1.40
    assert result.sharpe == 1.20
    assert result.win_rate_pct == 42.0
    assert result.max_drawdown_pct == 12.5
    assert result.total_trades == 18
    assert result.final_pnl_pct == 8.0


def test_calmar_computed_from_annualized_return() -> None:
    """Calmar = annualized_return / abs(max_dd). Test period = 3 months → 4x scaling."""
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 1.5, "sharpe_ratio": 1.0, "win_rate_pct": 40.0,
            "max_drawdown_pct": 10.0, "total_trades": 10, "total_pnl_pct": 5.0,
        },
        per_symbol={},
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C1")
    # 5% over 3 months → 20% annualized → Calmar = 20 / 10 = 2.0
    assert result.calmar == pytest.approx(2.0, abs=0.01)


def test_per_symbol_carried_through() -> None:
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 1.5, "sharpe_ratio": 1.0, "win_rate_pct": 40.0,
            "max_drawdown_pct": 10.0, "total_trades": 10, "total_pnl_pct": 5.0,
        },
        per_symbol={
            "BTCUSDT": {"profit_factor": 2.0, "total_trades": 7, "total_pnl_pct": 4.0},
            "ETHUSDT": {"profit_factor": 0.5, "total_trades": 3, "total_pnl_pct": 1.0},
        },
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C2")
    assert result.per_symbol["BTCUSDT"]["profit_factor"] == 2.0
    assert result.per_symbol["ETHUSDT"]["total_trades"] == 3


def test_zero_dd_yields_inf_calmar() -> None:
    """A perfect run (no drawdown) has infinite Calmar — must not crash on division."""
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 5.0, "sharpe_ratio": 3.0, "win_rate_pct": 100.0,
            "max_drawdown_pct": 0.0, "total_trades": 5, "total_pnl_pct": 10.0,
        },
        per_symbol={},
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C1")
    assert result.calmar == float("inf")
