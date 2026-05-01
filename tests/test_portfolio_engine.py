"""Unit tests for the portfolio backtest engine — all synthetic data, no network."""

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    EXIT_END_OF_PERIOD,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)
from bot.backtest.portfolio_engine import (
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _synthetic_ohlcv(
    start:   str,
    periods: int,
    freq:    str   = "1h",
    trend:   float = 0.0,
    base:    float = 50_000.0,
) -> pd.DataFrame:
    """Synthetic OHLCV with a deterministic trend + seeded noise."""
    times  = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")
    closes = (
        base
        + (np.arange(periods) * trend)
        + np.random.RandomState(42).normal(0, base * 0.005, periods)
    )
    return pd.DataFrame({
        "open_time": times,
        "open":      closes,
        "high":      closes * 1.005,
        "low":       closes * 0.995,
        "close":     closes,
        "volume":    1_000.0,
    })


def _default_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = 0.01,
        timeframe         = "1h",
        cost_per_side_pct = 0.0,
    )


# ── 1. Dataclass shape ────────────────────────────────────────────────────────

def test_portfolio_result_dataclass_shape():
    """PortfolioBacktestResult must expose every spec'd field."""
    result = PortfolioBacktestResult(
        combined_equity_curve = [],
        per_symbol_trades     = {},
        per_symbol_summary    = {},
        portfolio_summary     = {},
        start_date            = "",
        end_date              = "",
        symbols               = [],
        timeframe             = "1h",
        initial_capital       = 0.0,
        final_capital         = 0.0,
    )
    assert result.combined_equity_curve == []
    assert result.per_symbol_trades     == {}
    assert result.per_symbol_summary    == {}
    assert result.portfolio_summary     == {}
    assert result.start_date            == ""
    assert result.end_date              == ""
    assert result.symbols               == []
    assert result.timeframe             == "1h"
    assert result.initial_capital       == 0.0
    assert result.final_capital         == 0.0


# ── 2. Empty input rejected ──────────────────────────────────────────────────

def test_run_portfolio_rejects_empty_dfs():
    """Empty `dfs` mapping must raise ValueError."""
    engine = PortfolioBacktestEngine(_default_config())
    with pytest.raises(ValueError, match="at least one symbol"):
        engine.run_portfolio({})


# ── 3. N=1 matches the single-engine baseline ────────────────────────────────

def test_n_equals_1_matches_single_engine():
    """A 1-symbol portfolio run should converge to the same final capital
    as a plain BacktestEngine run on the same DataFrame.

    A small tolerance accommodates the union-iteration bookkeeping (the
    portfolio engine re-records the equity curve point on every union timestamp
    rather than only on close events) and any minor rounding differences."""
    cfg = _default_config()
    df  = _synthetic_ohlcv("2024-01-01", periods=300, freq="1h", trend=20.0, base=40_000.0)

    single_engine = BacktestEngine(cfg)
    single_result = single_engine.run(df.copy(), symbol="BTCUSDT")

    portfolio = PortfolioBacktestEngine(cfg).run_portfolio({"BTCUSDT": df.copy()})

    assert portfolio.final_capital == pytest.approx(single_result.final_capital, rel=0.01)


# ── 4. Max one open position per symbol ──────────────────────────────────────

def test_max_one_position_per_symbol():
    """For any symbol the trade list must never contain two trades whose
    [entry_time, exit_time] windows overlap."""
    cfg = _default_config()
    df  = _synthetic_ohlcv("2024-01-01", periods=300, freq="1h", trend=15.0, base=40_000.0)

    portfolio = PortfolioBacktestEngine(cfg).run_portfolio({"BTCUSDT": df.copy()})

    for symbol, trades in portfolio.per_symbol_trades.items():
        for i in range(len(trades) - 1):
            t_curr = trades[i]
            t_next = trades[i + 1]
            assert t_curr["exit_bar"]  is not None
            assert t_curr["exit_time"] is not None
            # Subsequent trade for the same symbol cannot start before the
            # previous one closes (bar index ordering is monotonic).
            assert t_curr["exit_bar"] <= t_next["entry_bar"], (
                f"{symbol}: trade {i} (bars {t_curr['entry_bar']}–{t_curr['exit_bar']}) "
                f"overlaps trade {i + 1} (bars {t_next['entry_bar']}–{t_next['exit_bar']})"
            )


# ── 5. Time alignment — symbols missing bars at certain timestamps ───────────

def test_time_alignment_skips_missing_bars():
    """When ETH only has bars on every other timestamp, the portfolio engine
    must still run end-to-end and never produce an ETH trade whose entry_time
    isn't actually present in the original ETH DataFrame."""
    cfg = _default_config()

    df_btc = _synthetic_ohlcv("2024-01-01", periods=300, freq="1h", trend=15.0, base=40_000.0)
    # ETH at half-resolution: every-other timestamp from BTC's grid.
    df_eth_full = _synthetic_ohlcv("2024-01-01", periods=300, freq="1h", trend=10.0, base=2_500.0)
    df_eth = df_eth_full.iloc[::2].reset_index(drop=True)

    portfolio = PortfolioBacktestEngine(cfg).run_portfolio({
        "BTCUSDT": df_btc.copy(),
        "ETHUSDT": df_eth.copy(),
    })

    assert len(portfolio.combined_equity_curve) > 0

    eth_valid_times = set(df_eth["open_time"].tolist())
    for trade in portfolio.per_symbol_trades.get("ETHUSDT", []):
        # entry_time stored as Timestamp — compare directly against the original set
        assert trade["entry_time"] in eth_valid_times, (
            f"ETH trade entry_time {trade['entry_time']} is not in the original ETH bars"
        )
