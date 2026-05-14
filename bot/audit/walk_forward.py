"""Walk-forward validation framework.

Splits a date range into rolling train/test windows, runs PortfolioBacktestEngine
on each test window with a fixed BacktestConfig, and collects per-window metrics
for downstream verdict / comparison analysis.

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class WalkForwardConfig:
    """Parameters for splitting a date range into walk-forward windows."""

    start_date: datetime
    end_date:   datetime
    train_months: int                       # warm-up period (no params learned here)
    test_months:  int                       # length of each out-of-sample test window
    step_months:  int                       # how far to shift the window each step
    symbols:      tuple[str, ...]
    timeframe:    str


@dataclass(frozen=True)
class Window:
    """A single train + test window pair within the walk-forward run."""

    index:       int
    train_start: datetime
    train_end:   datetime
    test_start:  datetime
    test_end:    datetime


@dataclass(frozen=True)
class WindowResult:
    """Metrics from running ONE config on ONE window's test period."""

    window:            Window
    config_name:       str                  # "C1" | "C2" | arbitrary label
    pf:                float
    calmar:            float
    sharpe:            float
    win_rate_pct:      float
    max_drawdown_pct:  float
    total_trades:      int
    final_pnl_pct:     float
    per_symbol:        dict[str, dict[str, Any]] = field(default_factory=dict)


def split_windows(cfg: WalkForwardConfig) -> list[Window]:
    """Return rolling train+test windows over [cfg.start_date, cfg.end_date].

    Each window has back-to-back train and test periods. Subsequent windows
    are shifted by cfg.step_months. When step == test_months, test windows
    are non-overlapping (independent samples — valid for paired t-tests).
    """
    train_delta = relativedelta(months=cfg.train_months)
    test_delta  = relativedelta(months=cfg.test_months)
    step_delta  = relativedelta(months=cfg.step_months)

    windows: list[Window] = []
    train_start = cfg.start_date
    index = 0
    while True:
        train_end  = train_start + train_delta
        test_start = train_end
        test_end   = test_start + test_delta
        if test_end > cfg.end_date:
            break
        windows.append(Window(
            index       = index,
            train_start = train_start,
            train_end   = train_end,
            test_start  = test_start,
            test_end    = test_end,
        ))
        train_start = train_start + step_delta
        index += 1
    return windows


def extract_window_metrics(
    window: Window,
    portfolio_result: Any,    # PortfolioBacktestResult — duck-typed for test stubs
    config_name: str,
) -> WindowResult:
    """Build a WindowResult from a PortfolioBacktestEngine output.

    PortfolioBacktestResult exposes `portfolio_summary` (dict) and
    `per_symbol_summary` (dict[symbol, dict]). We pull the canonical fields
    and compute Calmar from the annualized return.
    """
    s = portfolio_result.portfolio_summary

    pnl_pct        = float(s.get("total_pnl_pct", 0.0))
    max_dd_pct     = float(s.get("max_drawdown_pct", 0.0))
    test_months    = relativedelta(window.test_end, window.test_start).months \
                     + relativedelta(window.test_end, window.test_start).years * 12
    annual_factor  = 12.0 / max(test_months, 1)
    annualized_pct = pnl_pct * annual_factor

    if max_dd_pct == 0.0:
        calmar = float("inf")
    else:
        calmar = annualized_pct / abs(max_dd_pct)

    return WindowResult(
        window           = window,
        config_name      = config_name,
        pf               = float(s.get("profit_factor", 0.0)),
        calmar           = calmar,
        sharpe           = float(s.get("sharpe_ratio", 0.0)),
        win_rate_pct     = float(s.get("win_rate_pct", 0.0)),
        max_drawdown_pct = max_dd_pct,
        total_trades     = int(s.get("total_trades", 0)),
        final_pnl_pct    = pnl_pct,
        per_symbol       = dict(portfolio_result.per_symbol_summary),
    )


def _slice_by_time(df: "pd.DataFrame | None", start: datetime, end: datetime):
    """Return df rows where open_time is within [start, end). None passes through."""
    if df is None:
        return None
    mask = (df["open_time"] >= start) & (df["open_time"] < end)
    return df.loc[mask].reset_index(drop=True)


def run_window(
    window:           Window,
    backtest_config:  Any,         # BacktestConfig — duck-typed for circular import safety
    config_name:      str,
    dfs:              dict,
    dfs_bias:         dict | None  = None,
    dfs_weekly:       dict | None  = None,
    dfs_1m:           dict | None  = None,
) -> WindowResult:
    """Slice dataframes to the window's [train_start, test_end) range, run portfolio
    backtest on the combined train+test, then extract metrics.

    The training period is used only as indicator warm-up — no parameter fitting.
    All metrics are computed over the FULL combined range; the per-window stats
    therefore include warm-up bars. This matches BacktestEngine semantics.
    """
    from bot.backtest.portfolio_engine import PortfolioBacktestEngine

    dfs_sliced        = {sym: _slice_by_time(df, window.train_start, window.test_end)
                         for sym, df in dfs.items()}
    dfs_bias_sliced   = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_bias.items()}
        if dfs_bias else None
    )
    dfs_weekly_sliced = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_weekly.items()}
        if dfs_weekly else None
    )
    dfs_1m_sliced     = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_1m.items()}
        if dfs_1m else None
    )

    engine = PortfolioBacktestEngine(backtest_config)
    result = engine.run_portfolio(
        dfs_sliced,
        dfs_4h     = dfs_bias_sliced,
        dfs_weekly = dfs_weekly_sliced,
        dfs_1m     = dfs_1m_sliced,
    )
    return extract_window_metrics(window=window, portfolio_result=result, config_name=config_name)


def run_all(
    wf_config:        WalkForwardConfig,
    backtest_configs: dict[str, Any],     # {"C1": BacktestConfig, "C2": ...}
    dfs:              dict,
    dfs_bias:         dict | None = None,
    dfs_weekly:       dict | None = None,
    dfs_1m:           dict | None = None,
    progress_cb:      "callable | None" = None,
) -> list[WindowResult]:
    """Run every (window, config) pair sequentially.

    Order is `[w0_C1, w0_C2, w1_C1, w1_C2, ...]` so paired-window comparison
    is straightforward. progress_cb receives a string per (window, config).
    """
    windows = split_windows(wf_config)
    results: list[WindowResult] = []
    for w in windows:
        for name, bt_cfg in backtest_configs.items():
            if progress_cb:
                progress_cb(
                    f"window={w.index} config={name} "
                    f"test={w.test_start.date()}→{w.test_end.date()}"
                )
            results.append(run_window(
                window=w, backtest_config=bt_cfg, config_name=name,
                dfs=dfs, dfs_bias=dfs_bias, dfs_weekly=dfs_weekly, dfs_1m=dfs_1m,
            ))
    return results


def aggregate_metrics(
    results: list[WindowResult],
    *,
    bootstrap_seed: int = 42,
    n_bootstrap:    int = 2000,
) -> dict:
    """Aggregate per-window metrics into mean / median / std / CI / hit rate / worst.

    Returns an empty dict on empty input. Infinite values are stripped before
    computing mean/std (zero-DD windows produce inf Calmar — valid but skews stats).
    """
    if not results:
        return {}

    def _stats(values: list[float], hit_threshold: float | None = None) -> dict:
        arr      = np.array([v for v in values if np.isfinite(v)], dtype=float)
        all_arr  = np.array(values, dtype=float)
        out = {
            "mean":   float(np.mean(arr))   if arr.size else float("nan"),
            "median": float(np.median(arr)) if arr.size else float("nan"),
            "std":    float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
            "min":    float(np.min(all_arr))  if all_arr.size else float("nan"),
            "max":    float(np.max(all_arr))  if all_arr.size else float("nan"),
            "worst":  float(np.min(all_arr))  if all_arr.size else float("nan"),
        }
        if hit_threshold is not None and all_arr.size:
            out["hit_rate"] = float(np.mean(all_arr > hit_threshold))
        # Bootstrap CI on the finite values
        if arr.size > 1:
            rng = np.random.default_rng(bootstrap_seed)
            samples = rng.choice(arr, size=(n_bootstrap, arr.size), replace=True)
            means   = samples.mean(axis=1)
            out["ci95"] = (float(np.percentile(means, 2.5)),
                           float(np.percentile(means, 97.5)))
        else:
            out["ci95"] = (out["mean"], out["mean"])
        return out

    pf_vals     = [r.pf for r in results]
    calmar_vals = [r.calmar for r in results]
    sharpe_vals = [r.sharpe for r in results]
    wr_vals     = [r.win_rate_pct for r in results]
    dd_vals     = [r.max_drawdown_pct for r in results]
    pnl_vals    = [r.final_pnl_pct for r in results]
    trades      = [r.total_trades for r in results]

    # Worst DD means highest DD (not lowest) — override the convention
    dd_stats          = _stats(dd_vals)
    dd_stats["worst"] = float(np.max(np.array(dd_vals)))

    return {
        "pf":               {**_stats(pf_vals, hit_threshold=1.0)},
        "calmar":           _stats(calmar_vals),
        "sharpe":           _stats(sharpe_vals),
        "win_rate_pct":     _stats(wr_vals),
        "max_drawdown_pct": dd_stats,
        "final_pnl_pct":    _stats(pnl_vals),
        "total_trades":     {
            "mean":   float(np.mean(trades)),
            "median": float(np.median(trades)),
            "min":    int(np.min(trades)),
            "max":    int(np.max(trades)),
        },
        "sparsity": {
            "windows_lt_5_trades": int(sum(1 for n in trades if n < 5)),
            "pct":                 float(np.mean(np.array(trades) < 5)),
        },
        "n_windows": len(results),
    }
