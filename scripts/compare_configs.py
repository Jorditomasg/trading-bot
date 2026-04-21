"""
Compare baseline vs improved backtest configurations.
Usage: .venv/bin/python scripts/compare_configs.py
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from datetime import datetime, timezone

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.constants import StrategyName
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy

START  = datetime(2025, 11, 2,  tzinfo=timezone.utc)
END    = datetime(2026, 4, 21,  tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
TF     = "4h"
BIAS   = "1d"

CONFIGS = {
    "A — Baseline": {
        "bt": BacktestConfig(
            timeframe=TF, cost_per_side_pct=0.0007,
            ema_tp_mult=3.5,
            trail_atr_mult=1.5, trail_activation_mult=1.0,
        ),
        "ema": EMACrossoverConfig(
            max_distance_atr=1.0, tp_atr_mult=3.5,
            volume_multiplier=0.0, min_atr_pct=0.0,
            require_bar_direction=False, require_ema_momentum=False,
        ),
    },
    "B — Better trail only": {
        "bt": BacktestConfig(
            timeframe=TF, cost_per_side_pct=0.0007,
            ema_tp_mult=5.0,
            trail_atr_mult=1.5, trail_activation_mult=2.0,
        ),
        "ema": EMACrossoverConfig(
            max_distance_atr=1.0, tp_atr_mult=5.0,
            volume_multiplier=0.0, min_atr_pct=0.0,
            require_bar_direction=False, require_ema_momentum=False,
        ),
    },
    "C — Filters only (no trail change)": {
        "bt": BacktestConfig(
            timeframe=TF, cost_per_side_pct=0.0007,
            ema_tp_mult=3.5,
            trail_atr_mult=1.5, trail_activation_mult=1.0,
        ),
        "ema": EMACrossoverConfig(
            max_distance_atr=0.3, tp_atr_mult=3.5,
            volume_multiplier=1.5, min_atr_pct=0.005,
            require_bar_direction=True, require_ema_momentum=True,
        ),
    },
    "D — Full improvement": {
        "bt": BacktestConfig(
            timeframe=TF, cost_per_side_pct=0.0007,
            ema_tp_mult=5.0,
            trail_atr_mult=1.5, trail_activation_mult=2.0,
        ),
        "ema": EMACrossoverConfig(
            max_distance_atr=0.3, tp_atr_mult=5.0,
            volume_multiplier=1.5, min_atr_pct=0.005,
            require_bar_direction=True, require_ema_momentum=True,
        ),
    },
}

def run(label: str, bt_cfg: BacktestConfig, ema_cfg: EMACrossoverConfig, df, df_bias) -> dict:
    engine = BacktestEngine(bt_cfg)
    engine._strategies[StrategyName.EMA_CROSSOVER] = EMACrossoverStrategy(ema_cfg)
    result  = engine.run(df, df_4h=df_bias, symbol=SYMBOL)
    summary = engine.summary(result)
    return summary

def bar(value: float, baseline: float) -> str:
    delta = value - baseline
    sign  = "+" if delta >= 0 else ""
    return f"{sign}{delta:+.2f}" if delta != 0 else " ="

def main():
    print("Loading data from cache…")
    df      = fetch_and_cache(SYMBOL, TF,   START, END)
    df_bias = fetch_and_cache(SYMBOL, BIAS, START, END)
    print(f"Primary: {len(df):,} bars   Bias: {len(df_bias):,} bars\n")

    results = {}
    for label, cfg in CONFIGS.items():
        s = run(label, cfg["bt"], cfg["ema"], df, df_bias)
        results[label] = s
        print(f"✓ {label}")

    # ── Print comparison table ─────────────────────────────────────────────
    base = results["A — Baseline"]
    keys = [
        ("total_trades",      "Trades",           "{:.0f}"),
        ("win_rate_pct",      "Win rate %",        "{:.1f}%"),
        ("total_pnl",         "Net PnL $",         "${:.0f}"),
        ("total_pnl_pct",     "Net PnL %",         "{:.2f}%"),
        ("profit_factor",     "Profit Factor",     "{:.3f}"),
        ("sharpe_ratio",      "Sharpe",            "{:.2f}"),
        ("max_drawdown_pct",  "Max DD %",          "{:.1f}%"),
        ("max_loss_streak",   "Loss streak",       "{:.0f}"),
    ]

    col_w = 26
    header = f"{'Metric':<22}" + "".join(f"{l:<{col_w}}" for l in results)
    print("\n" + "═" * len(header))
    print(header)
    print("─" * len(header))

    for key, label, fmt in keys:
        row = f"{label:<22}"
        for i, (cfg_label, s) in enumerate(results.items()):
            raw = s[key] if s[key] != float("inf") else 999.0
            val = fmt.format(raw)
            if i > 0:
                b_raw = base[key] if base[key] != float("inf") else 999.0
                delta = raw - b_raw
                sign  = "+" if delta > 0 else ""
                delta_str = f"  ({sign}{delta:.1f})" if abs(delta) > 0.005 else "  (=)"
                val += delta_str
            row += f"{val:<{col_w}}"
        print(row)

    print("═" * len(header))

    # ── Exit reason breakdown per config ──────────────────────────────────
    print("\n── Exit reasons ──────────────────────────────────────────────")
    for label, cfg in CONFIGS.items():
        engine = BacktestEngine(cfg["bt"])
        engine._strategies[StrategyName.EMA_CROSSOVER] = EMACrossoverStrategy(cfg["ema"])
        result = engine.run(df, df_4h=df_bias, symbol=SYMBOL)
        from collections import Counter
        reasons = Counter(t["exit_reason"] for t in result.trades)
        trail   = [t for t in result.trades if t["exit_reason"] == "TRAILING_STOP"]
        trail_losses = sum(1 for t in trail if t["pnl"] < 0)
        print(f"\n{label}")
        for r, c in sorted(reasons.items()):
            trades_r = [t for t in result.trades if t["exit_reason"] == r]
            avg_pnl  = sum(t["pnl"] for t in trades_r) / c
            print(f"  {r:<22} {c:>3}  avg ${avg_pnl:>8.1f}")
        if trail:
            print(f"  └ trail losses: {trail_losses}/{len(trail)}")


if __name__ == "__main__":
    main()
