#!/usr/bin/env python3
"""Empirical validation of SL/TP wick-detection variants.

The live bot currently checks SL/TP only against `live_tick.price` (close-like),
while the backtest engine uses the bar's high/low (intra-bar idealised).
This script quantifies the gap by running 3 variants over the same 3-year
BTC 4h dataset:

    V0  baseline  — high/low detection, exit at SL/TP level    (current backtest)
    V1  live-like — close-only detection, exit at bar close    (current live bug)
    V2  fix       — high/low detection, exit at bar close      (proposed fix)

Output: comparison table of trades, win rate, PF, Sharpe, ann return, max DD.

Decision rule:
    if V2 ≫ V1                  → wick fix improves real numbers (ship it)
    if V2 ≈ V0                  → option B (close at spot) is practically idealised
    if V0 ≫ V2                  → backtest is over-optimistic; document in CLAUDE.md
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
)
from bot.backtest.fetcher import fetch_historical_klines

logging.basicConfig(
    level=logging.WARNING,  # silent — only print final table
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)


# ── Variant engines ───────────────────────────────────────────────────────────


class CloseOnlyEngine(BacktestEngine):
    """V1 — simulates the live bot's bug.

    Detection: only the bar's CLOSE is compared against SL/TP. Wicks that touched
    the level but the bar closed back inside are NOT detected.
    Exit price: the bar's close (which is what `live_tick.price` would have been).
    """

    def _check_exit(self, trade: dict, bar: pd.Series):
        close = float(bar["close"])
        sl    = trade["stop_loss"]
        tp    = trade["take_profit"]

        if trade["side"] == "BUY":
            if close <= sl:
                return EXIT_STOP_LOSS, close
            if close >= tp:
                return EXIT_TAKE_PROFIT, close
        else:  # SELL
            if close >= sl:
                return EXIT_STOP_LOSS, close
            if close <= tp:
                return EXIT_TAKE_PROFIT, close
        return None


class HighLowCloseEngine(BacktestEngine):
    """V2 — proposed fix: detect with high/low, exit at the bar's close.

    Detection: high/low captures intra-bar wicks (matches V0 baseline).
    Exit price: the bar's close — proxy for the spot price the live bot will
    fill at when sending a market order after detecting the wick a few seconds
    later. Honest about real-world execution.
    """

    def _check_exit(self, trade: dict, bar: pd.Series):
        high  = float(bar["high"])
        low   = float(bar["low"])
        close = float(bar["close"])
        sl    = trade["stop_loss"]
        tp    = trade["take_profit"]

        if trade["side"] == "BUY":
            if low <= sl:
                return EXIT_STOP_LOSS, close
            if high >= tp:
                return EXIT_TAKE_PROFIT, close
        else:  # SELL
            if high >= sl:
                return EXIT_STOP_LOSS, close
            if low <= tp:
                return EXIT_TAKE_PROFIT, close
        return None


# ── Runner ────────────────────────────────────────────────────────────────────


def _make_config() -> BacktestConfig:
    """Optimal config from CLAUDE.md baseline (long_only, dist=1.0, TP=4.5, SL=1.5)."""
    return BacktestConfig(
        initial_capital      = 10_000.0,
        risk_per_trade       = 0.01,
        timeframe            = "4h",
        long_only            = True,
        ema_stop_mult        = 1.5,
        ema_tp_mult          = 4.5,
        ema_max_distance_atr = 1.0,
    )


def _run_variant(
    name: str,
    engine_cls: type[BacktestEngine],
    df: pd.DataFrame,
    df_4h: pd.DataFrame | None,
) -> dict:
    cfg    = _make_config()
    engine = engine_cls(cfg)
    result = engine.run(df, df_4h=df_4h, symbol="BTCUSDT")
    summary = engine.summary(result)

    # Annualised return from total return + period length
    bars   = result.total_bars
    years  = bars * 4 / (24 * 365)  # 4h bars
    total  = summary["total_pnl_pct"] / 100.0
    if years > 0:
        ann = ((1.0 + total) ** (1.0 / years) - 1.0) * 100.0
    else:
        ann = 0.0

    return {
        "name":        name,
        "trades":      summary["total_trades"],
        "win_rate":    summary["win_rate_pct"],
        "pf":          summary["profit_factor"],
        "sharpe":      summary["sharpe_ratio"],
        "ann_pct":     ann,
        "max_dd_pct":  summary["max_drawdown_pct"],
        "total_pct":   summary["total_pnl_pct"],
    }


def _print_table(rows: list[dict]) -> None:
    print()
    print("─" * 88)
    print(f"  {'Variant':<32}{'Trades':>8}{'WinRate':>10}{'PF':>8}{'Sharpe':>9}{'Ann%':>9}{'MaxDD%':>9}")
    print("─" * 88)
    for r in rows:
        pf_s = f"{r['pf']:.3f}" if r["pf"] != float("inf") else "  ∞"
        print(
            f"  {r['name']:<32}"
            f"{r['trades']:>8}"
            f"{r['win_rate']:>9.1f}%"
            f"{pf_s:>8}"
            f"{r['sharpe']:>9.3f}"
            f"{r['ann_pct']:>8.2f}%"
            f"{r['max_dd_pct']:>8.2f}%"
        )
    print("─" * 88)


def _print_deltas(rows: list[dict]) -> None:
    """Highlight the gap V0→V1 (current bug) and V1→V2 (improvement from fix)."""
    by_name = {r["name"]: r for r in rows}
    v0 = by_name["V0 baseline (high/low + level)"]
    v1 = by_name["V1 live-like (close + close)"]
    v2 = by_name["V2 fix (high/low + close)"]

    print()
    print("  IMPACT ANALYSIS")
    print("─" * 88)
    print(f"  Current bug cost   (V0 → V1):  ann {v0['ann_pct']:>+6.2f}% → {v1['ann_pct']:>+6.2f}%   "
          f"Δ {v1['ann_pct']-v0['ann_pct']:>+6.2f} pp")
    print(f"  Fix improvement    (V1 → V2):  ann {v1['ann_pct']:>+6.2f}% → {v2['ann_pct']:>+6.2f}%   "
          f"Δ {v2['ann_pct']-v1['ann_pct']:>+6.2f} pp")
    print(f"  Residual gap       (V2 → V0):  ann {v2['ann_pct']:>+6.2f}% → {v0['ann_pct']:>+6.2f}%   "
          f"Δ {v0['ann_pct']-v2['ann_pct']:>+6.2f} pp  (idealisation premium)")
    print("─" * 88)
    print()


def main() -> int:
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=1095)  # 3 years

    print(f"Fetching BTCUSDT 4h klines  {start_dt:%Y-%m-%d} → {end_dt:%Y-%m-%d}…")
    df    = fetch_historical_klines("BTCUSDT", "4h", start_dt, end_dt)
    df_4h = fetch_historical_klines("BTCUSDT", "1d", start_dt, end_dt)  # bias TF

    print(f"  primary  bars: {len(df):,}")
    print(f"  bias(1d) bars: {len(df_4h):,}")

    variants = [
        ("V0 baseline (high/low + level)", BacktestEngine),
        ("V1 live-like (close + close)",   CloseOnlyEngine),
        ("V2 fix (high/low + close)",      HighLowCloseEngine),
    ]

    rows = []
    for name, cls in variants:
        print(f"\n  running {name}…")
        rows.append(_run_variant(name, cls, df, df_4h))

    _print_table(rows)
    _print_deltas(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
