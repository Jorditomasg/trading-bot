"""Period-asymmetry analysis: validate why 1y→6m underperforms vs 6m→now.

Splits the recent 12 months into:
  Period A (BAD?):  2025-05-04 → 2025-11-04   (1y to 6m ago)
  Period B (GOOD?): 2025-11-04 → 2026-05-04   (last 6m)

Plus context:
  Period C (2024-H1): 2024-01-01 → 2024-07-01
  Period D (2024-H2): 2024-07-01 → 2025-01-01

For each period, runs:
- BacktestEngine with current optimal config (long_only, dist=1.0, TP=4.5, SL=1.5, risk=2%)
- RegimeDetector across all bars to get TRENDING/RANGING/VOLATILE distribution
- Trade-by-trade aggregation by regime at entry
- Volatility (ATR%) and trend metrics (ADX mean, EMA slope)

Run:
    PYTHONPATH=. venv/bin/python scripts/research/period_asymmetry.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.regime.detector").setLevel(logging.ERROR)

import pandas as pd
import numpy as np

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.regime.detector import RegimeDetector, RegimeDetectorConfig, MarketRegime
from bot.config_presets import get_regime_config
from bot.indicators import atr as compute_atr


@dataclass
class Period:
    name:  str
    start: str   # ISO date
    end:   str
    label: str   # short label


PERIODS = [
    Period("2024-H1",       "2024-01-01", "2024-07-01", "Bull early-2024"),
    Period("2024-H2",       "2024-07-01", "2025-01-01", "Bull late-2024"),
    Period("2025-05→11 BAD",  "2025-05-04", "2025-11-04", "1y→6m ago"),
    Period("2025-11→26 GOOD", "2025-11-04", "2026-05-04", "Last 6m"),
]


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end,   tz="UTC")
    mask = (df["open_time"] >= s) & (df["open_time"] < e)
    return df.loc[mask].reset_index(drop=True)


def _regime_distribution(df: pd.DataFrame, tf: str) -> dict:
    """Walk through df bar-by-bar and classify each bar's regime.

    Uses the same windowed approach as the live engine (no lookahead).
    """
    cfg = get_regime_config(tf)
    detector = RegimeDetector(cfg)
    min_lb = max(
        cfg.atr_period + cfg.atr_volatile_lookback,
        cfg.adx_period * 2,
        cfg.hurst_lookback,
    )

    counts = {MarketRegime.TRENDING: 0, MarketRegime.RANGING: 0, MarketRegime.VOLATILE: 0}
    if len(df) < min_lb + 1:
        return {"trending_pct": 0.0, "ranging_pct": 0.0, "volatile_pct": 0.0, "n_bars": 0}

    cols = ["open", "high", "low", "close", "volume"]
    for i in range(min_lb, len(df)):
        window = df.iloc[: i + 1][cols].reset_index(drop=True)
        regime = detector.detect(window)
        counts[regime] += 1

    n = sum(counts.values())
    return {
        "trending_pct": counts[MarketRegime.TRENDING] / n * 100 if n else 0.0,
        "ranging_pct":  counts[MarketRegime.RANGING]  / n * 100 if n else 0.0,
        "volatile_pct": counts[MarketRegime.VOLATILE] / n * 100 if n else 0.0,
        "n_bars": n,
    }


def _market_stats(df: pd.DataFrame, tf: str) -> dict:
    """Volatility, return, and trend metrics for the period."""
    if len(df) < 20:
        return {}
    atr_series = compute_atr(df, 14)
    avg_atr_pct = float((atr_series / df["close"]).mean() * 100)

    # Buy & hold return
    bh_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100

    # Std of log returns annualized (vol)
    log_ret = np.log(df["close"]).diff().dropna()
    bars_per_year = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}.get(tf, 6 * 365)
    realized_vol = float(log_ret.std() * np.sqrt(bars_per_year) * 100)

    # Hurst on full series (rough trendiness signature)
    closes = df["close"].values
    if len(closes) >= 100:
        from bot.regime.detector import RegimeDetector as RD
        rd = RD(get_regime_config(tf))
        hurst = rd._hurst_exponent(closes[-100:])  # last 100 bars
    else:
        hurst = float("nan")

    return {
        "buy_hold_pct":  bh_return,
        "avg_atr_pct":   avg_atr_pct,
        "ann_vol_pct":   realized_vol,
        "hurst_last100": hurst,
    }


def _run_backtest(
    df_4h: pd.DataFrame,
    df_1d: pd.DataFrame,
    period: Period,
    risk: float = 0.02,
) -> dict:
    """Run BacktestEngine with current optimal config on the period."""
    df_4h_p = _slice(df_4h, period.start, period.end)
    df_1d_p = _slice(df_1d, period.start, period.end)

    if len(df_4h_p) < 200:
        return {"period": period.name, "skipped": True, "reason": f"only {len(df_4h_p)} 4h bars"}

    cfg = BacktestConfig(
        initial_capital = 10_000.0,
        risk_per_trade  = risk,
        timeframe       = "4h",
        long_only       = True,
        ema_stop_mult   = 1.5,
        ema_tp_mult     = 4.5,
    )
    engine = BacktestEngine(cfg)
    result = engine.run(df=df_4h_p, df_4h=df_1d_p, symbol="BTCUSDT")
    summary = engine.summary(result)

    days = (pd.Timestamp(period.end, tz="UTC") - pd.Timestamp(period.start, tz="UTC")).days
    annual = compute_annual_return(result.initial_capital, result.final_capital, days) * 100

    # Trades by regime at entry
    by_regime = {}
    for t in result.trades:
        if t["exit_reason"] == "END_OF_PERIOD":
            continue
        r = t.get("regime", "?")
        by_regime.setdefault(r, []).append(t)

    regime_breakdown = {}
    for r, lst in by_regime.items():
        wins = [t for t in lst if (t["pnl"] or 0) > 0]
        regime_breakdown[r] = {
            "n":          len(lst),
            "win_rate":   len(wins) / len(lst) * 100 if lst else 0.0,
            "total_pnl":  sum(t["pnl"] for t in lst),
            "avg_pnl":    sum(t["pnl"] for t in lst) / len(lst) if lst else 0.0,
        }

    return {
        "period":         period.name,
        "label":          period.label,
        "days":           days,
        "annual_pct":     annual,
        "total_pnl":      summary["total_pnl"],
        "total_pnl_pct":  summary["total_pnl_pct"],
        "max_dd_pct":     summary["max_drawdown_pct"],
        "sharpe":         summary["sharpe_ratio"],
        "pf":             summary["profit_factor"],
        "win_rate_pct":   summary["win_rate_pct"],
        "n_trades":       summary["total_trades"],
        "max_loss_streak": summary["max_loss_streak"],
        "by_regime":      regime_breakdown,
    }


def _print_table(rows: list[dict], stats: list[dict], regimes: list[dict]) -> None:
    print("\n" + "=" * 120)
    print("BACKTEST METRICS PER PERIOD (config: long_only, dist=1.0, TP=4.5, SL=1.5, risk=2%)")
    print("=" * 120)
    h = ["Period", "Label", "Days", "Trades", "WR%", "Annual%", "MaxDD%", "Sharpe", "PF", "TotalPnL$", "MaxLossStrk"]
    print(f"{h[0]:<22} {h[1]:<18} {h[2]:>5} {h[3]:>7} {h[4]:>6} {h[5]:>9} {h[6]:>8} {h[7]:>7} {h[8]:>6} {h[9]:>11} {h[10]:>11}")
    for r in rows:
        if r.get("skipped"):
            print(f"{r['period']:<22} SKIPPED — {r['reason']}")
            continue
        print(
            f"{r['period']:<22} {r['label']:<18} {r['days']:>5} {r['n_trades']:>7} "
            f"{r['win_rate_pct']:>6.1f} {r['annual_pct']:>9.2f} {r['max_dd_pct']:>8.2f} "
            f"{r['sharpe']:>7.2f} {r['pf']:>6.2f} {r['total_pnl']:>11.2f} {r['max_loss_streak']:>11}"
        )

    print("\n" + "=" * 120)
    print("MARKET STATS PER PERIOD")
    print("=" * 120)
    h = ["Period", "BuyHold%", "AvgATR%", "AnnVol%", "Hurst(last 100)"]
    print(f"{h[0]:<22} {h[1]:>10} {h[2]:>9} {h[3]:>9} {h[4]:>17}")
    for s in stats:
        print(
            f"{s['period']:<22} {s['buy_hold_pct']:>10.2f} {s['avg_atr_pct']:>9.3f} "
            f"{s['ann_vol_pct']:>9.2f} {s['hurst_last100']:>17.4f}"
        )

    print("\n" + "=" * 120)
    print("REGIME DISTRIBUTION PER PERIOD (% of bars classified each)")
    print("=" * 120)
    h = ["Period", "TRENDING%", "RANGING%", "VOLATILE%", "Bars"]
    print(f"{h[0]:<22} {h[1]:>10} {h[2]:>10} {h[3]:>11} {h[4]:>7}")
    for r in regimes:
        print(
            f"{r['period']:<22} {r['trending_pct']:>10.1f} {r['ranging_pct']:>10.1f} "
            f"{r['volatile_pct']:>11.1f} {r['n_bars']:>7}"
        )

    print("\n" + "=" * 120)
    print("TRADES BY REGIME (at entry) — win rate and total PnL per period × regime")
    print("=" * 120)
    h = ["Period", "Regime", "N", "WR%", "TotalPnL$", "AvgPnL$"]
    print(f"{h[0]:<22} {h[1]:<12} {h[2]:>5} {h[3]:>6} {h[4]:>11} {h[5]:>10}")
    for r in rows:
        if r.get("skipped"):
            continue
        for regime, m in r["by_regime"].items():
            print(
                f"{r['period']:<22} {regime:<12} {m['n']:>5} {m['win_rate']:>6.1f} "
                f"{m['total_pnl']:>11.2f} {m['avg_pnl']:>10.2f}"
            )


def main() -> None:
    print("Loading cached BTC 4h and 1d data...")
    df_4h = pd.read_parquet("data/klines/BTCUSDT_4h.parquet")
    df_1d = pd.read_parquet("data/klines/BTCUSDT_1d.parquet")
    print(f"  4h: {df_4h['open_time'].min()} → {df_4h['open_time'].max()}  ({len(df_4h)} bars)")
    print(f"  1d: {df_1d['open_time'].min()} → {df_1d['open_time'].max()}  ({len(df_1d)} bars)")

    rows = []
    market_stats = []
    regimes = []

    for p in PERIODS:
        print(f"\n→ Running {p.name} ({p.start} → {p.end})")
        df_p = _slice(df_4h, p.start, p.end)
        if len(df_p) == 0:
            print(f"  skipped — no data in range")
            continue

        ms = _market_stats(df_p, "4h")
        ms["period"] = p.name
        market_stats.append(ms)

        rd = _regime_distribution(df_p, "4h")
        rd["period"] = p.name
        regimes.append(rd)

        bt = _run_backtest(df_4h, df_1d, p, risk=0.02)
        rows.append(bt)
        if not bt.get("skipped"):
            print(f"  trades={bt['n_trades']}  annual={bt['annual_pct']:.1f}%  PF={bt['pf']:.2f}  MaxDD={bt['max_dd_pct']:.1f}%")

    _print_table(rows, market_stats, regimes)


if __name__ == "__main__":
    main()
