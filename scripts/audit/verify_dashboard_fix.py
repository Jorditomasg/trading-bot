"""Verify the Phase 1 follow-up fix: dashboard BacktestConfig now matches live.

Builds the EXACT BacktestConfig the dashboard would build (after the fix),
runs BTC-only 6mo (the user's failing case), and prints PF/WR/trades.

Pre-fix:  PF=0.90, WR=25%, 16 trades  (user's dashboard)
Post-fix (expected): PF≈1.79, WR≈36%, ≈11 trades (matches C3_LIVE)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine

REPORT_END   = datetime(2026, 5, 17, tzinfo=timezone.utc)
SIX_MO_START = REPORT_END - timedelta(days=180)


def _dashboard_config_after_fix(cfg_rt: dict) -> BacktestConfig:
    """Replicates dashboard/sections/backtest_runner.py:_run_portfolio_backtest
    BacktestConfig construction AFTER the 2026-05-17 fix.
    """
    return BacktestConfig(
        initial_capital         = 10_000.0,
        risk_per_trade          = float(cfg_rt.get("risk_per_trade", 0.015)),
        timeframe               = "4h",
        cost_per_side_pct       = float(cfg_rt.get("backtest_cost_per_side", 0.001)),
        momentum_filter_enabled = True,
        momentum_sma_period     = 20,
        momentum_neutral_band   = 0.08,
        long_only               = cfg_rt.get("long_only", "true") == "true",
        ema_stop_mult           = float(cfg_rt.get("ema_stop_mult", 1.5)),
        ema_tp_mult             = float(cfg_rt.get("ema_tp_mult", 5.0)),
        ema_max_distance_atr    = float(cfg_rt.get("ema_max_dist_atr", 1.0)),
        ema_volume_mult         = float(cfg_rt.get("ema_vol_mult", 1.5)),
        ema_require_momentum    = cfg_rt.get("ema_momentum_req", "true") == "true",
        ema_require_bar_dir     = cfg_rt.get("ema_bar_dir", "true") == "true",
        ema_min_atr_pct         = float(cfg_rt.get("ema_min_atr", 0.005)),
    )


def main() -> int:
    # Two scenarios:
    #   (1) empty DB (all-fallback path) — the realistic user case
    #   (2) full seed (after _seed_optimized_defaults runs)
    print("\n" + "═" * 72)
    print(" VERIFY — dashboard BacktestConfig after 2026-05-17 fix")
    print("═" * 72)

    df_4h = fetch_and_cache("BTCUSDT", "4h",
                            SIX_MO_START - timedelta(days=20), REPORT_END)
    df_4h = df_4h[df_4h["open_time"] >= SIX_MO_START].reset_index(drop=True)
    df_1d = fetch_and_cache("BTCUSDT", "1d",
                            SIX_MO_START - timedelta(days=20), REPORT_END)
    df_1w = fetch_and_cache("BTCUSDT", "1w",
                            SIX_MO_START - timedelta(days=40), REPORT_END)

    cases = {
        "(1) Fully-empty DB (only fallbacks fire)": {},
        "(2) Seeded DB (matches live)": {
            "risk_per_trade":          "0.015",
            "ema_stop_mult":           "1.5",
            "ema_tp_mult":             "5.0",
            "ema_max_dist_atr":        "1.0",
            "ema_vol_mult":            "1.5",
            "ema_bar_dir":             "true",
            "ema_momentum_req":        "true",
            "ema_min_atr":             "0.005",
            "long_only":               "true",
            "backtest_cost_per_side":  "0.001",
        },
    }

    for label, cfg_rt in cases.items():
        cfg = _dashboard_config_after_fix(cfg_rt)
        engine = BacktestEngine(cfg)
        res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w)
        s = engine.summary(res)
        print(f"\n{label}")
        print(f"  ema_min_atr_pct={cfg.ema_min_atr_pct:.4f}  ema_volume_mult={cfg.ema_volume_mult}  "
              f"ema_require_momentum={cfg.ema_require_momentum}  "
              f"ema_require_bar_dir={cfg.ema_require_bar_dir}")
        print(f"  PF={s['profit_factor']:.2f}  WR={s['win_rate_pct']:.1f}%  "
              f"trades={s['total_trades']}  DD={s['max_drawdown_pct']:.1f}%  "
              f"PnL={s['total_pnl_pct']:+.2f}%  Sharpe={s['sharpe_ratio']:.2f}")

    print("\n" + "═" * 72)
    print(" User reported (PRE-FIX): PF=0.90  WR=25.0%  trades=16  DD=6.5%  PnL=-1.5%")
    print("═" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
