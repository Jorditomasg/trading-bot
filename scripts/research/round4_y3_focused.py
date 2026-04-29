"""Round 4: focused Y3-OOS comparison of weekly-momentum-only configs.

Daily-EMA filter was over-restrictive in Y3 (PF 1.24 vs baseline 1.33).
Test if H1c WITHOUT the daily EMA still adds genuine value across all subsamples,
or whether the documented baseline is already the honest optimum.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from scripts.research.hypotheses import annualize, base_cfg, _payoff
from scripts.research.round3b_macro_engine import MacroFilteredEngine

logging.disable(logging.CRITICAL)
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "klines"


def load_window(symbol, interval, s, e):
    df = pd.read_parquet(CACHE_DIR / f"{symbol}_{interval}.parquet")
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df[(df["open_time"] >= s) & (df["open_time"] <= e)].reset_index(drop=True)


def years_span(df):
    return (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / (365.25 * 24 * 3600)


def run(label, df_4h, df_1d, df_1w, cfg, *, use_weekly=True, ema_period=None):
    engine = BacktestEngine(cfg) if ema_period is None else MacroFilteredEngine(cfg, df_4h, df_1d, ema_period)
    res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w if use_weekly else None, symbol="BTCUSDT")
    s = engine.summary(res)
    yrs = years_span(df_4h)
    return {
        "label": label,
        "ann":   annualize(s["total_pnl_pct"], yrs),
        "sharpe": s["sharpe_ratio"],
        "pf":    s["profit_factor"],
        "dd":    s["max_drawdown_pct"],
        "trades": s["total_trades"],
        "wr":    s["win_rate_pct"],
        "payoff": _payoff(res.trades),
    }


def fmt(r):
    pf = f"{r['pf']:6.3f}" if r['pf'] != float('inf') else "   inf"
    po = f"{r['payoff']:6.3f}" if r['payoff'] != float('inf') else "   inf"
    return f"{r['label']:<48} {r['ann']:>6.2f}% {r['sharpe']:>7.3f} {pf} {r['dd']:>6.2f}% {r['trades']:>4d} {r['wr']:>5.1f}% {po}"


def main():
    splits = [
        ("FULL  (2022-04→2025-04)", pd.Timestamp("2022-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")),
        ("Y1    (2022-04→2023-04)", pd.Timestamp("2022-04-01", tz="UTC"), pd.Timestamp("2023-04-01", tz="UTC")),
        ("Y2    (2023-04→2024-04)", pd.Timestamp("2023-04-01", tz="UTC"), pd.Timestamp("2024-04-01", tz="UTC")),
        ("Y3    (2024-04→2025-04)", pd.Timestamp("2024-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")),
    ]

    H1c = dict(momentum_filter_enabled=True, momentum_sma_period=20, momentum_neutral_band=0.08)

    CFGS = [
        ("Baseline (risk 2%)",                     base_cfg(),                                   False, None),
        ("Baseline (risk 3%)",                     base_cfg(risk_per_trade=0.03),                False, None),
        ("H1c only (risk 2%)",                     base_cfg(**H1c),                              True,  None),
        ("H1c only (risk 2.5%)",                   base_cfg(**H1c, risk_per_trade=0.025),        True,  None),
        ("H1c only (risk 3%)",                     base_cfg(**H1c, risk_per_trade=0.03),         True,  None),
        ("H1c only (risk 3.5%)",                   base_cfg(**H1c, risk_per_trade=0.035),        True,  None),
        ("H1c only (risk 4%)",                     base_cfg(**H1c, risk_per_trade=0.04),         True,  None),
        ("H1c+EMA200d (risk 2%)",                  base_cfg(**H1c),                              True,  200),
        ("H1c+EMA200d (risk 3%)",                  base_cfg(**H1c, risk_per_trade=0.03),         True,  200),
    ]

    hdr = f"{'Config':<48} {'CAGR':>7} {'Sharpe':>7} {'PF':>6} {'MaxDD':>7} {'Trad':>4} {'WR%':>6} {'Payoff':>7}"
    for split_name, s, e in splits:
        df_4h = load_window("BTCUSDT", "4h", s, e)
        df_1d = load_window("BTCUSDT", "1d", s, e)
        df_1w = load_window("BTCUSDT", "1w", s, e)
        print(f"\n──── {split_name} ────")
        print(hdr)
        print("-" * len(hdr))
        for label, cfg, uw, ep in CFGS:
            r = run(label, df_4h, df_1d, df_1w, cfg, use_weekly=uw, ema_period=ep)
            # ★ flag = strictly meets all criteria (CAGR>22.5, PF>=1.3, DD<=25)
            ok = (r["ann"] > 22.57 and r["pf"] >= 1.3 and r["dd"] <= 25.0)
            flag = " ★" if ok else ""
            print(fmt(r) + flag)


if __name__ == "__main__":
    main()
