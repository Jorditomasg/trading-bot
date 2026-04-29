"""Entry filter parameter optimizer.

Runs a grid search over EMACrossover entry quality filter params using the
backtest engine on recent historical data. TP/SL are fixed at current approved
values. Results ranked by Profit Factor and saved to entry_quality_runs.
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

# ── Search space (4 × 2 × 2 × 3 × 5 = 240 combos) ───────────────────────────
VOL_GRID      = [0.0, 1.0, 1.5, 2.0]          # volume_multiplier; 0.0 = filter off
BAR_DIR_GRID  = [False, True]                  # require_bar_direction
MOMENTUM_GRID = [False, True]                  # require_ema_momentum
ATR_PCT_GRID  = [0.0, 0.003, 0.005]           # min_atr_pct; 0.0 = dead-market filter off
DIST_ATR_GRID = [0.3, 0.5, 0.8, 1.0, 1.5]    # max_distance_atr; controls pullback entry zone width

# Viability constraints — design is authoritative; MIN_TRADES = 15 (not 10 from spec)
# because entry filters reduce trade count and we need enough data to be confident.
MIN_TRADES       = 15
MAX_DRAWDOWN_PCT = 20.0
MIN_SHARPE       = 0.4
MIN_PF           = 1.05

# Bias timeframe per primary timeframe (mirrors trail_optimizer.py)
_BIAS_TF = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}

_DEFAULT_STOP_MULT = 1.5
_DEFAULT_TP_MULT   = 3.5


def _get_ema_tp_sl(db: Database) -> tuple[float, float]:
    """Return (ema_stop_mult, ema_tp_mult) from runtime config or defaults."""
    cfg = db.get_runtime_config()
    return float(cfg.get("ema_stop_mult", _DEFAULT_STOP_MULT)), float(cfg.get("ema_tp_mult", _DEFAULT_TP_MULT))


def _is_viable(summary: dict) -> bool:
    return (
        summary["total_trades"]         >= MIN_TRADES
        and summary["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT
        and summary["sharpe_ratio"]     >= MIN_SHARPE
        and summary["profit_factor"]    >= MIN_PF
    )


def run_entry_quality_grid_search(
    db: Database,
    symbol: str,
    timeframe: str,
    lookback_days: int = 270,
    cost_per_side: float = 0.0007,
    capital: float = 10_000.0,
    risk_per_trade: float = 0.01,
    on_progress: Callable[[int, int, float, bool, bool, float, float, dict | None], None] | None = None,
) -> list[dict]:
    """Run 240-combo entry filter grid search and persist viable results.

    Args:
        db:            Database instance for saving results.
        symbol:        Binance pair, e.g. "BTCUSDT".
        timeframe:     Primary candle interval, e.g. "1h".
        lookback_days: How many days of history to use.
        cost_per_side: Fee fraction per side (0.0007 = 0.07% maker).
        capital:       Starting capital for each simulation.
        risk_per_trade: Fraction of capital risked per trade.
        on_progress:   Optional callback(combo_idx, total, vol_mult, bar_dir,
                       momentum, min_atr, dist_atr, summary|None).

    Returns:
        List of result dicts sorted viable-first then by profit_factor DESC.
    """
    ema_stop, ema_tp = _get_ema_tp_sl(db)
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days)
    bias_tf  = _BIAS_TF.get(timeframe, "1d")

    logger.info(
        "Entry quality optimizer: fetching %s %s klines (%d days)…",
        symbol, timeframe, lookback_days,
    )
    df = fetch_and_cache(symbol, timeframe, start_dt, end_dt)

    df_bias = None
    try:
        df_bias = fetch_and_cache(symbol, bias_tf, start_dt, end_dt)
        logger.info("Entry quality optimizer: fetched %s %s bias klines", symbol, bias_tf)
    except Exception as exc:
        logger.warning(
            "Entry quality optimizer: bias fetch failed (%s) — running without", exc
        )

    combos = list(product(VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID, DIST_ATR_GRID))
    total  = len(combos)
    logger.info(
        "Entry quality optimizer: %d combinations (EMA fixed stop=%.2f tp=%.2f)",
        total, ema_stop, ema_tp,
    )

    results: list[dict] = []

    for idx, (vol_mult, bar_dir, momentum, min_atr, dist_atr) in enumerate(combos):
        cfg = BacktestConfig(
            initial_capital=capital,
            risk_per_trade=risk_per_trade,
            timeframe=timeframe,
            cost_per_side_pct=cost_per_side,
            ema_stop_mult=ema_stop,
            ema_tp_mult=ema_tp,
            ema_volume_mult=vol_mult,
            ema_require_bar_dir=bar_dir,
            ema_require_momentum=momentum,
            ema_min_atr_pct=min_atr,
            ema_max_distance_atr=dist_atr,
        )
        engine = BacktestEngine(cfg)

        try:
            result  = engine.run(df.copy(), df_4h=df_bias, symbol=symbol)
            summary = engine.summary(result)
        except Exception as exc:
            logger.warning(
                "Entry quality optimizer: failed vol=%.1f bar=%s mom=%s atr=%.3f dist=%.2f: %s",
                vol_mult, bar_dir, momentum, min_atr, dist_atr, exc,
            )
            if on_progress:
                on_progress(idx + 1, total, vol_mult, bar_dir, momentum, min_atr, dist_atr, None)
            continue

        viable = _is_viable(summary)
        row = {
            "vol_mult":         vol_mult,
            "bar_direction":    bar_dir,
            "ema_momentum":     momentum,
            "min_atr_pct":      min_atr,
            "max_distance_atr": dist_atr,
            "profit_factor":    summary["profit_factor"],
            "sharpe_ratio":     summary["sharpe_ratio"],
            "win_rate":         summary["win_rate_pct"],
            "max_drawdown":     summary["max_drawdown_pct"],
            "total_trades":     summary["total_trades"],
            "total_pnl":        summary["total_pnl"],
            "viable":           viable,
        }
        results.append(row)

        if on_progress:
            on_progress(idx + 1, total, vol_mult, bar_dir, momentum, min_atr, dist_atr, summary if viable else None)

        logger.debug(
            "vol=%.1f bar=%s mom=%s atr=%.3f dist=%.2f → PF=%.2f WR=%.1f%% trades=%d viable=%s",
            vol_mult, bar_dir, momentum, min_atr, dist_atr,
            summary["profit_factor"], summary["win_rate_pct"],
            summary["total_trades"], viable,
        )

    results.sort(key=lambda r: (r["viable"], r["profit_factor"]), reverse=True)

    for row in results:
        if not row["viable"]:
            continue
        db.insert_entry_quality_run(
            symbol=symbol,
            timeframe=timeframe,
            period_days=lookback_days,
            vol_mult=row["vol_mult"],
            bar_direction=row["bar_direction"],
            ema_momentum=row["ema_momentum"],
            min_atr_pct=row["min_atr_pct"],
            max_distance_atr=row["max_distance_atr"],
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

    viable_n = sum(1 for r in results if r["viable"])
    logger.info(
        "Entry quality optimizer: %d/%d viable configs found", viable_n, len(results)
    )
    return results
