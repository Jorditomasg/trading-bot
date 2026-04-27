"""Auto entry-quality optimizer — runs the entry filter grid search on a schedule
and applies the best viable config automatically, without human approval.

Usage (from main.py):
    from bot.optimizer.auto_entry_quality_optimizer import should_run, run_and_apply

    if should_run(db):
        run_and_apply(db, symbol, timeframe, on_applied=callback)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from bot.database.db import Database
from bot.optimizer.entry_quality_optimizer import run_entry_quality_grid_search

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

OPTIMIZER_INTERVAL_DAYS = 7
LAST_RUN_KEY            = "last_auto_entry_quality_run"
_lock                   = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def should_run(db: Database, interval_days: int = OPTIMIZER_INTERVAL_DAYS) -> bool:
    """Return True if the optimizer has not run within *interval_days*."""
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
    lookback_days: int = 270,
    cost_per_side: float = 0.0007,
    capital: float = 10_000.0,
    risk_per_trade: float = 0.01,
    on_applied: Callable[[dict, dict], None] | None = None,
) -> tuple[dict, dict] | None:
    """Run the entry filter grid search and auto-apply the best viable config.

    Args:
        db:            Database instance.
        symbol:        Binance symbol, e.g. "BTCUSDT".
        timeframe:     Primary candle interval, e.g. "1h".
        lookback_days: Historical window for the backtest (default 270 days).
        cost_per_side: Fee fraction per side (default 0.07%).
        capital:       Starting capital for each backtest simulation.
        risk_per_trade: Fraction of capital risked per trade.
        on_applied:    Optional callback(old_params, new_params) called on config change.

    Returns:
        ``(old_params, new_params)`` when a better config was found and applied.
        ``None`` when no viable config was found or best config equals current.

    Thread-safety:
        Concurrent calls are skipped — only one run can execute at a time.
    """
    if not _lock.acquire(blocking=False):
        logger.info("Auto entry-quality optimizer: already running — skipping")
        return None

    try:
        now = datetime.now(tz=timezone.utc)
        logger.info(
            "Auto entry-quality optimizer: starting grid search (%s %s, %d-day lookback)",
            symbol, timeframe, lookback_days,
        )

        results = run_entry_quality_grid_search(
            db=db,
            symbol=symbol,
            timeframe=timeframe,
            lookback_days=lookback_days,
            cost_per_side=cost_per_side,
            capital=capital,
            risk_per_trade=risk_per_trade,
        )

        db.set_runtime_config(**{LAST_RUN_KEY: now.isoformat()})

        viable = [r for r in results if r["viable"]]
        if not viable:
            logger.info("Auto entry-quality optimizer: no viable configs found — keeping current params")
            return None

        best = viable[0]

        current_cfg = db.get_runtime_config()
        old_params = {
            "ema_vol_mult": float(current_cfg.get("ema_vol_mult", 0.0)),
            "ema_bar_dir":  current_cfg.get("ema_bar_dir", "false") == "true",
            "ema_momentum": current_cfg.get("ema_momentum", "false") == "true",
            "ema_min_atr":  float(current_cfg.get("ema_min_atr", 0.0)),
        }
        new_params = {
            "ema_vol_mult":   best["vol_mult"],
            "ema_bar_dir":    best["bar_direction"],
            "ema_momentum":   best["ema_momentum"],
            "ema_min_atr":    best["min_atr_pct"],
            "profit_factor":  best["profit_factor"],
            "sharpe_ratio":   best["sharpe_ratio"],
            "win_rate":       best["win_rate"],
            "max_drawdown":   best["max_drawdown"],
            "total_trades":   best["total_trades"],
        }

        # Skip update if nothing changed
        if (
            old_params["ema_vol_mult"] == best["vol_mult"]
            and old_params["ema_bar_dir"]  == best["bar_direction"]
            and old_params["ema_momentum"] == best["ema_momentum"]
            and old_params["ema_min_atr"]  == best["min_atr_pct"]
        ):
            logger.info(
                "Auto entry-quality optimizer: best config unchanged — no update needed"
            )
            return None

        db.set_runtime_config(
            ema_vol_mult=str(best["vol_mult"]),
            ema_bar_dir="true" if best["bar_direction"] else "false",
            ema_momentum="true" if best["ema_momentum"] else "false",
            ema_min_atr=str(best["min_atr_pct"]),
        )

        # Promote matching pending run to 'auto_applied'
        pending = db.get_best_pending_entry_quality_run()
        if (
            pending
            and abs(pending["vol_mult"]    - best["vol_mult"])   < 1e-6
            and pending["bar_direction"]  == best["bar_direction"]
            and pending["ema_momentum"]   == best["ema_momentum"]
            and abs(pending["min_atr_pct"] - best["min_atr_pct"]) < 1e-6
        ):
            db.set_entry_quality_run_status(pending["id"], "auto_applied")

        logger.info(
            "Auto entry-quality optimizer: applied — "
            "vol=%.1f bar=%s mom=%s atr=%.3f  PF=%.2f Sharpe=%.2f WR=%.1f%% trades=%d",
            best["vol_mult"], best["bar_direction"], best["ema_momentum"], best["min_atr_pct"],
            best["profit_factor"], best["sharpe_ratio"], best["win_rate"], best["total_trades"],
        )

        if on_applied:
            on_applied(old_params, new_params)

        return old_params, new_params

    finally:
        _lock.release()
