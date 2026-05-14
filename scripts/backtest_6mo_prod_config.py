"""Verify whether last 6 months on BTC+ETH still produce the validated baseline returns.

Uses the EXACT production runtime config snapshot from `bot_config` at the time the
prod DB was copied to Desktop. Runs two variants: with and without momentum filter.

Outputs portfolio + per-symbol metrics.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

os.chdir("/mnt/c/Users/Jordi/PROYECTOS/trading-bot")

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine


# Production runtime config (from prod_snapshot.db bot_config keys, May 2026)
PROD = dict(
    long_only=True,
    ema_stop_mult=1.25,
    ema_tp_mult=3.5,
    ema_max_distance_atr=1.0,
    ema_volume_mult=2.0,
    ema_require_momentum=True,
    ema_require_bar_dir=False,
    ema_min_atr_pct=0.0,
    risk_per_trade=0.03,
)


def _run(momentum: bool, label: str) -> None:
    end_dt = datetime(2026, 5, 8, tzinfo=timezone.utc)  # match cache end
    start_dt = end_dt - timedelta(days=180)

    dfs = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt),
    }
    dfs_4h = {  # bias_tf for primary=4h is 1d
        "BTCUSDT": fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt),
    }
    dfs_weekly = (
        {
            "BTCUSDT": fetch_and_cache("BTCUSDT", "1w", start_dt, end_dt),
            "ETHUSDT": fetch_and_cache("ETHUSDT", "1w", start_dt, end_dt),
        }
        if momentum
        else None
    )

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        timeframe="4h",
        cost_per_side_pct=0.001,  # 0.10% (production rt_backtest_cost_per_side)
        momentum_filter_enabled=momentum,
        momentum_sma_period=20,
        momentum_neutral_band=0.05,
        **PROD,
    )
    engine = PortfolioBacktestEngine(cfg)
    result = engine.run_portfolio(dfs, dfs_4h=dfs_4h, dfs_weekly=dfs_weekly)

    s = result.portfolio_summary

    days = (end_dt - start_dt).days
    annual_pct = (s["total_pnl_pct"] / days) * 365

    print(f"\n=== {label} ===")
    print(f"Window:      {start_dt.date()} → {end_dt.date()}  ({days} days)")
    print(f"Symbols:     BTC + ETH (shared capital pool)")
    print(f"Capital:     ${result.initial_capital:,.0f}")
    print(f"Net PnL:     ${s['total_pnl']:+,.0f}  ({s['total_pnl_pct']:+.1f}%)")
    print(f"Annualized:  {annual_pct:+.1f}%")
    print(f"Profit Factor: {s['profit_factor']:.2f}")
    print(f"Win Rate:    {s['win_rate_pct']:.1f}%  ({s['total_trades']} trades)")
    print(f"Sharpe:      {s['sharpe_ratio']:.2f}")
    print(f"Max DD:      {s['max_drawdown_pct']:.1f}%")
    print(f"Calmar:      {(annual_pct / abs(s['max_drawdown_pct'])) if s['max_drawdown_pct'] else float('nan'):.2f}")

    for sym in result.symbols:
        ps = result.per_symbol_summary.get(sym, {})
        if ps:
            print(
                f"  {sym}: PnL ${ps.get('total_pnl', 0):+,.0f}  "
                f"({ps.get('total_pnl_pct', 0):+.1f}%)  "
                f"PF {ps.get('profit_factor', 0):.2f}  "
                f"WR {ps.get('win_rate_pct', 0):.1f}%  "
                f"n={ps.get('total_trades', 0)}  "
                f"DD {ps.get('max_drawdown_pct', 0):.1f}%"
            )


if __name__ == "__main__":
    _run(momentum=False, label="Production config, momentum OFF")
    _run(momentum=True,  label="Production config, momentum ON")
