"""Pure portfolio-backtest runner — no UI dependencies, fully testable.

Splits the dashboard's `_run_portfolio_backtest` into composable pure functions
so the same code path can be driven from tests without Streamlit. Three pieces:

1. `build_fetch_plan(req)` — deterministic per-(symbol, timeframe) fetch jobs
   including the warmup buffers required by higher-timeframe filters
   (gotcha #37).
2. `build_backtest_config(req, runtime_cfg)` — single source of truth for how
   DB `bot_config` keys map into `BacktestConfig`. The live bot's
   `main._apply_ema_config()` applies the same keys to the live strategy; the
   runtime parity test asserts both paths produce equivalent values.
3. `run_portfolio_backtest_core(req, runtime_cfg, *, fetcher, ...)` — drives
   the full backtest given any callable fetcher. The dashboard injects
   `fetch_and_cache`; tests inject a fake returning canned DataFrames.

Live behaviour is unaffected: live `run_cycle()` does not use this module. It
exists purely to make the dashboard runner testable and to lock in the
config-mapping invariant that gotchas #36/#37 violated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import (
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)
from bot.config_presets import BIAS_TIMEFRAME_MAP

logger = logging.getLogger(__name__)


# Warmup buffers — see gotcha #37. The higher-timeframe filters need indicator
# history BEFORE backtest_start. Without these:
#   - BiasFilter falls into NEUTRAL/passthrough until ≥22 daily bars exist
#   - MomentumFilter falls into BULLISH (fail-open) until ≥21 weekly bars exist
# Bias=22 days × safety = 30d; momentum=21 weeks (147d) × safety = 154d.
BIAS_WARMUP:     timedelta = timedelta(days=30)
MOMENTUM_WARMUP: timedelta = timedelta(days=154)


# ── User-facing request ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class BacktestRequest:
    """Parameters for one portfolio backtest run.

    `cost_per_side` is the fraction applied to entry AND exit (0.001 = 0.10%).
    `risk` is the fraction of capital risked per trade (0.015 = 1.5%).
    """
    symbols:       tuple[str, ...]
    timeframe:     str
    start_dt:      datetime
    end_dt:        datetime
    capital:       float
    risk:          float
    cost_per_side: float
    use_bias:      bool
    use_momentum:  bool
    use_1m:        bool


# ── Fetch planning ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FetchJob:
    symbol:   str
    interval: str
    start:    datetime
    end:      datetime


@dataclass(frozen=True)
class FetchPlan:
    """Per-(symbol, timeframe) fetch jobs the runner will execute."""
    primary: tuple[FetchJob, ...]
    bias:    tuple[FetchJob, ...]
    weekly:  tuple[FetchJob, ...]
    minute:  tuple[FetchJob, ...]


def build_fetch_plan(req: BacktestRequest) -> FetchPlan:
    bias_tf = BIAS_TIMEFRAME_MAP.get(req.timeframe, "1d")
    primary, bias, weekly, minute = [], [], [], []
    for sym in req.symbols:
        primary.append(FetchJob(sym, req.timeframe, req.start_dt, req.end_dt))
        if req.use_bias:
            bias.append(FetchJob(sym, bias_tf, req.start_dt - BIAS_WARMUP, req.end_dt))
        if req.use_momentum:
            weekly.append(FetchJob(sym, "1w", req.start_dt - MOMENTUM_WARMUP, req.end_dt))
        if req.use_1m:
            minute.append(FetchJob(sym, "1m", req.start_dt, req.end_dt))
    return FetchPlan(
        primary=tuple(primary),
        bias=tuple(bias),
        weekly=tuple(weekly),
        minute=tuple(minute),
    )


# ── BacktestConfig builder ───────────────────────────────────────────────────


def build_backtest_config(
    req: BacktestRequest,
    runtime_cfg: dict,
) -> BacktestConfig:
    """Map user request + DB runtime config into a `BacktestConfig`.

    Fallback values mirror `_seed_optimized_defaults()` in main.py — they
    represent the validated baseline. Gotchas #36 and #37 surfaced because
    these fallbacks (or the consumer keys) were wrong on the dashboard side
    while live had different defaults. The runtime parity test guards this.

    `momentum_neutral_band` is intentionally hardcoded to 0.08 here — matches
    the live `MomentumFilter` dataclass default (live never reads the DB key,
    so reading it on the backtest side would break parity, not improve it).
    """
    return BacktestConfig(
        initial_capital         = req.capital,
        risk_per_trade          = req.risk,
        timeframe               = req.timeframe,
        cost_per_side_pct       = req.cost_per_side,
        momentum_filter_enabled = req.use_momentum,
        momentum_sma_period     = 20,
        momentum_neutral_band   = 0.08,
        long_only            = runtime_cfg.get("long_only", "true") == "true",
        ema_stop_mult        = float(runtime_cfg.get("ema_stop_mult", 1.5)),
        ema_tp_mult          = float(runtime_cfg.get("ema_tp_mult", 5.0)),
        ema_max_distance_atr = float(runtime_cfg.get("ema_max_dist_atr", 1.0)),
        ema_volume_mult      = float(runtime_cfg.get("ema_vol_mult", 1.5)),
        ema_require_momentum = runtime_cfg.get("ema_momentum_req", "true") == "true",
        ema_require_bar_dir  = runtime_cfg.get("ema_bar_dir", "true") == "true",
        ema_min_atr_pct      = float(runtime_cfg.get("ema_min_atr", 0.005)),
        ema_min_entry_adx            = float(runtime_cfg.get("ema_min_entry_adx", 0.0)),
        ema_require_ema200_alignment = runtime_cfg.get("ema_require_ema200", "false") == "true",
    )


# ── Full runner ──────────────────────────────────────────────────────────────


ProgressCb = Callable[[str], None]
WarningCb  = Callable[[str, str], None]
Fetcher    = Callable[[str, str, datetime, datetime], pd.DataFrame]


def run_portfolio_backtest_core(
    req: BacktestRequest,
    runtime_cfg: dict,
    *,
    fetcher: Fetcher = fetch_and_cache,
    on_progress: Optional[ProgressCb] = None,
    on_warning:  Optional[WarningCb]  = None,
) -> Optional[PortfolioBacktestResult]:
    """Run a portfolio backtest end-to-end. Returns None when no primary data
    could be fetched for any symbol.

    Side effects are isolated to the injected callbacks — the dashboard wraps
    this with Streamlit spinner/error UI; tests inject a fake fetcher that
    returns canned DataFrames so the engine can be driven offline.
    """
    plan = build_fetch_plan(req)

    dfs:        dict[str, pd.DataFrame] = {}
    dfs_4h:     dict[str, pd.DataFrame | None] = {}
    dfs_weekly: dict[str, pd.DataFrame | None] = {}
    dfs_1m:     dict[str, pd.DataFrame | None] = {}

    progress = on_progress or (lambda _msg: None)
    warn     = on_warning  or (lambda _sym, _msg: None)

    for job in plan.primary:
        progress(f"Fetching {job.symbol} {job.interval} data…")
        try:
            dfs[job.symbol] = fetcher(job.symbol, job.interval, job.start, job.end)
        except Exception as exc:
            warn(job.symbol, f"Failed to fetch {job.interval} data: {exc}")

    if not dfs:
        return None

    for job in plan.bias:
        if job.symbol not in dfs:
            continue
        progress(f"Fetching {job.symbol} {job.interval} klines for BiasFilter…")
        try:
            dfs_4h[job.symbol] = fetcher(job.symbol, job.interval, job.start, job.end)
        except Exception as exc:
            dfs_4h[job.symbol] = None
            warn(job.symbol, f"Could not fetch {job.interval} data ({exc}) — BiasFilter pass-through.")

    for job in plan.weekly:
        if job.symbol not in dfs:
            continue
        progress(f"Fetching {job.symbol} weekly klines for momentum filter…")
        try:
            dfs_weekly[job.symbol] = fetcher(job.symbol, job.interval, job.start, job.end)
        except Exception as exc:
            dfs_weekly[job.symbol] = None
            warn(job.symbol, f"Could not fetch weekly data ({exc}) — momentum pass-through.")

    for job in plan.minute:
        if job.symbol not in dfs:
            continue
        progress(f"Fetching {job.symbol} 1m klines for precision exits…")
        try:
            dfs_1m[job.symbol] = fetcher(job.symbol, job.interval, job.start, job.end)
            progress(f"{job.symbol} 1m cache ready: {len(dfs_1m[job.symbol]):,} bars")
        except Exception as exc:
            dfs_1m[job.symbol] = None
            warn(job.symbol, f"Could not fetch 1m data ({exc}) — bar-level precision only.")

    cfg = build_backtest_config(req, runtime_cfg)
    engine = PortfolioBacktestEngine(cfg)

    return engine.run_portfolio(
        dfs,
        dfs_4h     = dfs_4h     or None,
        dfs_weekly = dfs_weekly or None,
        dfs_1m     = dfs_1m     or None,
    )
