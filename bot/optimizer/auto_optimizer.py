"""Auto-optimizer — runs the walk-forward grid search on a schedule and applies
the best viable EMA config automatically, without human approval.

Usage (from main.py):
    from bot.optimizer.auto_optimizer import should_run, run_and_apply

    if should_run(db):
        run_and_apply(db, symbol, timeframe, on_applied=callback)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from bot.database.db import Database
from bot.optimizer.walk_forward import run_grid_search

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

OPTIMIZER_INTERVAL_DAYS = 7          # run once per week
LAST_RUN_KEY            = "last_auto_optimizer_run"
_lock                   = threading.Lock()  # prevents concurrent runs


# ── Public API ────────────────────────────────────────────────────────────────

def should_run(db: Database, interval_days: int = OPTIMIZER_INTERVAL_DAYS) -> bool:
    """Return True if the auto-optimizer has not run within *interval_days*."""
    cfg    = db.get_runtime_config()
    ts_str = cfg.get(LAST_RUN_KEY)
    if not ts_str:
        return True
    try:
        last = datetime.fromisoformat(ts_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return datetime.now(tz=timezone.utc) - last >= timedelta(days=interval_days)
    except ValueError:
        return True


def run_and_apply(
    db: Database,
    symbol: str,
    timeframe: str,
    lookback_days: int = 180,
    cost_per_side: float = 0.0007,
    capital: float = 10_000.0,
    risk_per_trade: float = 0.01,
    on_applied: Callable[[dict, dict], None] | None = None,
) -> tuple[dict, dict] | None:
    """Run the grid search and auto-apply the best viable EMA config.

    Args:
        db:            Database instance.
        symbol:        Binance symbol, e.g. "BTCUSDT".
        timeframe:     Primary candle interval, e.g. "4h".
        lookback_days: Historical window for the backtest (default 180 days).
        cost_per_side: Fee fraction per side (default 0.07% — conservative maker estimate).
        capital:       Starting capital for each backtest simulation.
        risk_per_trade: Fraction of capital risked per trade.
        on_applied:    Optional callback(old_params, new_params) called when config changes.
                       Called from within this function before returning.

    Returns:
        ``(old_params, new_params)`` tuple when a better config was found and applied.
        ``None`` when no viable config was found or when the best config equals current.

    Thread-safety:
        Concurrent calls are skipped — only one run can execute at a time.
    """
    if not _lock.acquire(blocking=False):
        logger.info("Auto-optimizer: already running — skipping this trigger")
        return None

    try:
        now = datetime.now(tz=timezone.utc)
        logger.info(
            "Auto-optimizer: starting grid search (%s %s, %d-day lookback)",
            symbol, timeframe, lookback_days,
        )

        results = run_grid_search(
            db=db,
            symbol=symbol,
            timeframe=timeframe,
            lookback_days=lookback_days,
            cost_per_side=cost_per_side,
            capital=capital,
            risk_per_trade=risk_per_trade,
        )

        # Always record that we ran, even when no viable result exists
        db.set_runtime_config(**{LAST_RUN_KEY: now.isoformat()})

        viable = [r for r in results if r["viable"]]
        if not viable:
            logger.info("Auto-optimizer: no viable configs found — keeping current params")
            return None

        best = viable[0]  # sorted by profit_factor DESC in run_grid_search

        current_cfg  = db.get_runtime_config()
        current_stop = float(current_cfg.get("ema_stop_mult", 1.5))
        current_tp   = float(current_cfg.get("ema_tp_mult",   3.5))

        old_params = {
            "ema_stop_mult": current_stop,
            "ema_tp_mult":   current_tp,
        }
        new_params = {
            "ema_stop_mult": best["stop_mult"],
            "ema_tp_mult":   best["tp_mult"],
            "profit_factor": best["profit_factor"],
            "sharpe_ratio":  best["sharpe_ratio"],
            "win_rate":      best["win_rate"],
            "max_drawdown":  best["max_drawdown"],
            "total_trades":  best["total_trades"],
        }

        if best["stop_mult"] == current_stop and best["tp_mult"] == current_tp:
            logger.info(
                "Auto-optimizer: best config unchanged "
                "(SL=%.2f TP=%.2f PF=%.2f) — no update needed",
                best["stop_mult"], best["tp_mult"], best["profit_factor"],
            )
            return None

        # Write new params to DB
        db.set_runtime_config(
            ema_stop_mult=str(best["stop_mult"]),
            ema_tp_mult=str(best["tp_mult"]),
        )

        # Promote the matching pending run to 'auto_applied'
        pending = db.get_best_pending_optimizer_run()
        if (
            pending
            and abs(pending["ema_stop_mult"] - best["stop_mult"]) < 1e-6
            and abs(pending["ema_tp_mult"]   - best["tp_mult"])   < 1e-6
        ):
            db.set_optimizer_run_status(pending["id"], "auto_applied")

        logger.info(
            "Auto-optimizer: applied — SL %.2f→%.2f  TP %.2f→%.2f  "
            "PF=%.2f  Sharpe=%.2f  WR=%.1f%%  trades=%d",
            current_stop, best["stop_mult"],
            current_tp,   best["tp_mult"],
            best["profit_factor"], best["sharpe_ratio"],
            best["win_rate"],      best["total_trades"],
        )

        if on_applied:
            on_applied(old_params, new_params)

        return old_params, new_params

    finally:
        _lock.release()
