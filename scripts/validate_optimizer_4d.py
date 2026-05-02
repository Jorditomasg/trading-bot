#!/usr/bin/env python
"""4-dimensional grid search to validate an expanded optimizer.

Current optimizer searches only (stop_atr_mult, tp_atr_mult). This adds two
dimensions: max_distance_atr and volume_multiplier. The current production
default ('SL=1.5, TP=4.5, dist=1.0, vol=0') is the reference; we look for
any combo that materially beats it on BTC 3y.

Decision rule (out-of-sample validation, see WALK-FORWARD section below):
- Run grid on years 1-2 (in-sample).
- Re-test top 5 candidates on year 3 (out-of-sample).
- Only declare GO if a candidate beats baseline on BOTH samples.

Usage:
    PYTHONPATH=. venv/bin/python scripts/validate_optimizer_4d.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import product

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return

# ── Search space ──────────────────────────────────────────────────────────────
STOP_GRID = [1.0, 1.25, 1.5, 1.75]
TP_GRID   = [3.5, 4.0, 4.5, 5.0, 5.5]
DIST_GRID = [0.75, 1.0, 1.25, 1.5]
VOL_GRID  = [0.0, 1.2, 1.5]   # 0.0 = filter off

# Reference (current production default)
REF = {"stop": 1.5, "tp": 4.5, "dist": 1.0, "vol": 0.0}


@dataclass
class GridResult:
    stop:      float
    tp:        float
    dist:      float
    vol:       float
    annual:    float
    pf:        float
    dd:        float
    sharpe:    float
    trades:    int
    final:     float


def _run_combo(combo: dict, df: pd.DataFrame, df_bias: pd.DataFrame, days: int) -> GridResult:
    cfg = BacktestConfig(
        initial_capital      = 10_000.0,
        risk_per_trade       = 0.04,
        timeframe            = "4h",
        long_only            = True,
        ema_stop_mult        = combo["stop"],
        ema_tp_mult          = combo["tp"],
        ema_max_distance_atr = combo["dist"],
        ema_volume_mult      = combo["vol"] if combo["vol"] > 0 else None,
    )
    e  = BacktestEngine(cfg)
    bt = e.run(df=df, df_4h=df_bias, symbol="BTCUSDT")
    s  = e.summary(bt)
    return GridResult(
        stop=combo["stop"], tp=combo["tp"], dist=combo["dist"], vol=combo["vol"],
        annual = compute_annual_return(bt.initial_capital, bt.final_capital, days),
        pf     = s["profit_factor"],
        dd     = s["max_drawdown_pct"],
        sharpe = s["sharpe_ratio"],
        trades = s["total_trades"],
        final  = bt.final_capital,
    )


def _is_baseline(c: dict) -> bool:
    return (c["stop"] == REF["stop"] and c["tp"] == REF["tp"]
            and c["dist"] == REF["dist"] and c["vol"] == REF["vol"])


def _print_top(results: list[GridResult], n: int = 10, label: str = "") -> None:
    print(f"\nTop {n} by Annual return{(' ' + label) if label else ''}:")
    print(f"  {'Stop':>5}  {'TP':>5}  {'Dist':>5}  {'Vol':>4}    "
          f"{'Annual':>8}  {'PF':>5}  {'DD':>7}  {'Trades':>6}")
    sorted_r = sorted(results, key=lambda r: -r.annual)[:n]
    for r in sorted_r:
        marker = "  ← BASELINE" if all([
            r.stop == REF["stop"], r.tp == REF["tp"],
            r.dist == REF["dist"], r.vol == REF["vol"],
        ]) else ""
        print(
            f"  {r.stop:>5.2f}  {r.tp:>5.2f}  {r.dist:>5.2f}  {r.vol:>4.1f}    "
            f"{r.annual*100:>+7.1f}%  {r.pf:>5.2f}  -{r.dd:>5.1f}%  {r.trades:>6}{marker}"
        )


def main() -> None:
    days_total = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days_total + 30)

    print(f"\n{'=' * 100}")
    print(f"  OPTIMIZER 4D VALIDATION  —  BTCUSDT, 3y, 4h, long-only, risk=4%")
    print(f"{'=' * 100}\n")

    print("Fetching data…")
    df_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)

    # Split: in-sample = first 2 years, out-of-sample = last 1 year
    cutoff = pd.Timestamp(end_dt - timedelta(days=365))
    in_sample_4h    = df_4h[df_4h["open_time"] < cutoff].reset_index(drop=True)
    in_sample_1d    = df_1d[df_1d["open_time"] < cutoff].reset_index(drop=True)
    out_sample_4h   = df_4h[df_4h["open_time"] >= cutoff].reset_index(drop=True)
    out_sample_1d   = df_1d[df_1d["open_time"] >= cutoff].reset_index(drop=True)

    print(f"  In-sample (years 1-2): {len(in_sample_4h)} bars")
    print(f"  Out-of-sample (year 3): {len(out_sample_4h)} bars")
    print(f"  Cutoff date: {cutoff.date()}\n")

    # ── In-sample grid ────────────────────────────────────────────────────────
    combos = [
        {"stop": s, "tp": t, "dist": d, "vol": v}
        for s, t, d, v in product(STOP_GRID, TP_GRID, DIST_GRID, VOL_GRID)
        if t / s >= 1.5  # min R:R
    ]
    print(f"In-sample grid: {len(combos)} combos to test (~{len(combos) * 1.5 / 60:.0f} min)\n")

    in_sample_days = (in_sample_4h.iloc[-1]["open_time"] - in_sample_4h.iloc[0]["open_time"]).days
    out_sample_days = days_total - in_sample_days

    in_results: list[GridResult] = []
    for i, combo in enumerate(combos, 1):
        if i % 20 == 0 or i == len(combos):
            print(f"  [{i}/{len(combos)}] running…", flush=True)
        try:
            r = _run_combo(combo, in_sample_4h, in_sample_1d, in_sample_days)
            in_results.append(r)
        except Exception as exc:
            print(f"  combo {combo} failed: {exc}")

    # Find baseline result
    baseline = next(
        (r for r in in_results if r.stop == REF["stop"] and r.tp == REF["tp"]
         and r.dist == REF["dist"] and r.vol == REF["vol"]),
        None,
    )

    print(f"\n{'─' * 100}")
    print(f"IN-SAMPLE RESULTS (years 1-2, ~{in_sample_days} days)")
    print(f"{'─' * 100}")
    if baseline is None:
        print(f"  Baseline ({REF}) NOT in grid — adding manually")
        baseline = _run_combo(REF, in_sample_4h, in_sample_1d, in_sample_days)
        in_results.append(baseline)
    print(f"  Baseline: SL={REF['stop']:.2f} TP={REF['tp']:.2f} dist={REF['dist']:.2f} vol={REF['vol']:.1f}  "
          f"→ Ann={baseline.annual*100:+.1f}%  PF={baseline.pf:.2f}  DD=-{baseline.dd:.1f}%")
    _print_top(in_results, n=10, label="(in-sample)")

    # Pick top 5 candidates that beat baseline meaningfully
    candidates = [r for r in in_results
                  if r.annual > baseline.annual and r.dd <= baseline.dd + 5.0
                  and r.trades >= 50]
    candidates.sort(key=lambda r: -r.annual)
    candidates = candidates[:5]

    if not candidates:
        print(f"\n{'=' * 100}")
        print("VERDICT: NO-GO — no in-sample candidate beats baseline within DD budget")
        print("=" * 100)
        return

    # ── Out-of-sample test ────────────────────────────────────────────────────
    print(f"\n{'─' * 100}")
    print(f"OUT-OF-SAMPLE TEST (year 3, ~{out_sample_days} days)")
    print(f"{'─' * 100}")

    baseline_oos = _run_combo(REF, out_sample_4h, out_sample_1d, out_sample_days)
    print(f"  Baseline OOS: Ann={baseline_oos.annual*100:+.1f}%  PF={baseline_oos.pf:.2f}  DD=-{baseline_oos.dd:.1f}%")

    print(f"\n  Testing top 5 in-sample winners on out-of-sample data…")
    print(f"  {'Stop':>5}  {'TP':>5}  {'Dist':>5}  {'Vol':>4}    "
          f"{'IS Ann':>7}  {'OOS Ann':>8}  {'OOS PF':>6}  {'OOS DD':>7}  Verdict")
    survivors: list[tuple[GridResult, GridResult]] = []
    for cand in candidates:
        oos = _run_combo({"stop": cand.stop, "tp": cand.tp, "dist": cand.dist, "vol": cand.vol},
                         out_sample_4h, out_sample_1d, out_sample_days)
        beats_oos = oos.annual > baseline_oos.annual and oos.dd <= baseline_oos.dd + 5.0
        verdict = "ROBUST" if beats_oos else "OVERFIT"
        if beats_oos:
            survivors.append((cand, oos))
        print(
            f"  {cand.stop:>5.2f}  {cand.tp:>5.2f}  {cand.dist:>5.2f}  {cand.vol:>4.1f}    "
            f"{cand.annual*100:>+6.1f}%  {oos.annual*100:>+7.1f}%  {oos.pf:>6.2f}  "
            f"-{oos.dd:>5.1f}%  {verdict}"
        )

    # ── Final verdict ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("VERDICT")
    print(f"{'=' * 100}")
    if survivors:
        best_is, best_oos = max(survivors, key=lambda pair: pair[1].annual)
        print(f"  GO — {len(survivors)}/{len(candidates)} candidates survived out-of-sample.")
        print(f"  Best ROBUST config:  SL={best_is.stop:.2f}  TP={best_is.tp:.2f}  "
              f"dist={best_is.dist:.2f}  vol={best_is.vol:.1f}")
        print(f"    In-sample (2y):    Ann={best_is.annual*100:+.1f}%   PF={best_is.pf:.2f}   DD=-{best_is.dd:.1f}%")
        print(f"    Out-of-sample (1y): Ann={best_oos.annual*100:+.1f}%  PF={best_oos.pf:.2f}  DD=-{best_oos.dd:.1f}%")
        print(f"    vs current baseline OOS: Ann={baseline_oos.annual*100:+.1f}%  PF={baseline_oos.pf:.2f}")
        print(f"\n  RECOMMENDATION: Update DB runtime config to these values.")
    else:
        print(f"  NO-GO — 0/{len(candidates)} candidates survived out-of-sample.")
        print(f"  All in-sample winners were OVERFIT — they don't generalize.")
        print(f"  Keep current baseline: SL=1.5 TP=4.5 dist=1.0 vol=0.0")
    print()


if __name__ == "__main__":
    main()
