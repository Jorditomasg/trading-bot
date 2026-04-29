"""Run hypotheses A/B against the documented 4h baseline.

Each hypothesis is a callable that returns a (BacktestConfig, run_kwargs, label).
We print a side-by-side table of CAGR / Sharpe / PF / MaxDD / Trades / WinRate / Payoff
so improvements (or regressions) are obvious.

Pure research — never imported by main.py.
"""

from __future__ import annotations

import json
import logging
import sys
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
        raise FileNotFoundError(path)
    df = pd.read_parquet(path)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    mask = (df["open_time"] >= START) & (df["open_time"] <= END)
    return df[mask].reset_index(drop=True)


def years_span(df: pd.DataFrame) -> float:
    delta = df["open_time"].iloc[-1] - df["open_time"].iloc[0]
    return delta.total_seconds() / (365.25 * 24 * 3600)


def annualize(total_pct: float, years: float) -> float:
    if years <= 0:
        return 0.0
    return ((1.0 + total_pct / 100.0) ** (1.0 / years) - 1.0) * 100.0


def _payoff(trades: list[dict]) -> float:
    closed = [t for t in trades if t["exit_reason"] != "END_OF_PERIOD"]
    wins   = [t["pnl"] for t in closed if (t["pnl"] or 0) > 0]
    losses = [t["pnl"] for t in closed if (t["pnl"] or 0) < 0]
    if not wins or not losses:
        return float("inf") if wins else 0.0
    return abs(mean(wins) / mean(losses))


def run_one(label: str, df_4h, df_1d, df_1w, cfg: BacktestConfig, *, use_weekly: bool) -> dict:
    engine = BacktestEngine(cfg)
    res = engine.run(
        df_4h,
        df_4h=df_1d,
        df_weekly=df_1w if use_weekly else None,
        symbol="BTCUSDT",
    )
    s = engine.summary(res)
    yrs = years_span(df_4h)
    return {
        "label":     label,
        "ann":       annualize(s["total_pnl_pct"], yrs),
        "sharpe":    s["sharpe_ratio"],
        "pf":        s["profit_factor"],
        "dd":        s["max_drawdown_pct"],
        "trades":    s["total_trades"],
        "wr":        s["win_rate_pct"],
        "payoff":    _payoff(res.trades),
        "streak":    s["max_loss_streak"],
        "trades_obj": res.trades,  # for downstream analysis; stripped before printing
    }


def base_cfg(**overrides) -> BacktestConfig:
    """Start from documented optimum, allow overrides."""
    base = dict(
        initial_capital=10_000,
        risk_per_trade=0.02,
        timeframe="4h",
        cost_per_side_pct=0.0015,
        long_only=True,
        ema_stop_mult=1.5,
        ema_tp_mult=4.5,
        ema_max_distance_atr=1.0,
    )
    base.update(overrides)
    return BacktestConfig(**base)


def run_all() -> None:
    print("Loading cached klines...", flush=True)
    df_4h = load_cached("BTCUSDT", "4h")
    df_1d = load_cached("BTCUSDT", "1d")
    df_1w = load_cached("BTCUSDT", "1w")
    print(f"  4h: {len(df_4h)}  1d: {len(df_1d)}  1w: {len(df_1w)}\n", flush=True)

    # Each entry: (label, cfg-overrides-dict, use_weekly_flag)
    HYPS = [
        ("BASELINE",                                    {},                                                                       False),
        ("H1  weekly momentum filter ON",               {"momentum_filter_enabled": True, "momentum_sma_period": 20, "momentum_neutral_band": 0.05}, True),
        ("H1b weekly momentum (band 3%)",               {"momentum_filter_enabled": True, "momentum_sma_period": 20, "momentum_neutral_band": 0.03}, True),
        ("H1c weekly momentum (band 8%)",               {"momentum_filter_enabled": True, "momentum_sma_period": 20, "momentum_neutral_band": 0.08}, True),
        ("H2a tighter SL=1.25 (TP=4.5)",                {"ema_stop_mult": 1.25},                                                  False),
        ("H2b tighter SL=1.0 (TP=4.5)",                 {"ema_stop_mult": 1.0},                                                   False),
        ("H2c wider SL=1.75 (TP=4.5)",                  {"ema_stop_mult": 1.75},                                                  False),
        ("H3a TP=5.0",                                  {"ema_tp_mult": 5.0},                                                     False),
        ("H3b TP=5.5",                                  {"ema_tp_mult": 5.5},                                                     False),
        ("H3c TP=6.0",                                  {"ema_tp_mult": 6.0},                                                     False),
        ("H4a max_dist=0.75",                           {"ema_max_distance_atr": 0.75},                                           False),
        ("H4b max_dist=1.5",                            {"ema_max_distance_atr": 1.5},                                            False),
        ("H4c max_dist=2.0",                            {"ema_max_distance_atr": 2.0},                                            False),
    ]

    results = []
    for label, overrides, use_weekly in HYPS:
        cfg = base_cfg(**overrides)
        r = run_one(label, df_4h, df_1d, df_1w, cfg, use_weekly=use_weekly)
        results.append(r)
        print(f"  ✓ {label}", flush=True)

    base = results[0]
    print()
    hdr = (
        f"{'Hypothesis':<36} {'CAGR':>7} {'ΔCAGR':>7} {'Sharpe':>7} "
        f"{'PF':>6} {'MaxDD':>7} {'Trades':>7} {'WR%':>6} {'Payoff':>7} {'Streak':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        d = r["ann"] - base["ann"]
        flag = ""
        if r["label"] != "BASELINE":
            ok = (r["ann"] > base["ann"] and r["pf"] >= 1.3 and r["dd"] <= 25.0)
            flag = " ★" if ok else ""
        pf_str = f"{r['pf']:6.3f}" if r['pf'] != float('inf') else "   inf"
        po_str = f"{r['payoff']:6.3f}" if r['payoff'] != float('inf') else "   inf"
        print(
            f"{r['label']:<36} "
            f"{r['ann']:>6.2f}% "
            f"{d:+6.2f}% "
            f"{r['sharpe']:>7.3f} "
            f"{pf_str} "
            f"{r['dd']:>6.2f}% "
            f"{r['trades']:>7d} "
            f"{r['wr']:>5.1f}% "
            f"{po_str} "
            f"{r['streak']:>7d}"
            f"{flag}"
        )

    # Strip trades_obj before persisting
    out = []
    for r in results:
        r2 = {k: v for k, v in r.items() if k != "trades_obj"}
        out.append(r2)
    out_path = Path(__file__).resolve().parent.parent.parent / "data" / "hypotheses_round1.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    run_all()
