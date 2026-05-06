"""Validate the DrawdownRiskConfig scaler — grid search vs baseline.

Decision rule:
- on FULL 3y: variant is candidate if MaxDD strictly improves AND PF within -3%
  AND annual within -10% relative
- on bad 2025-05→11 period: variant must improve annual (less negative)

Run:
    PYTHONPATH=. venv/bin/python scripts/research/validate_dd_scaler.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.risk.drawdown_scaler import DrawdownRiskConfig


@dataclass
class TestPeriod:
    name: str
    start: str
    end: str


PERIODS = [
    TestPeriod("FULL 3y",        "2022-05-01", "2026-05-04"),
    TestPeriod("Bad 2025-05→11", "2025-05-04", "2025-11-04"),
    TestPeriod("Good last 6m",   "2025-11-04", "2026-05-04"),
    TestPeriod("Bull 2024",      "2024-01-01", "2025-01-01"),
]


@dataclass
class Variant:
    label: str
    config: DrawdownRiskConfig | None


VARIANTS = [
    Variant("BASELINE",           None),
    Variant("DD 5/-50",           DrawdownRiskConfig(enabled=True, thresholds=[0.05], multipliers=[0.5])),
    Variant("DD 5/-50 + 10/-25",  DrawdownRiskConfig(enabled=True, thresholds=[0.05, 0.10], multipliers=[0.5, 0.25])),
    Variant("DD 7/-50 + 12/-25",  DrawdownRiskConfig(enabled=True, thresholds=[0.07, 0.12], multipliers=[0.5, 0.25])),
    Variant("DD 3/-75 + 7/-50 + 12/-25",
            DrawdownRiskConfig(enabled=True, thresholds=[0.03, 0.07, 0.12], multipliers=[0.75, 0.5, 0.25])),
    Variant("DD 5/-66 + 10/-33",  DrawdownRiskConfig(enabled=True, thresholds=[0.05, 0.10], multipliers=[0.66, 0.33])),
    Variant("DD 7/-66 + 12/-33",  DrawdownRiskConfig(enabled=True, thresholds=[0.07, 0.12], multipliers=[0.66, 0.33])),
    Variant("DD 8/-50",           DrawdownRiskConfig(enabled=True, thresholds=[0.08], multipliers=[0.5])),
    Variant("DD 10/-50",          DrawdownRiskConfig(enabled=True, thresholds=[0.10], multipliers=[0.5])),
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
        return {"period": period.name, "variant": variant.label, "skipped": True}

    cfg = BacktestConfig(
        initial_capital=10_000.0,
        risk_per_trade=0.02,
        timeframe="4h",
        long_only=True,
        ema_stop_mult=1.5,
        ema_tp_mult=4.5,
        dd_risk=variant.config,
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
        print(f"{h[0]:<32} {h[1]:>7} {h[2]:>6} {h[3]:>9} {h[4]:>8} {h[5]:>7} {h[6]:>6} {h[7]:>11} {h[8]:>9}")
        baseline = next((r for r in rows if r["variant"] == "BASELINE"), None)
        for r in rows:
            if r.get("skipped"):
                print(f"{r['variant']:<32} SKIPPED")
                continue
            line = (
                f"{r['variant']:<32} {r['trades']:>7} {r['wr']:>6.1f} {r['annual']:>9.2f} "
                f"{r['max_dd']:>8.2f} {r['sharpe']:>7.2f} {r['pf']:>6.2f} {r['total_pnl']:>11.2f} "
                f"{r['max_loss_streak']:>9}"
            )
            if baseline and not baseline.get("skipped") and r["variant"] != "BASELINE":
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
        print(f"  → {v.label}")
        for p in PERIODS:
            rows_by_period[p.name].append(_run(p, v, df_4h, df_1d))

    _print(rows_by_period)

    # ── VERDICT ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 110)
    print("VERDICT")
    print("=" * 110)
    full = {r["variant"]: r for r in rows_by_period["FULL 3y"]}
    bad  = {r["variant"]: r for r in rows_by_period["Bad 2025-05→11"]}
    base_full = full["BASELINE"]
    base_bad  = bad["BASELINE"]

    print(f"FULL 3y baseline: annual={base_full['annual']:.2f}%  MaxDD={base_full['max_dd']:.2f}%  "
          f"PF={base_full['pf']:.2f}  Sharpe={base_full['sharpe']:.2f}")
    print(f"Bad period baseline: annual={base_bad['annual']:.2f}%")
    print()
    print("Candidates (FULL 3y: MaxDD strictly better, PF >= -3%, annual >= -10% rel; "
          "Bad period: annual strictly better):")
    found = False
    for v in VARIANTS:
        if v.label == "BASELINE":
            continue
        rf, rb = full.get(v.label), bad.get(v.label)
        if rf is None or rb is None or rf.get("skipped") or rb.get("skipped"):
            continue
        rel_annual = (rf["annual"] - base_full["annual"]) / abs(base_full["annual"]) * 100
        pf_rel     = (rf["pf"] - base_full["pf"]) / base_full["pf"] * 100 if base_full["pf"] > 0 else 0

        ok_dd     = rf["max_dd"] < base_full["max_dd"] - 0.5  # at least 0.5pp better
        ok_pf     = pf_rel >= -3.0
        ok_annual = rel_annual >= -10.0
        ok_bad    = rb["annual"] > base_bad["annual"]

        marker = "✓" if (ok_dd and ok_pf and ok_annual and ok_bad) else "✗"
        if marker == "✓":
            found = True
        print(f"  {marker} {v.label:<32} | 3y: ann={rf['annual']:.2f}% (rel {rel_annual:+.1f}%) "
              f"DD={rf['max_dd']:.2f}% PF={rf['pf']:.2f} ({pf_rel:+.1f}%) | "
              f"Bad: ann={rb['annual']:.2f}%")

    if not found:
        print("  → No variant fully passes. Marked '✗' indicate failure on at least one criterion.")


if __name__ == "__main__":
    main()
