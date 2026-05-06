"""Validate partial profit ladder — grid over TP1 mult x close fraction."""

from __future__ import annotations

import logging
logging.basicConfig(level=logging.WARNING)
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return


PERIODS = [
    ("FULL 3y",        "2022-05-01", "2026-05-04"),
    ("Bad 2025-05→11", "2025-05-04", "2025-11-04"),
    ("Good last 6m",   "2025-11-04", "2026-05-04"),
    ("Bull 2024",      "2024-01-01", "2025-01-01"),
]

VARIANTS = [
    ("BASELINE",            None, 0.5),
    ("TP1=1.5 frac 0.5",    1.5,  0.5),
    ("TP1=2.0 frac 0.5",    2.0,  0.5),
    ("TP1=2.5 frac 0.5",    2.5,  0.5),
    ("TP1=3.0 frac 0.5",    3.0,  0.5),
    ("TP1=2.0 frac 0.33",   2.0,  0.33),
    ("TP1=2.0 frac 0.66",   2.0,  0.66),
    ("TP1=2.5 frac 0.66",   2.5,  0.66),
    ("TP1=2.0 frac 0.40",   2.0,  0.40),
]


def _slice(df, start, end):
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end,   tz="UTC")
    return df.loc[(df["open_time"] >= s) & (df["open_time"] < e)].reset_index(drop=True)


def _run(df_4h, df_1d, period_name, start, end, p_tp_mult, p_frac):
    df_p   = _slice(df_4h, start, end)
    df_1dp = _slice(df_1d, start, end)
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.02, timeframe="4h",
        long_only=True, ema_stop_mult=1.5, ema_tp_mult=4.5,
        partial_tp_atr_mult=p_tp_mult,
        partial_close_fraction=p_frac,
    )
    engine = BacktestEngine(cfg)
    bt     = engine.run(df=df_p, df_4h=df_1dp, symbol="BTCUSDT")
    s      = engine.summary(bt)
    days   = (pd.Timestamp(end, tz="UTC") - pd.Timestamp(start, tz="UTC")).days
    annual = compute_annual_return(bt.initial_capital, bt.final_capital, days) * 100
    n_partials = sum(1 for t in bt.trades if t.get("partial_taken"))
    return {
        "period": period_name, "trades": s["total_trades"], "wr": s["win_rate_pct"],
        "annual": annual, "dd": s["max_drawdown_pct"], "pf": s["profit_factor"],
        "sharpe": s["sharpe_ratio"], "partials": n_partials,
    }


def main():
    print("Loading...", flush=True)
    df_4h = pd.read_parquet("data/klines/BTCUSDT_4h.parquet")
    df_1d = pd.read_parquet("data/klines/BTCUSDT_1d.parquet")

    rows: list[dict] = []
    for vname, p_tp, p_fr in VARIANTS:
        for pname, ps, pe in PERIODS:
            print(f"  {vname:<22} on {pname}...", flush=True)
            r = _run(df_4h, df_1d, pname, ps, pe, p_tp, p_fr)
            r["variant"] = vname
            rows.append(r)
            print(f"    trades={r['trades']} partials={r['partials']} ann={r['annual']:.2f}% dd={r['dd']:.2f}% pf={r['pf']:.2f}", flush=True)

    print("\n" + "=" * 110, flush=True)
    print("RESULTS BY PERIOD", flush=True)
    print("=" * 110, flush=True)
    for pname, _, _ in PERIODS:
        print(f"\n--- {pname} ---", flush=True)
        print(f"{'Variant':<22} {'Trades':>6} {'Partials':>9} {'WR%':>5} {'Annual%':>9} {'MaxDD%':>7} {'PF':>5} {'Sharpe':>7}", flush=True)
        baseline = next((r for r in rows if r["period"] == pname and r["variant"] == "BASELINE"), None)
        for r in rows:
            if r["period"] != pname:
                continue
            stars = ""
            if baseline and r["variant"] != "BASELINE":
                better = sum([
                    r["annual"] >= baseline["annual"],
                    r["dd"]     <= baseline["dd"],
                    r["pf"]     >= baseline["pf"],
                ])
                stars = "★" * better
            print(
                f"{r['variant']:<22} {r['trades']:>6} {r['partials']:>9} {r['wr']:>5.1f} "
                f"{r['annual']:>9.2f} {r['dd']:>7.2f} {r['pf']:>5.2f} {r['sharpe']:>7.2f}  {stars}",
                flush=True,
            )

    # ── verdict ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 110, flush=True)
    print("VERDICT — FULL 3y", flush=True)
    print("=" * 110, flush=True)
    full = {r["variant"]: r for r in rows if r["period"] == "FULL 3y"}
    bad  = {r["variant"]: r for r in rows if r["period"] == "Bad 2025-05→11"}
    base_full = full["BASELINE"]
    base_bad  = bad["BASELINE"]
    print(f"Baseline FULL 3y: ann={base_full['annual']:.2f}% DD={base_full['dd']:.2f}% PF={base_full['pf']:.2f}", flush=True)
    print(f"Baseline Bad:     ann={base_bad['annual']:.2f}%", flush=True)
    print()
    found = False
    for vname, _, _ in VARIANTS:
        if vname == "BASELINE":
            continue
        rf = full[vname]
        rb = bad[vname]
        rel_ann = (rf["annual"] - base_full["annual"]) / abs(base_full["annual"]) * 100
        pf_rel  = (rf["pf"] - base_full["pf"]) / base_full["pf"] * 100
        ok_dd   = rf["dd"] <= base_full["dd"] + 0.5  # similar or better DD
        ok_pf   = pf_rel  >= -3.0
        ok_ann  = rel_ann >= -5.0
        ok_bad  = rb["annual"] >= base_bad["annual"] - 1.0  # within 1pp of baseline OR better
        marker  = "✓" if (ok_dd and ok_pf and ok_ann and ok_bad) else "✗"
        if marker == "✓":
            found = True
        print(
            f"  {marker} {vname:<22} | 3y: ann={rf['annual']:.2f}% (rel {rel_ann:+.1f}%) "
            f"DD={rf['dd']:.2f}% PF={rf['pf']:.2f} ({pf_rel:+.1f}%) | Bad: ann={rb['annual']:.2f}%",
            flush=True,
        )
    if not found:
        print("\n  No variant fully passes — partial ladder doesn't beat baseline on this criterion.", flush=True)


if __name__ == "__main__":
    main()
