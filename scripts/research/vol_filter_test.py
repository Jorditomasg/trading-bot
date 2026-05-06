"""Quick test: would a realized-vol filter have saved us in 2025-05→11?

For each bar, compute realized vol (50-bar log returns annualized).
Classify bar as: HIGH_VOL (>=40%) or LOW_VOL (<40%).
Aggregate trade outcomes by entry-vol regime.

If hypothesis is right: trades entered in LOW_VOL regime should have terrible WR/PF.
"""

from __future__ import annotations

import logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)

import numpy as np
import pandas as pd

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return


VOL_LOOKBACK = 50  # bars (50 × 4h = 200h ≈ 8 days)
VOL_THRESHOLD_PCT = 40.0  # annualized %


def realized_vol_pct(closes: np.ndarray, idx: int, lookback: int = VOL_LOOKBACK) -> float:
    """Annualized realized vol % at bar idx using last `lookback` bars."""
    if idx < lookback:
        return float("nan")
    window = closes[idx - lookback : idx + 1]
    log_ret = np.diff(np.log(window))
    if len(log_ret) == 0:
        return 0.0
    bars_per_year = 6 * 365  # 4h
    return float(log_ret.std() * np.sqrt(bars_per_year) * 100)


def main() -> None:
    df_4h = pd.read_parquet("data/klines/BTCUSDT_4h.parquet")
    df_1d = pd.read_parquet("data/klines/BTCUSDT_1d.parquet")

    # Slice to last 2 years for max statistical power
    s = pd.Timestamp("2024-05-01", tz="UTC")
    e = pd.Timestamp("2026-05-04", tz="UTC")
    mask4 = (df_4h["open_time"] >= s) & (df_4h["open_time"] < e)
    mask1 = (df_1d["open_time"] >= s) & (df_1d["open_time"] < e)
    df_4h_p = df_4h.loc[mask4].reset_index(drop=True)
    df_1d_p = df_1d.loc[mask1].reset_index(drop=True)

    days = (e - s).days
    print(f"Period: {s.date()} → {e.date()}  ({days} days, {len(df_4h_p)} 4h bars)\n")

    closes = df_4h_p["close"].values

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.02,
        timeframe="4h",
        long_only=True,
        ema_stop_mult=1.5,
        ema_tp_mult=4.5,
    )
    engine = BacktestEngine(cfg)
    bt = engine.run(df=df_4h_p, df_4h=df_1d_p, symbol="BTCUSDT")
    summary = engine.summary(bt)
    annual = compute_annual_return(bt.initial_capital, bt.final_capital, days) * 100

    print("=" * 100)
    print("BASELINE (no vol filter) — last 2 years")
    print("=" * 100)
    print(f"  Trades={summary['total_trades']}  WR={summary['win_rate_pct']:.1f}%  "
          f"Annual={annual:.2f}%  MaxDD={summary['max_drawdown_pct']:.2f}%  "
          f"PF={summary['profit_factor']:.2f}  Sharpe={summary['sharpe_ratio']:.2f}")

    # Map entry-bar timestamp to vol regime
    closed = [t for t in bt.trades if t["exit_reason"] != "END_OF_PERIOD"]
    print(f"\nClosed trades: {len(closed)}")

    # Build a vol-at-time map by indexing into df_4h_p
    time_to_vol = {}
    for i in range(len(df_4h_p)):
        time_to_vol[df_4h_p.iloc[i]["open_time"]] = realized_vol_pct(closes, i)

    high_vol_trades = []
    low_vol_trades = []
    nan_trades = []
    for t in closed:
        entry_time = pd.Timestamp(t["entry_time"]) if not isinstance(t["entry_time"], pd.Timestamp) else t["entry_time"]
        # Convert string back to Timestamp if needed
        if isinstance(t["entry_time"], str):
            entry_time = pd.Timestamp(t["entry_time"])
        else:
            entry_time = t["entry_time"]

        vol = time_to_vol.get(entry_time)
        if vol is None or np.isnan(vol):
            # Try lookup by closest match
            nan_trades.append(t)
            continue
        if vol >= VOL_THRESHOLD_PCT:
            high_vol_trades.append((vol, t))
        else:
            low_vol_trades.append((vol, t))

    def _stats(name, items):
        if not items:
            print(f"  {name}: no trades")
            return
        trades = [x[1] for x in items]
        wins = [t for t in trades if (t["pnl"] or 0) > 0]
        total_pnl = sum(t["pnl"] for t in trades)
        avg_vol = np.mean([x[0] for x in items])
        wr = len(wins) / len(trades) * 100
        gross_win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        print(f"  {name}: n={len(trades)}  AvgVol={avg_vol:.1f}%  WR={wr:.1f}%  "
              f"PF={pf:.2f}  TotalPnL=${total_pnl:.2f}  AvgPnL=${total_pnl/len(trades):.2f}")

    print(f"\n=== Trades segmented by realized vol at entry (threshold={VOL_THRESHOLD_PCT}%) ===")
    _stats("HIGH_VOL (>=40%)", high_vol_trades)
    _stats("LOW_VOL  (<40%)",  low_vol_trades)
    if nan_trades:
        print(f"  (skipped {len(nan_trades)} trades with NaN vol — early period)")

    # Compute counterfactual: what if we had skipped LOW_VOL trades?
    if low_vol_trades:
        low_vol_pnl = sum(x[1]["pnl"] for x in low_vol_trades)
        baseline_total = bt.final_capital - bt.initial_capital
        counterfactual = baseline_total - low_vol_pnl
        cf_final = bt.initial_capital + counterfactual
        cf_annual = compute_annual_return(bt.initial_capital, cf_final, days) * 100
        print(f"\n=== Counterfactual: skip all LOW_VOL trades ===")
        print(f"  Baseline final: ${bt.final_capital:.2f}  (annual {annual:.2f}%)")
        print(f"  Skip LOW_VOL  final: ${cf_final:.2f}  (annual {cf_annual:.2f}%)")
        print(f"  Improvement: +{cf_annual - annual:.2f}pp annual")


if __name__ == "__main__":
    main()
