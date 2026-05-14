"""Integration test: run_window() against synthetic OHLCV."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig
from bot.audit.walk_forward import Window, WindowResult, run_window


def _synthetic_klines(start: datetime, end: datetime, interval_h: int = 4,
                       seed: int = 0) -> pd.DataFrame:
    """Deterministic random-walk OHLCV for testing."""
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range(start, end, freq=f"{interval_h}h", tz=timezone.utc, inclusive="left")
    n = len(timestamps)
    prices = 100.0 + np.cumsum(rng.normal(0, 1, size=n))
    prices = np.maximum(prices, 1.0)
    df = pd.DataFrame({
        "open_time": timestamps,
        "open":  prices,
        "high":  prices * 1.005,
        "low":   prices * 0.995,
        "close": prices,
        "volume": rng.uniform(1000, 5000, size=n),
    })
    return df


def test_run_window_returns_window_result_for_synthetic_data() -> None:
    """run_window must accept slices, invoke PortfolioBacktestEngine, return WindowResult."""
    train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    test_end    = datetime(2024, 9, 1, tzinfo=timezone.utc)
    w = Window(
        index=0,
        train_start=train_start,
        train_end=datetime(2024, 7, 1, tzinfo=timezone.utc),
        test_start=datetime(2024, 7, 1, tzinfo=timezone.utc),
        test_end=test_end,
    )

    dfs = {
        "BTCUSDT": _synthetic_klines(train_start, test_end, interval_h=4, seed=1),
        "ETHUSDT": _synthetic_klines(train_start, test_end, interval_h=4, seed=2),
    }
    dfs_bias = {
        "BTCUSDT": _synthetic_klines(train_start, test_end, interval_h=24, seed=3),
        "ETHUSDT": _synthetic_klines(train_start, test_end, interval_h=24, seed=4),
    }

    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.015, timeframe="4h",
        long_only=True, ema_stop_mult=1.5, ema_tp_mult=4.5,
    )
    result = run_window(
        window=w,
        backtest_config=cfg,
        config_name="C1",
        dfs=dfs,
        dfs_bias=dfs_bias,
        dfs_weekly=None,
    )
    assert isinstance(result, WindowResult)
    assert result.config_name == "C1"
    assert result.window.index == 0
    # Numbers can be anything on synthetic data — just verify shape
    assert isinstance(result.pf, float)
    assert isinstance(result.total_trades, int)


def test_run_all_iterates_windows_and_configs() -> None:
    train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end         = datetime(2025, 6, 1, tzinfo=timezone.utc)

    dfs = {
        "BTCUSDT": _synthetic_klines(train_start, end, interval_h=4, seed=1),
        "ETHUSDT": _synthetic_klines(train_start, end, interval_h=4, seed=2),
    }
    dfs_bias = {
        "BTCUSDT": _synthetic_klines(train_start, end, interval_h=24, seed=3),
        "ETHUSDT": _synthetic_klines(train_start, end, interval_h=24, seed=4),
    }

    from bot.audit.walk_forward import WalkForwardConfig, run_all
    from bot.backtest.engine import BacktestConfig

    wf_cfg = WalkForwardConfig(
        start_date=train_start, end_date=end,
        train_months=6, test_months=3, step_months=3,
        symbols=("BTCUSDT", "ETHUSDT"), timeframe="4h",
    )
    configs = {
        "C1": BacktestConfig(initial_capital=10_000.0, risk_per_trade=0.015,
                              timeframe="4h", long_only=True,
                              ema_stop_mult=1.5, ema_tp_mult=4.5),
        "C2": BacktestConfig(initial_capital=10_000.0, risk_per_trade=0.03,
                              timeframe="4h", long_only=True,
                              ema_stop_mult=1.25, ema_tp_mult=3.5),
    }
    results = run_all(
        wf_config=wf_cfg, backtest_configs=configs,
        dfs=dfs, dfs_bias=dfs_bias, dfs_weekly=None,
    )
    # 6mo train + 3mo test, step 3mo over 17 months → ≥ 3 windows × 2 configs = 6 results
    assert len(results) >= 6
    config_names_seen = {r.config_name for r in results}
    assert config_names_seen == {"C1", "C2"}
