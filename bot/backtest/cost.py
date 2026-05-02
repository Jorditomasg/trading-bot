"""Backtest cost helper — single source of truth for fee/slippage assumption.

Production fees are charged automatically by Binance and are NOT a bot setting.
This module only matters for SIMULATIONS (backtest, optimizer, research scripts).

Resolution order:
    1. Database `bot_config` runtime config key `backtest_cost_per_side` (editable from dashboard CONFIG tab).
    2. Hardcoded fallback FALLBACK_COST_PER_SIDE (0.001 = 0.10%, Binance VIP-0).

Reference fee tiers (Binance Spot, no BNB discount):
    VIP-0:   0.100% maker / 0.100% taker
    VIP-1:   0.090% / 0.100%   (>$1M 30d volume)
    VIP-9:   0.020% / 0.040%
With BNB pay (-25%): subtract 0.025pp from each side.
"""

from __future__ import annotations

import logging

FALLBACK_COST_PER_SIDE: float = 0.001   # 0.10% — Binance VIP-0 default

logger = logging.getLogger(__name__)


def resolve_cost_per_side(db_path: str | None = None) -> float:
    """Read `backtest_cost_per_side` from the bot DB. Fallback to constant if unavailable.

    Use this from scripts that don't already hold a Database handle.
    """
    if db_path is None:
        # Default to the live bot's DB path. Lazy import to avoid circular deps.
        from bot.config import settings  # noqa: PLC0415
        import os                        # noqa: PLC0415
        db_path = os.getenv("DB_PATH", "trading_bot.db")
        _ = settings   # silence unused (kept for future use)

    try:
        from bot.database.db import Database  # noqa: PLC0415
        db = Database(db_path)
        return db.get_backtest_cost_per_side()
    except Exception as exc:
        logger.debug("resolve_cost_per_side fell back to constant (%s): %s",
                     FALLBACK_COST_PER_SIDE, exc)
        return FALLBACK_COST_PER_SIDE
