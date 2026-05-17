"""Reproduce user's dashboard backtest exactly to understand PF=0.86.

User report (2026-05-15): BTC+ETH, 4h, risk=3%, 6mo, fee=0.07%, bias ON,
momentum ON → PF=0.86 in dashboard backtest.

Audit said: B-pick (SL=1.5, TP=5.0) at risk=1.5%, 10×3mo windows → PF mean 1.38.

This script reconstructs the dashboard's exact BacktestConfig and runs it on
the prod-aligned config to expose the gap: risk=3% vs 1.5%, single window vs
walk-forward, momentum band 0.08 vs 0.05.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

os.chdir("/mnt/c/Users/Jordi/PROYECTOS/trading-bot")

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine


def run_one(label: str, **overrides) -> None:
    """Run a single 6mo backtest with the given config overrides."""
    end_dt   = datetime(2026, 5, 15, tzinfo=timezone.utc)
    start_dt = end_dt - timedelta(days=180)

    dfs = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt),
    }
    dfs_bias = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt),
    }
    dfs_weekly = {
        "BTCUSDT": fetch_and_cache("BTCUSDT", "1w", start_dt, end_dt),
        "ETHUSDT": fetch_and_cache("ETHUSDT", "1w", start_dt, end_dt),
    }

    defaults = dict(
        initial_capital         = 10_000.0,
        timeframe               = "4h",
        long_only               = True,
        ema_stop_mult           = 1.5,
        ema_tp_mult             = 5.0,
        ema_max_distance_atr    = 1.0,
        momentum_filter_enabled = True,
        momentum_sma_period     = 20,
    )
    defaults.update(overrides)
    cfg = BacktestConfig(**defaults)

    engine = PortfolioBacktestEngine(cfg)
    res = engine.run_portfolio(dfs, dfs_4h=dfs_bias, dfs_weekly=dfs_weekly)
    s = res.portfolio_summary

    print(f"\n── {label} ──")
    print(f"  risk={cfg.risk_per_trade*100:.2f}%  cost={cfg.cost_per_side_pct*100:.3f}%  "
          f"momentum_band=±{cfg.momentum_neutral_band*100:.0f}%  kelly={cfg.kelly_enabled}")
    print(f"  PF={s['profit_factor']:.2f}  WR={s['win_rate_pct']:.1f}%  "
          f"n_trades={s['total_trades']}  DD={s['max_drawdown_pct']:.1f}%  "
          f"PnL={s['total_pnl_pct']:+.1f}%  Sharpe={s['sharpe_ratio']:.2f}")
    for sym in res.symbols:
        ps = res.per_symbol_summary.get(sym, {})
        if ps:
            print(f"    {sym}: PF={ps['profit_factor']:.2f}  WR={ps['win_rate_pct']:.1f}%  "
                  f"n={ps['total_trades']}  PnL={ps['total_pnl_pct']:+.1f}%")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("EXACT REPRO of user's dashboard backtest")
    print("BTC+ETH, 4h, risk=3%, fee=0.07%, bias ON, momentum ON, 6mo")
    print("=" * 70)
    # Dashboard's exact defaults from backtest_runner.py
    run_one(
        "User's backtest (exact dashboard params)",
        risk_per_trade        = 0.03,
        cost_per_side_pct     = 0.0007,
        momentum_neutral_band = 0.08,   # dashboard default
    )

    print("\n" + "=" * 70)
    print("VARIANTS — isolate which knob drives the bad PF")
    print("=" * 70)
    # Drop risk to audit-validated 1.5%
    run_one(
        "Same but risk=1.5% (audit-validated B-pick)",
        risk_per_trade        = 0.015,
        cost_per_side_pct     = 0.0007,
        momentum_neutral_band = 0.08,
    )
    # Audit's exact cost (0.10%) instead of 0.07%
    run_one(
        "risk=1.5%, audit's cost=0.10%",
        risk_per_trade        = 0.015,
        cost_per_side_pct     = 0.001,
        momentum_neutral_band = 0.05,
    )
    # Disable Kelly (which kicks in after 15 trades; could be hurting on a short window)
    run_one(
        "risk=3% but Kelly=OFF (flat-risk)",
        risk_per_trade        = 0.03,
        cost_per_side_pct     = 0.0007,
        momentum_neutral_band = 0.08,
        kelly_enabled         = False,
    )
