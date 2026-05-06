"""Quick DD scaler validation — only 4 variants × FULL 3y + bad period."""

from __future__ import annotations

import logging

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)

import sys
import pandas as pd

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.risk.drawdown_scaler import DrawdownRiskConfig


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC")
    return df.loc[(df["open_time"] >= s) & (df["open_time"] < e)].reset_index(drop=True)


def _run(df_4h, df_1d, period_name, start, end, dd_cfg) -> dict:
    df_p   = _slice(df_4h, start, end)
    df_1dp = _slice(df_1d, start, end)
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.02, timeframe="4h",
        long_only=True, ema_stop_mult=1.5, ema_tp_mult=4.5, dd_risk=dd_cfg,
    )
    bt = BacktestEngine(cfg).run(df=df_p, df_4h=df_1dp, symbol="BTCUSDT")
    s  = BacktestEngine(cfg).summary(bt)
    days = (pd.Timestamp(end, tz="UTC") - pd.Timestamp(start, tz="UTC")).days
    annual = compute_annual_return(bt.initial_capital, bt.final_capital, days) * 100
    return {"period": period_name, "trades": s["total_trades"], "wr": s["win_rate_pct"],
            "annual": annual, "dd": s["max_drawdown_pct"], "pf": s["profit_factor"],
            "sharpe": s["sharpe_ratio"]}


VARIANTS = {
    "BASELINE":             None,
    "DD 5/-50":              DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5]),
    "DD 5/-50 + 10/-25":     DrawdownRiskConfig(enabled=True, thresholds=[0.05, 0.10], multipliers=[0.5, 0.25]),
    "DD 7/-50 + 12/-25":     DrawdownRiskConfig(enabled=True, thresholds=[0.07, 0.12], multipliers=[0.5, 0.25]),
    "DD 5/-66 + 10/-33":     DrawdownRiskConfig(enabled=True, thresholds=[0.05, 0.10], multipliers=[0.66, 0.33]),
    "DD 8/-50":              DrawdownRiskConfig(enabled=True, thresholds=[0.08], multipliers=[0.5]),
    "DD 10/-50":             DrawdownRiskConfig(enabled=True, thresholds=[0.10], multipliers=[0.5]),
}

PERIODS = [
    ("FULL 3y",        "2022-05-01", "2026-05-04"),
    ("Bad 2025-05→11", "2025-05-04", "2025-11-04"),
]


def main():
    print("Loading data...", flush=True)
    df_4h = pd.read_parquet("data/klines/BTCUSDT_4h.parquet")
    df_1d = pd.read_parquet("data/klines/BTCUSDT_1d.parquet")
    print(f"  4h={len(df_4h)} 1d={len(df_1d)}", flush=True)

    rows: list[dict] = []
    for vname, vcfg in VARIANTS.items():
        for pname, pstart, pend in PERIODS:
            print(f"  running {vname} on {pname}...", flush=True)
            r = _run(df_4h, df_1d, pname, pstart, pend, vcfg)
            r["variant"] = vname
            rows.append(r)
            print(f"    → trades={r['trades']} ann={r['annual']:.2f}% dd={r['dd']:.2f}% pf={r['pf']:.2f}", flush=True)

    print("\n" + "=" * 110, flush=True)
    print("RESULTS", flush=True)
    print("=" * 110, flush=True)
    for pname, _, _ in PERIODS:
        print(f"\n--- {pname} ---", flush=True)
        print(f"{'Variant':<25} {'Trades':>6} {'WR%':>5} {'Annual%':>8} {'MaxDD%':>7} {'PF':>5} {'Sharpe':>7}", flush=True)
        for r in rows:
            if r["period"] == pname:
                print(f"{r['variant']:<25} {r['trades']:>6} {r['wr']:>5.1f} "
                      f"{r['annual']:>8.2f} {r['dd']:>7.2f} {r['pf']:>5.2f} {r['sharpe']:>7.2f}", flush=True)


if __name__ == "__main__":
    main()
