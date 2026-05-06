"""Validate the VolRegimeFilter — grid search vs baseline.

Decision rule: a variant is KEPT if on the full 3-year window it:
  - Improves PF (>= baseline)
  - Improves MaxDD (<= baseline)
  - Annual return is within -10% relative of baseline (some sacrifice OK
    if drawdown improvement justifies it)
  AND on the bad period (2025-05→11) it cuts the loss in half.

Run:
    PYTHONPATH=. venv/bin/python scripts/research/validate_vol_filter.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)
logging.getLogger("bot.risk.vol_regime").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.risk.vol_regime import VolRegimeConfig


@dataclass
class TestPeriod:
    name: str
    start: str
    end: str


PERIODS = [
    TestPeriod("FULL 3y",         "2022-05-01", "2026-05-04"),
    TestPeriod("Bad 2025-05→11",  "2025-05-04", "2025-11-04"),
    TestPeriod("Good last 6m",    "2025-11-04", "2026-05-04"),
    TestPeriod("Bull 2024",       "2024-01-01", "2025-01-01"),
]


@dataclass
class Variant:
    label: str
    config: VolRegimeConfig | None  # None = baseline


VARIANTS = [
    Variant("BASELINE (no filter)", None),
    # block-action variants — different percentile thresholds
    Variant("block P20",   VolRegimeConfig(enabled=True, percentile_threshold=20.0, action="block",  timeframe="4h")),
    Variant("block P30",   VolRegimeConfig(enabled=True, percentile_threshold=30.0, action="block",  timeframe="4h")),
    Variant("block P40",   VolRegimeConfig(enabled=True, percentile_threshold=40.0, action="block",  timeframe="4h")),
    Variant("block P50",   VolRegimeConfig(enabled=True, percentile_threshold=50.0, action="block",  timeframe="4h")),
    # reduce-size variants — same percentiles but halve size instead of skip
    Variant("reduce P30",  VolRegimeConfig(enabled=True, percentile_threshold=30.0, action="reduce", reduce_factor=0.5, timeframe="4h")),
    Variant("reduce P40",  VolRegimeConfig(enabled=True, percentile_threshold=40.0, action="reduce", reduce_factor=0.5, timeframe="4h")),
    Variant("reduce P50",  VolRegimeConfig(enabled=True, percentile_threshold=50.0, action="reduce", reduce_factor=0.5, timeframe="4h")),
]


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end, tz="UTC")
    mask = (df["open_time"] >= s) & (df["open_time"] < e)
    return df.loc[mask].reset_index(drop=True)


def _run(period: TestPeriod, variant: Variant, df_4h: pd.DataFrame, df_1d: pd.DataFrame) -> dict:
    df_4h_p = _slice(df_4h, period.start, period.end)
    df_1d_p = _slice(df_1d, period.start, period.end)
    if len(df_4h_p) < 250:
        return {"period": period.name, "variant": variant.label, "skipped": True, "reason": "insufficient bars"}

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.02,
        timeframe="4h",
        long_only=True,
        ema_stop_mult=1.5,
        ema_tp_mult=4.5,
        vol_regime=variant.config,
    )
    engine = BacktestEngine(cfg)
    bt = engine.run(df=df_4h_p, df_4h=df_1d_p, symbol="BTCUSDT")
    summary = engine.summary(bt)
    days = (pd.Timestamp(period.end, tz="UTC") - pd.Timestamp(period.start, tz="UTC")).days
    annual = compute_annual_return(bt.initial_capital, bt.final_capital, days) * 100
    return {
        "period":     period.name,
        "variant":    variant.label,
        "trades":     summary["total_trades"],
        "wr":         summary["win_rate_pct"],
        "annual":     annual,
        "max_dd":     summary["max_drawdown_pct"],
        "sharpe":     summary["sharpe_ratio"],
        "pf":         summary["profit_factor"],
        "total_pnl":  summary["total_pnl"],
        "max_loss_streak": summary["max_loss_streak"],
    }


def _print(rows_by_period: dict[str, list[dict]]) -> None:
    for pname, rows in rows_by_period.items():
        print("\n" + "=" * 110)
        print(f"PERIOD: {pname}")
        print("=" * 110)
        h = ["Variant", "Trades", "WR%", "Annual%", "MaxDD%", "Sharpe", "PF", "TotalPnL$", "LossStrk"]
        print(f"{h[0]:<25} {h[1]:>7} {h[2]:>6} {h[3]:>9} {h[4]:>8} {h[5]:>7} {h[6]:>6} {h[7]:>11} {h[8]:>9}")
        baseline = next((r for r in rows if r["variant"].startswith("BASELINE")), None)
        for r in rows:
            if r.get("skipped"):
                print(f"{r['variant']:<25} SKIPPED — {r['reason']}")
                continue
            line = (
                f"{r['variant']:<25} {r['trades']:>7} {r['wr']:>6.1f} {r['annual']:>9.2f} "
                f"{r['max_dd']:>8.2f} {r['sharpe']:>7.2f} {r['pf']:>6.2f} {r['total_pnl']:>11.2f} "
                f"{r['max_loss_streak']:>9}"
            )
            # Mark improvement vs baseline on annual + max_dd + pf
            if baseline and not baseline.get("skipped") and r["variant"] != baseline["variant"]:
                better = sum([
                    r["annual"] >= baseline["annual"],
                    r["max_dd"] <= baseline["max_dd"],
                    r["pf"] >= baseline["pf"],
                ])
                if better == 3:
                    line += "   ★★★"
                elif better == 2:
                    line += "   ★★"
                elif better == 1:
                    line += "   ★"
            print(line)


def main() -> None:
    print("Loading BTC 4h + 1d cached data...")
    df_4h = pd.read_parquet("data/klines/BTCUSDT_4h.parquet")
    df_1d = pd.read_parquet("data/klines/BTCUSDT_1d.parquet")

    rows_by_period: dict[str, list[dict]] = {p.name: [] for p in PERIODS}
    for v in VARIANTS:
        print(f"  → variant: {v.label}")
        for p in PERIODS:
            r = _run(p, v, df_4h, df_1d)
            rows_by_period[p.name].append(r)

    _print(rows_by_period)

    # Verdict on FULL 3y
    print("\n" + "=" * 110)
    print("VERDICT — FULL 3y window")
    print("=" * 110)
    base = next(r for r in rows_by_period["FULL 3y"] if r["variant"].startswith("BASELINE"))
    print(f"Baseline: annual={base['annual']:.2f}%  MaxDD={base['max_dd']:.2f}%  PF={base['pf']:.2f}  Sharpe={base['sharpe']:.2f}")
    print()
    print("Candidates that improve PF AND MaxDD without sacrificing annual >10%:")
    found = False
    for r in rows_by_period["FULL 3y"]:
        if r["variant"].startswith("BASELINE"):
            continue
        rel_annual = (r["annual"] - base["annual"]) / abs(base["annual"]) * 100 if base["annual"] != 0 else 0
        if r["pf"] >= base["pf"] and r["max_dd"] <= base["max_dd"] and rel_annual >= -10:
            found = True
            print(f"  ✓ {r['variant']:<22}  annual={r['annual']:.2f}% (rel {rel_annual:+.1f}%)  "
                  f"MaxDD={r['max_dd']:.2f}%  PF={r['pf']:.2f}")
    if not found:
        print("  ✗ NONE — no variant improves all three metrics")


if __name__ == "__main__":
    main()
