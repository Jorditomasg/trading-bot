"""Establish baseline 4h backtest from local Parquet cache (no network).

Loads cached BTCUSDT 4h + 1d, runs current optimal config (long_only, no trail,
dist=1.0, TP=4.5, SL=1.5) at 2% risk over Apr 2022 → Apr 2025.

Outputs a comprehensive metric dump that subsequent hypotheses will be compared
against.  Pure research — never imported by main.py.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine

logging.disable(logging.CRITICAL)


CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "klines"
START = pd.Timestamp("2022-04-01", tz="UTC")
END   = pd.Timestamp("2025-04-01", tz="UTC")


def load_cached(symbol: str, interval: str) -> pd.DataFrame:
    path = CACHE_DIR / f"{symbol}_{interval}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing cache: {path}")
    df = pd.read_parquet(path)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    mask = (df["open_time"] >= START) & (df["open_time"] <= END)
    return df[mask].reset_index(drop=True)


def years_span(df: pd.DataFrame) -> float:
    delta = df["open_time"].iloc[-1] - df["open_time"].iloc[0]
    return delta.total_seconds() / (365.25 * 24 * 3600)


def annualize(total_pct: float, years: float) -> float:
    """CAGR from total return %."""
    if years <= 0:
        return 0.0
    return ((1.0 + total_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["exit_reason"] != "END_OF_PERIOD"]
    wins   = [t["pnl"] for t in closed if (t["pnl"] or 0) > 0]
    losses = [t["pnl"] for t in closed if (t["pnl"] or 0) < 0]
    avg_w = mean(wins) if wins else 0.0
    avg_l = mean(losses) if losses else 0.0
    payoff = abs(avg_w / avg_l) if avg_l else float("inf")

    by_reason: dict[str, int] = {}
    for t in closed:
        by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1

    by_regime: dict[str, dict] = {}
    for t in closed:
        r = t.get("regime") or "UNKNOWN"
        bucket = by_regime.setdefault(r, {"count": 0, "wins": 0, "pnl": 0.0})
        bucket["count"] += 1
        if (t["pnl"] or 0) > 0:
            bucket["wins"] += 1
        bucket["pnl"] += t["pnl"] or 0.0

    return {
        "n_closed": len(closed),
        "n_wins":   len(wins),
        "n_losses": len(losses),
        "avg_win":  avg_w,
        "avg_loss": avg_l,
        "payoff":   payoff,
        "by_reason": by_reason,
        "by_regime": by_regime,
    }


def run_baseline() -> dict:
    print("Loading cached klines (no network)...", flush=True)
    df_4h = load_cached("BTCUSDT", "4h")
    df_1d = load_cached("BTCUSDT", "1d")
    print(f"  4h bars: {len(df_4h):>5}  ({df_4h['open_time'].iloc[0]} → {df_4h['open_time'].iloc[-1]})")
    print(f"  1d bars: {len(df_1d):>5}  ({df_1d['open_time'].iloc[0]} → {df_1d['open_time'].iloc[-1]})")

    cfg = BacktestConfig(
        initial_capital=10_000,
        risk_per_trade=0.02,
        timeframe="4h",
        # Current optimal (per CLAUDE.md):
        long_only=True,
        ema_stop_mult=1.5,
        ema_tp_mult=4.5,
        ema_max_distance_atr=1.0,
    )
    engine = BacktestEngine(cfg)
    print("\nRunning 4h backtest (long_only, no trail, dist=1.0, TP=4.5, SL=1.5, risk 2%)...", flush=True)
    res = engine.run(df_4h, df_4h=df_1d, symbol="BTCUSDT")
    s   = engine.summary(res)
    ts  = trade_stats(res.trades)

    yrs = years_span(df_4h)
    ann = annualize(s["total_pnl_pct"], yrs)

    print("\n" + "=" * 78)
    print(f"BASELINE — 4h BTCUSDT, {yrs:.2f} years, risk 2%")
    print("=" * 78)
    print(f"  Total return:       {s['total_pnl_pct']:>8.2f}%")
    print(f"  Annualised (CAGR):  {ann:>8.2f}%")
    print(f"  Sharpe ratio:       {s['sharpe_ratio']:>8.3f}")
    print(f"  Profit factor:      {s['profit_factor']:>8.3f}")
    print(f"  Max drawdown:       {s['max_drawdown_pct']:>8.2f}%")
    print(f"  Trades closed:      {s['total_trades']:>8d}")
    print(f"  Win rate:           {s['win_rate_pct']:>8.2f}%")
    print(f"  Avg win / Avg loss: {ts['avg_win']:>+8.2f} / {ts['avg_loss']:>+8.2f}")
    print(f"  Payoff (W:L):       {ts['payoff']:>8.3f}")
    print(f"  Max loss streak:    {s['max_loss_streak']:>8d}")
    print(f"  Best trade:         {s['best_trade_pnl']:>+8.2f}")
    print(f"  Worst trade:        {s['worst_trade_pnl']:>+8.2f}")
    print(f"  Open at period end: {s['open_at_period_end']:>8d}")

    print("\n  Exits by reason:")
    for reason, n in sorted(ts["by_reason"].items(), key=lambda x: -x[1]):
        print(f"    {reason:<20} {n:>4}  ({n/ts['n_closed']*100:.1f}%)")

    print("\n  Performance by regime (entry-time regime):")
    print(f"    {'Regime':<12} {'N':>4} {'Wins':>5} {'WR%':>6} {'P&L':>10}")
    for reg, b in sorted(ts["by_regime"].items(), key=lambda x: -x[1]["count"]):
        wr = b["wins"] / b["count"] * 100 if b["count"] else 0.0
        print(f"    {reg:<12} {b['count']:>4d} {b['wins']:>5d} {wr:>5.1f}% {b['pnl']:>+10.2f}")

    print("=" * 78)

    out = {
        "annual_return_pct": ann,
        "total_return_pct":  s["total_pnl_pct"],
        "sharpe":            s["sharpe_ratio"],
        "profit_factor":     s["profit_factor"],
        "max_drawdown_pct":  s["max_drawdown_pct"],
        "win_rate_pct":      s["win_rate_pct"],
        "total_trades":      s["total_trades"],
        "payoff":            ts["payoff"],
        "avg_win":           ts["avg_win"],
        "avg_loss":          ts["avg_loss"],
        "max_loss_streak":   s["max_loss_streak"],
        "by_reason":         ts["by_reason"],
        "by_regime":         {r: {"count": b["count"], "wr": b["wins"]/b["count"]*100 if b["count"] else 0, "pnl": b["pnl"]} for r, b in ts["by_regime"].items()},
        "years":             yrs,
    }
    out_path = Path(__file__).resolve().parent.parent.parent / "data" / "baseline_metrics.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nMetrics written to {out_path}")
    return out


if __name__ == "__main__":
    run_baseline()
