"""Test Config D at different risk levels and timeframes."""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone
from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.constants import StrategyName
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy

START  = datetime(2025, 11, 2, tzinfo=timezone.utc)
END    = datetime(2026, 4, 21, tzinfo=timezone.utc)
MONTHS = 5.5
BIAS_MAP = {"4h": "1d", "2h": "4h", "1h": "4h"}

IMPROVED_EMA_4H = EMACrossoverConfig(
    max_distance_atr=0.3, tp_atr_mult=5.0, stop_atr_mult=1.5,
    volume_multiplier=1.5, min_atr_pct=0.005,
    require_bar_direction=True, require_ema_momentum=True,
)

def run(tf, risk, df, df_bias):
    cfg = BacktestConfig(
        timeframe=tf, risk_per_trade=risk,
        ema_tp_mult=5.0, trail_atr_mult=1.5, trail_activation_mult=2.0,
    )
    engine = BacktestEngine(cfg)
    # For 2h we rebuild EMA config with same quality filters
    ema_cfg = EMACrossoverConfig(
        max_distance_atr=0.3, tp_atr_mult=5.0, stop_atr_mult=1.5,
        volume_multiplier=1.5, min_atr_pct=0.005,
        require_bar_direction=True, require_ema_momentum=True,
    )
    engine._strategies[StrategyName.EMA_CROSSOVER] = EMACrossoverStrategy(ema_cfg)
    result  = engine.run(df, df_4h=df_bias, symbol="BTCUSDT")
    return engine.summary(result), result

def main():
    print("Loading data…")
    data = {}
    for tf, bias_tf in BIAS_MAP.items():
        df      = fetch_and_cache("BTCUSDT", tf,      START, END)
        df_bias = fetch_and_cache("BTCUSDT", bias_tf, START, END)
        data[tf] = (df, df_bias)
        print(f"  {tf}: {len(df):,} bars")

    print()
    print(f"{'Config':<28} {'Trades':>7} {'WR%':>6} {'PnL%':>7} {'AnnPnL%':>9} {'PF':>6} {'Sharpe':>7} {'MaxDD%':>7} {'Streak':>7}")
    print("─" * 90)

    for tf in ["4h", "2h", "1h"]:
        df, df_bias = data[tf]
        for risk in [0.01, 0.02, 0.03, 0.05]:
            s, r = run(tf, risk, df, df_bias)
            ann = s["total_pnl_pct"] * (12 / MONTHS)
            pf  = f"{s['profit_factor']:.3f}" if s['profit_factor'] != float('inf') else "∞"
            label = f"{tf}  risk={int(risk*100)}%"
            print(
                f"{label:<28} {s['total_trades']:>7} {s['win_rate_pct']:>5.1f}% "
                f"{s['total_pnl_pct']:>6.2f}% {ann:>8.1f}% "
                f"{pf:>6} {s['sharpe_ratio']:>7.2f} "
                f"{s['max_drawdown_pct']:>6.1f}% {s['max_loss_streak']:>7}"
            )
        print()

if __name__ == "__main__":
    main()
