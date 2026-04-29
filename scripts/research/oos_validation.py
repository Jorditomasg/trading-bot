"""Out-of-sample sanity check.

Splits 2022-04 → 2025-04 into TRAIN (first 2 years) and TEST (last 1 year),
then evaluates the winning config on TEST alone. If TEST behaves similarly to
the full-sample backtest, the result is unlikely to be overfit.

Also runs a robustness sweep: small perturbations of every key parameter to
show the optimum is on a stable plateau, not a knife-edge.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from scripts.research.hypotheses import annualize, base_cfg, _payoff
from scripts.research.round3b_macro_engine import MacroFilteredEngine

logging.disable(logging.CRITICAL)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"


def load_window(symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    df = pd.read_parquet(CACHE_DIR / f"{symbol}_{interval}.parquet")
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df[(df["open_time"] >= start) & (df["open_time"] <= end)].reset_index(drop=True)


def years_span(df: pd.DataFrame) -> float:
    return (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / (365.25 * 24 * 3600)


def run(label: str, df_4h, df_1d, df_1w, cfg: BacktestConfig, *, use_weekly=True, ema_period=None) -> dict:
    if ema_period is None:
        engine = BacktestEngine(cfg)
    else:
        engine = MacroFilteredEngine(cfg, df_4h, df_1d, ema_period)
    res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w if use_weekly else None, symbol="BTCUSDT")
    s = engine.summary(res)
    yrs = years_span(df_4h)
    return {
        "label":  label,
        "ann":    annualize(s["total_pnl_pct"], yrs),
        "sharpe": s["sharpe_ratio"],
        "pf":     s["profit_factor"],
        "dd":     s["max_drawdown_pct"],
        "trades": s["total_trades"],
        "wr":     s["win_rate_pct"],
        "payoff": _payoff(res.trades),
        "streak": s["max_loss_streak"],
        "yrs":    yrs,
    }


def fmt_row(r: dict) -> str:
    pf = f"{r['pf']:6.3f}" if r['pf'] != float('inf') else "   inf"
    po = f"{r['payoff']:6.3f}" if r['payoff'] != float('inf') else "   inf"
    return (f"{r['label']:<46} {r['ann']:>6.2f}% {r['sharpe']:>7.3f} "
            f"{pf} {r['dd']:>6.2f}% {r['trades']:>5d} {r['wr']:>5.1f}% {po}")


def main() -> None:
    H1c_BEST = dict(
        momentum_filter_enabled=True,
        momentum_sma_period=20,
        momentum_neutral_band=0.08,
    )
    BEST = base_cfg(**H1c_BEST, risk_per_trade=0.05)
    BEST_EMA = 200

    splits = [
        ("FULL  (2022-04 → 2025-04)", pd.Timestamp("2022-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")),
        ("TRAIN (2022-04 → 2024-04)", pd.Timestamp("2022-04-01", tz="UTC"), pd.Timestamp("2024-04-01", tz="UTC")),
        ("TEST  (2024-04 → 2025-04)", pd.Timestamp("2024-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")),
        ("Y1    (2022-04 → 2023-04)", pd.Timestamp("2022-04-01", tz="UTC"), pd.Timestamp("2023-04-01", tz="UTC")),
        ("Y2    (2023-04 → 2024-04)", pd.Timestamp("2023-04-01", tz="UTC"), pd.Timestamp("2024-04-01", tz="UTC")),
        ("Y3    (2024-04 → 2025-04)", pd.Timestamp("2024-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")),
    ]

    print(f"{'Split':<46} {'CAGR':>7} {'Sharpe':>7} {'PF':>6} {'MaxDD':>7} {'Trad':>5} {'WR%':>6} {'Payoff':>7}")
    print("-" * 110)

    print("\n=== BASELINE (no filters, risk 2%) ===")
    for name, s, e in splits:
        try:
            df_4h = load_window("BTCUSDT", "4h", s, e)
            df_1d = load_window("BTCUSDT", "1d", s, e)
            df_1w = load_window("BTCUSDT", "1w", s, e)
            r = run(name, df_4h, df_1d, df_1w, base_cfg(), use_weekly=False, ema_period=None)
            print(fmt_row(r))
        except Exception as exc:
            print(f"{name:<46}  failed: {exc}")

    print(f"\n=== BEST CONFIG (H1c band 8% + EMA{BEST_EMA}d, risk 5%) ===")
    for name, s, e in splits:
        try:
            df_4h = load_window("BTCUSDT", "4h", s, e)
            df_1d = load_window("BTCUSDT", "1d", s, e)
            df_1w = load_window("BTCUSDT", "1w", s, e)
            r = run(name, df_4h, df_1d, df_1w, BEST, use_weekly=True, ema_period=BEST_EMA)
            print(fmt_row(r))
        except Exception as exc:
            print(f"{name:<46}  failed: {exc}")

    # ── Robustness sweep: small perturbations of best config, full sample ─────
    print("\n=== ROBUSTNESS SWEEP (full 3yr) ===")
    df_4h = load_window("BTCUSDT", "4h", splits[0][1], splits[0][2])
    df_1d = load_window("BTCUSDT", "1d", splits[0][1], splits[0][2])
    df_1w = load_window("BTCUSDT", "1w", splits[0][1], splits[0][2])
    perturbations = [
        ("BEST (band 8%, EMA200d, risk 5%)",     {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05}, 200),
        ("band 5%, EMA200d, risk 5%",            {"momentum_neutral_band": 0.05, "risk_per_trade": 0.05}, 200),
        ("band 10%, EMA200d, risk 5%",           {"momentum_neutral_band": 0.10, "risk_per_trade": 0.05}, 200),
        ("band 8%, EMA100d, risk 5%",            {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05}, 100),
        ("band 8%, EMA150d, risk 5%",            {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05}, 150),
        ("band 8%, EMA250d, risk 5%",            {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05}, 250),
        ("band 8%, EMA200d, risk 4.5%",          {"momentum_neutral_band": 0.08, "risk_per_trade": 0.045}, 200),
        ("band 8%, EMA200d, risk 5.5%",          {"momentum_neutral_band": 0.08, "risk_per_trade": 0.055}, 200),
        ("SMA period 15 (vs 20)",                {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05, "momentum_sma_period": 15}, 200),
        ("SMA period 30 (vs 20)",                {"momentum_neutral_band": 0.08, "risk_per_trade": 0.05, "momentum_sma_period": 30}, 200),
    ]
    base_kwargs = dict(
        momentum_filter_enabled=True,
        momentum_sma_period=20,
    )
    for label, perturb, ep in perturbations:
        merged = {**base_kwargs}
        merged.update(perturb)
        cfg = base_cfg(**merged)
        r = run(label, df_4h, df_1d, df_1w, cfg, use_weekly=True, ema_period=ep)
        print(fmt_row(r))

    # ── APPLES-TO-APPLES: isolate filter at risk=2% across all splits ────────
    print("\n=== FILTER PURE VALUE (risk 2% — matches baseline) ===")
    for name, s, e in splits:
        df_4h = load_window("BTCUSDT", "4h", s, e)
        df_1d = load_window("BTCUSDT", "1d", s, e)
        df_1w = load_window("BTCUSDT", "1w", s, e)
        cfg = base_cfg(
            risk_per_trade=0.02,
            momentum_filter_enabled=True,
            momentum_sma_period=20,
            momentum_neutral_band=0.08,
        )
        r = run(name, df_4h, df_1d, df_1w, cfg, use_weekly=True, ema_period=200)
        print(fmt_row(r))

    # ── Calibrated risk: what risk level keeps DD ≤ 25% in worst subsample? ──
    print("\n=== RISK CALIBRATION on TEST year (2024-04→2025-04) ===")
    s, e = pd.Timestamp("2024-04-01", tz="UTC"), pd.Timestamp("2025-04-01", tz="UTC")
    df_4h = load_window("BTCUSDT", "4h", s, e)
    df_1d = load_window("BTCUSDT", "1d", s, e)
    df_1w = load_window("BTCUSDT", "1w", s, e)
    for risk_pct in [0.015, 0.02, 0.025, 0.03, 0.035, 0.04]:
        cfg = base_cfg(
            risk_per_trade=risk_pct,
            momentum_filter_enabled=True,
            momentum_sma_period=20,
            momentum_neutral_band=0.08,
        )
        r = run(f"risk={risk_pct*100:.1f}%", df_4h, df_1d, df_1w, cfg, use_weekly=True, ema_period=200)
        print(fmt_row(r))


if __name__ == "__main__":
    main()
