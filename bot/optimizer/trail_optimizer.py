"""Trail stop parameter optimizer.

Runs a grid search over trail_activation_mult × trail_atr_mult using the
backtest engine on recent historical data. EMA TP/SL are fixed at the
current approved values (or defaults if none exist). Results ranked by
Profit Factor and saved to trail_optimizer_runs for human review.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from itertools import product
from typing import Callable

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.database.db import Database

logger = logging.getLogger(__name__)

# ── Search space ──────────────────────────────────────────────────────────────
ACTIVATION_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
TRAIL_GRID      = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

# Viability constraints (same as EMA optimizer)
MIN_TRADES       = 15
MAX_DRAWDOWN_PCT = 20.0
MIN_SHARPE       = 0.4
MIN_PF           = 1.05

# Bias timeframe per primary timeframe (mirrors walk_forward.py)
_BIAS_TF = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}

# EMA defaults when no approved run exists in DB
_DEFAULT_STOP_MULT = 1.5
_DEFAULT_TP_MULT   = 3.5


def _get_ema_config(db: Database) -> tuple[float, float]:
    """Return (ema_stop_mult, ema_tp_mult) from runtime config or defaults."""
    cfg = db.get_runtime_config()
    stop = float(cfg.get("ema_stop_mult", _DEFAULT_STOP_MULT))
    tp   = float(cfg.get("ema_tp_mult",   _DEFAULT_TP_MULT))
    return stop, tp


def _is_viable(summary: dict) -> bool:
    return (
        summary["total_trades"]         >= MIN_TRADES
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
        and summary["sharpe_ratio"]     >= MIN_SHARPE
        and summary["profit_factor"]    >= MIN_PF
    )


def run_trail_grid_search(
    db: Database,
    symbol: str,
    timeframe: str,
    lookback_days: int = 180,
    cost_per_side: float = 0.0007,
    capital: float = 10_000.0,
    risk_per_trade: float = 0.01,
    on_progress: Callable[[int, int, float, float, dict | None], None] | None = None,
) -> list[dict]:
    """Run a full trail activation × trail distance grid search on recent data.

    Args:
        db:            Database instance for saving results.
        symbol:        Binance pair, e.g. "BTCUSDT".
        timeframe:     Primary candle interval, e.g. "4h".
        lookback_days: How many days of history to use.
        cost_per_side: Fee fraction per side (0.0007 = 0.07% maker).
        capital:       Starting capital for each simulation.
        risk_per_trade: Fraction of capital risked per trade.
        on_progress:   Optional callback(combo_idx, total, activation, trail, summary|None).

    Returns:
        List of result dicts sorted viable-first then by profit_factor DESC.
        Each dict has: activation_mult, trail_mult, profit_factor, sharpe_ratio,
        win_rate, max_drawdown, total_trades, total_pnl, viable.
    """
    ema_stop, ema_tp = _get_ema_config(db)
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    bias_tf  = _BIAS_TF.get(timeframe, "1d")

    logger.info(
        "Trail optimizer: fetching %s %s klines (%d days)…", symbol, timeframe, lookback_days
    )
    df = fetch_and_cache(symbol, timeframe, start_dt, end_dt)

    df_bias = None
    try:
        df_bias = fetch_and_cache(symbol, bias_tf, start_dt, end_dt)
        logger.info("Trail optimizer: fetched %s %s bias klines", symbol, bias_tf)
    except Exception as exc:
        logger.warning("Trail optimizer: could not fetch bias data (%s) — running without", exc)

    combos = list(product(ACTIVATION_GRID, TRAIL_GRID))
    total  = len(combos)
    logger.info(
        "Trail optimizer: %d combinations to test (EMA fixed: stop=%.2f tp=%.2f)",
        total, ema_stop, ema_tp,
    )

    results: list[dict] = []

    for idx, (activation_mult, trail_mult) in enumerate(combos):
        cfg = BacktestConfig(
            initial_capital=capital,
            risk_per_trade=risk_per_trade,
            timeframe=timeframe,
            cost_per_side_pct=cost_per_side,
            ema_stop_mult=ema_stop,
            ema_tp_mult=ema_tp,
            simulate_trailing=True,
            trail_activation_mult=activation_mult,
            trail_atr_mult=trail_mult,
        )
        engine = BacktestEngine(cfg)

        try:
            result  = engine.run(df.copy(), df_4h=df_bias, symbol=symbol)
            summary = engine.summary(result)
        except Exception as exc:
            logger.warning(
                "Trail optimizer: backtest failed act=%.2f trail=%.2f: %s",
                activation_mult, trail_mult, exc,
            )
            if on_progress:
                on_progress(idx + 1, total, activation_mult, trail_mult, None)
            continue

        viable = _is_viable(summary)
        row = {
            "activation_mult": activation_mult,
            "trail_mult":      trail_mult,
            "profit_factor":   summary["profit_factor"],
            "sharpe_ratio":    summary["sharpe_ratio"],
            "win_rate":        summary["win_rate_pct"],
            "max_drawdown":    summary["max_drawdown_pct"],
            "total_trades":    summary["total_trades"],
            "total_pnl":       summary["total_pnl"],
            "viable":          viable,
        }
        results.append(row)

        if on_progress:
            on_progress(idx + 1, total, activation_mult, trail_mult, summary if viable else None)

        logger.debug(
            "act=%.2f trail=%.2f → PF=%.2f WR=%.1f%% trades=%d viable=%s",
            activation_mult, trail_mult,
            summary["profit_factor"], summary["win_rate_pct"],
            summary["total_trades"], viable,
        )

    results.sort(key=lambda r: (r["viable"], r["profit_factor"]), reverse=True)

    for row in results:
        if not row["viable"]:
            continue
        db.insert_trail_run(
            symbol=symbol,
            timeframe=timeframe,
            period_days=lookback_days,
            trail_activation_mult=row["activation_mult"],
            trail_atr_mult=row["trail_mult"],
            ema_stop_mult=ema_stop,
            ema_tp_mult=ema_tp,
            profit_factor=row["profit_factor"],
            sharpe_ratio=row["sharpe_ratio"],
            win_rate=row["win_rate"],
            max_drawdown=row["max_drawdown"],
            total_trades=row["total_trades"],
            total_pnl=row["total_pnl"],
            status="pending",
        )

    viable_results = [r for r in results if r["viable"]]
    if viable_results:
        best = viable_results[0]
        logger.info(
            "Trail optimizer: %d viable configs found (best PF=%.2f act=%.2f trail=%.2f)",
            len(viable_results), best["profit_factor"],
            best["activation_mult"], best["trail_mult"],
        )
    else:
        logger.info("Trail optimizer: 0 viable configs found out of %d total", len(results))

    return results
