"""Reproduce the OLD dashboard BacktestConfig (pre-fix) with user's exact params.

Goal: verify that the user's PF 0.94 BTC / 1.20 ETH numbers come from the OLD
dashboard code running default BacktestConfig (bidirectional, SL=1.5, TP=4.5,
no entry-quality filters) — proving they're not running the fix yet.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

os.chdir("/mnt/c/Users/Jordi/PROYECTOS/trading-bot")

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine


def _run(label: str, cfg_extras: dict) -> None:
    end_dt = datetime(2026, 5, 8, tzinfo=timezone.utc)  # cache end
    start_dt = end_dt - timedelta(days=180)

    dfs = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt),
    }
    dfs_4h = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt),
    }
    dfs_weekly = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "1w", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "1w", start_dt, end_dt),
    }

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.03,
        timeframe="4h",
        cost_per_side_pct=0.0007,  # user's 0.07%
        momentum_filter_enabled=True,
        momentum_sma_period=20,
        momentum_neutral_band=0.08,  # dashboard default
        **cfg_extras,
    )
    engine = PortfolioBacktestEngine(cfg)
    result = engine.run_portfolio(dfs, dfs_4h=dfs_4h, dfs_weekly=dfs_weekly)
    s = result.portfolio_summary

    print(f"\n=== {label} ===")
    print(f"Global: PnL ${s['total_pnl']:+,.0f} ({s['total_pnl_pct']:+.1f}%)  "
          f"PF {s['profit_factor']:.2f}  WR {s['win_rate_pct']:.1f}%  "
          f"n={s['total_trades']}  DD {s['max_drawdown_pct']:.1f}%")
    for sym in result.symbols:
        ps = result.per_symbol_summary.get(sym, {})
        if ps:
            print(f"  {sym}: PnL ${ps['total_pnl']:+,.0f} ({ps['total_pnl_pct']:+.1f}%)  "
                  f"PF {ps['profit_factor']:.2f}  WR {ps['win_rate_pct']:.1f}%  "
                  f"n={ps['total_trades']}  DD {ps['max_drawdown_pct']:.1f}%")


if __name__ == "__main__":
    # OLD dashboard: no fix → uses BacktestConfig dataclass defaults
    # → long_only=False, ema_stop_mult=1.5, ema_tp_mult=4.5, no entry-quality
    _run("OLD dashboard code (pre-fix)", {})

    # NEW dashboard (after my fix): propagates runtime config
    _run("NEW dashboard code (post-fix)", {
        "long_only": True,
        "ema_stop_mult": 1.25,
        "ema_tp_mult": 3.5,
        "ema_max_distance_atr": 1.0,
        "ema_volume_mult": 2.0,
        "ema_require_momentum": True,
        "ema_require_bar_dir": False,
        "ema_min_atr_pct": 0.0,
    })
