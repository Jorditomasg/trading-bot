"""Walk-forward parameter optimizer.

Runs a grid search over EMA strategy TP/SL ATR multipliers using the
backtest engine on recent historical data.  Results are ranked by Profit
Factor and saved to the optimizer_runs DB table for human review.

Constraints applied (all must be met for a run to be viable):
  - total_trades  >= MIN_TRADES
  - max_drawdown  <= MAX_DRAWDOWN_PCT
  - sharpe_ratio  >= MIN_SHARPE
  - profit_factor >= MIN_PF
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
STOP_GRID    = [1.0, 1.25, 1.5, 1.75, 2.0]
TP_GRID      = [2.5, 3.0, 3.5, 4.0, 4.5, 5.0]

# Viability constraints
MIN_TRADES       = 15
MAX_DRAWDOWN_PCT = 20.0
MIN_SHARPE       = 0.4
MIN_PF           = 1.05

# Bias timeframe per primary timeframe
_BIAS_TF = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}


def _is_viable(summary: dict) -> bool:
    return (
        summary["total_trades"]   >= MIN_TRADES
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
        and summary["sharpe_ratio"]     >= MIN_SHARPE
        and summary["profit_factor"]    >= MIN_PF
    )


def run_grid_search(
    db: Database,
    symbol: str,
    timeframe: str,
    lookback_days: int = 180,
    cost_per_side: float = 0.0007,
    capital: float = 10_000.0,
    risk_per_trade: float = 0.01,
    on_progress: Callable[[int, int, float, float, dict | None], None] | None = None,
) -> list[dict]:
    """Run a full TP/SL grid search on recent data.

    Args:
        db:            Database instance for saving results.
        symbol:        Binance pair, e.g. "BTCUSDT".
        timeframe:     Primary candle interval, e.g. "4h".
        lookback_days: How many days of history to use.
        cost_per_side: Fee fraction per side (0.0007 = 0.07% maker).
        capital:       Starting capital for each simulation.
        risk_per_trade: Fraction of capital risked per trade.
        on_progress:   Optional callback(combo_idx, total, stop, tp, summary|None).

    Returns:
        List of viable result dicts sorted by profit_factor DESC.
        Each dict has: stop_mult, tp_mult, profit_factor, sharpe_ratio,
        win_rate, max_drawdown, total_trades, total_pnl, viable.
    """
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    bias_tf  = _BIAS_TF.get(timeframe, "1d")

    logger.info(
        "Optimizer: fetching %s %s klines (%d days)…", symbol, timeframe, lookback_days
    )
    df = fetch_and_cache(symbol, timeframe, start_dt, end_dt)

    df_bias = None
    try:
        df_bias = fetch_and_cache(symbol, bias_tf, start_dt, end_dt)
        logger.info("Optimizer: fetched %s %s bias klines", symbol, bias_tf)
    except Exception as exc:
        logger.warning("Optimizer: could not fetch bias data (%s) — running without", exc)

    combos = list(product(STOP_GRID, TP_GRID))
    total  = len(combos)
    logger.info("Optimizer: %d combinations to test", total)

    results: list[dict] = []

    for idx, (stop_mult, tp_mult) in enumerate(combos):
        # Skip invalid R:R (TP must be at least 1.5× SL)
        if tp_mult / stop_mult < 1.5:
            if on_progress:
                on_progress(idx + 1, total, stop_mult, tp_mult, None)
            continue

        cfg = BacktestConfig(
            initial_capital=capital,
            risk_per_trade=risk_per_trade,
            timeframe=timeframe,
            cost_per_side_pct=cost_per_side,
            ema_stop_mult=stop_mult,
            ema_tp_mult=tp_mult,
            simulate_trailing=True,
        )
        engine = BacktestEngine(cfg)

        try:
            result  = engine.run(df.copy(), df_4h=df_bias, symbol=symbol)
            summary = engine.summary(result)
        except Exception as exc:
            logger.warning(
                "Optimizer: backtest failed stop=%.2f tp=%.2f: %s", stop_mult, tp_mult, exc
            )
            if on_progress:
                on_progress(idx + 1, total, stop_mult, tp_mult, None)
            continue

        viable = _is_viable(summary)
        row = {
            "stop_mult":    stop_mult,
            "tp_mult":      tp_mult,
            "profit_factor": summary["profit_factor"],
            "sharpe_ratio":  summary["sharpe_ratio"],
            "win_rate":      summary["win_rate_pct"],
            "max_drawdown":  summary["max_drawdown_pct"],
            "total_trades":  summary["total_trades"],
            "total_pnl":     summary["total_pnl"],
            "viable":        viable,
        }
        results.append(row)

        if on_progress:
            on_progress(idx + 1, total, stop_mult, tp_mult, summary if viable else None)

        logger.debug(
            "stop=%.2f tp=%.2f → PF=%.2f WR=%.1f%% trades=%d viable=%s",
            stop_mult, tp_mult,
            summary["profit_factor"], summary["win_rate_pct"],
            summary["total_trades"], viable,
        )

    # Sort viable results first, then by profit_factor DESC
    results.sort(key=lambda r: (r["viable"], r["profit_factor"]), reverse=True)

    # Persist all viable results to DB
    for row in results:
        if not row["viable"]:
            continue
        db.insert_optimizer_run(
            symbol=symbol,
            timeframe=timeframe,
            period_days=lookback_days,
            ema_stop_mult=row["stop_mult"],
            ema_tp_mult=row["tp_mult"],
            profit_factor=row["profit_factor"],
            sharpe_ratio=row["sharpe_ratio"],
            win_rate=row["win_rate"],
            max_drawdown=row["max_drawdown"],
            total_trades=row["total_trades"],
            total_pnl=row["total_pnl"],
            status="pending",
        )

    logger.info(
        "Optimizer: %d viable configs found (best PF=%.2f stop=%.2f tp=%.2f)",
        sum(1 for r in results if r["viable"]),
        results[0]["profit_factor"] if results else 0.0,
        results[0]["stop_mult"] if results else 0.0,
        results[0]["tp_mult"] if results else 0.0,
    )

    return results
