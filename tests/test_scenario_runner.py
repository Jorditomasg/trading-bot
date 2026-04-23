"""Tests for ScenarioRunner — synthetic data, no network calls."""

import pandas as pd
import pytest

from bot.backtest.scenario_runner import (
    SCENARIOS,
    Scenario,
    ScenarioResult,
    ScenarioRunner,
    compute_annual_return,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_ohlcv(closes: list[float], freq: str = "1h") -> pd.DataFrame:
    n = len(closes)
    times = pd.date_range("2022-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open":   closes,
        "high":   [c * 1.005 for c in closes],
        "low":    [c * 0.995 for c in closes],
        "close":  closes,
        "volume": [1_000_000.0] * n,
    })


def _uptrend_1h(n: int = 400) -> pd.DataFrame:
    step = (50_000 - 40_000) / (n - 1)
    return _make_ohlcv([40_000 + i * step for i in range(n)], freq="1h")


def _bullish_weekly(n: int = 60) -> pd.DataFrame:
    """Weekly candles with last bar well above SMA → always BULLISH."""
    closes = [40_000.0] * (n - 1) + [55_000.0]
    return _make_ohlcv(closes, freq="7D")


def _make_runner(lookback_days: int = 14) -> ScenarioRunner:
    df_1h     = _uptrend_1h(400)
    df_4h     = _make_ohlcv([45_000.0] * 200, freq="4h")
    df_1d     = _make_ohlcv([45_000.0] * 200, freq="1D")
    df_weekly = _bullish_weekly(60)
    return ScenarioRunner(
        df_1h=df_1h,
        df_4h=df_4h,
        df_1d=df_1d,
        df_weekly=df_weekly,
        lookback_days=lookback_days,
        risk_per_trade=0.01,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_compute_annual_return_doubles():
    """Doubling capital over 365 days = 100% annual return."""
    result = compute_annual_return(10_000.0, 20_000.0, 365)
    assert abs(result - 1.0) < 0.001


def test_compute_annual_return_flat():
    """No change = 0% annual return."""
    result = compute_annual_return(10_000.0, 10_000.0, 365)
    assert abs(result) < 0.0001


def test_compute_annual_return_loss():
    """Losing money returns negative."""
    result = compute_annual_return(10_000.0, 5_000.0, 365)
    assert result < 0


def test_scenario_dataclass_stores_fields():
    s = Scenario("Test", "1h", 2.0, True)
    assert s.name == "Test"
    assert s.timeframe == "1h"
    assert s.leverage == 2.0
    assert s.momentum_filter is True


def test_scenarios_list_has_8_entries():
    assert len(SCENARIOS) == 8


def test_scenarios_first_is_baseline():
    assert SCENARIOS[0].name == "Baseline 4h"
    assert SCENARIOS[0].timeframe == "4h"
    assert SCENARIOS[0].leverage == 1.0
    assert SCENARIOS[0].momentum_filter is False


def test_scenarios_last_is_10x():
    assert SCENARIOS[-1].leverage == 10.0
    assert SCENARIOS[-1].momentum_filter is True


def test_run_all_returns_one_result_per_scenario():
    """run_all() returns a ScenarioResult for each scenario."""
    runner = _make_runner()
    results = runner.run_all(SCENARIOS)

    assert len(results) == len(SCENARIOS)
    for r in results:
        assert isinstance(r, ScenarioResult)
        assert r.total_trades >= 0
        assert r.liquidations >= 0
        assert isinstance(r.equity_curve, list)
        assert r.final_capital > 0


def test_run_all_custom_scenarios():
    """run_all() works with a custom scenario list."""
    runner = _make_runner()
    custom = [Scenario("spot-1h", "1h", 1.0, False)]
    results = runner.run_all(custom)
    assert len(results) == 1
    assert results[0].scenario.name == "spot-1h"


def test_leverage_increases_return_on_uptrend():
    """3× leverage scenario should have higher (or equal) return than spot on uptrend."""
    runner = _make_runner(lookback_days=30)
    spot   = Scenario("spot", "1h", 1.0, False)
    lev3   = Scenario("3x",   "1h", 3.0, False)
    results = runner.run_all([spot, lev3])

    assert len(results[0].equity_curve) > 0
    assert results[1].annual_return_pct >= results[0].annual_return_pct


def test_momentum_filter_off_by_default_for_baseline():
    """Baseline 4h scenario never uses momentum filter."""
    runner  = _make_runner()
    results = runner.run_all([SCENARIOS[0]])  # Baseline 4h
    assert results[0].scenario.momentum_filter is False
