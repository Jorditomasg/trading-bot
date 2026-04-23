#!/usr/bin/env python
"""CLI tool to compare 8 profitability scenarios via backtesting.

Usage:
    python scripts/compare_scenarios.py
    python scripts/compare_scenarios.py --symbol BTCUSDT --days 1095 --risk 0.02
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

from bot.backtest.cache import fetch_and_cache
from bot.backtest.scenario_runner import SCENARIOS, ScenarioResult, ScenarioRunner


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compare 8 backtest scenarios: 1h vs 4h, momentum filter, leverage 1–10×"
    )
    p.add_argument("--symbol", default="BTCUSDT", help="Trading pair (default: BTCUSDT)")
    p.add_argument("--days",   type=int,   default=1095, help="Lookback in days (default: 1095 = 3 years)")
    p.add_argument("--risk",   type=float, default=0.02, help="Risk per trade fraction (default: 0.02 = 2%%)")
    return p.parse_args()


def _fetch_data(symbol: str, days: int):
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)   # extra buffer for warmup

    print(f"Fetching data for {symbol} ({days} days)...")

    df_1h     = fetch_and_cache(symbol, "1h",  start_dt, end_dt)
    df_4h     = fetch_and_cache(symbol, "4h",  start_dt, end_dt)
    df_1d     = fetch_and_cache(symbol, "1d",  start_dt, end_dt)
    df_weekly = fetch_and_cache(symbol, "1w",  start_dt, end_dt)

    print(
        f"  1h: {len(df_1h):,} bars | "
        f"4h: {len(df_4h):,} bars | "
        f"1d: {len(df_1d):,} bars | "
        f"1w: {len(df_weekly):,} bars"
    )
    return df_1h, df_4h, df_1d, df_weekly


def _fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def _fmt_dd(v: float) -> str:
    return f"-{abs(v):.1f}%"


def _fmt_f(v: float, decimals: int = 2) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.{decimals}f}"


def _print_table(results: list[ScenarioResult]) -> None:
    col_w    = [26, 9, 8, 9, 7, 8, 13]
    headers  = ["Scenario", "Annual", "Sharpe", "Max DD", "PF", "Trades", "Liquidations"]

    sep      = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr_row  = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"

    print(sep)
    print(hdr_row)
    print(sep)

    for r in results:
        liq = "-" if r.scenario.leverage <= 1.0 else str(r.liquidations)
        row = [
            r.scenario.name,
            _fmt_pct(r.annual_return_pct),
            _fmt_f(r.sharpe_ratio),
            _fmt_dd(r.max_drawdown_pct),
            _fmt_f(r.profit_factor),
            str(r.total_trades),
            liq,
        ]
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")

    print(sep)


def main() -> None:
    args = _parse_args()

    try:
        df_1h, df_4h, df_1d, df_weekly = _fetch_data(args.symbol, args.days)
    except Exception as exc:
        print(f"ERROR fetching data: {exc}", file=sys.stderr)
        sys.exit(1)

    runner = ScenarioRunner(
        df_1h          = df_1h,
        df_4h          = df_4h,
        df_1d          = df_1d,
        df_weekly      = df_weekly,
        lookback_days  = args.days,
        risk_per_trade = args.risk,
    )

    print(f"\nRunning {len(SCENARIOS)} scenarios (this may take 1–2 minutes)...\n")
    results = runner.run_all(symbol=args.symbol)

    print(f"\n{'=' * 80}")
    print(
        f"  SCENARIO COMPARISON — {args.symbol}"
        f"  |  {args.days}d lookback"
        f"  |  {args.risk * 100:.0f}% risk/trade"
    )
    print(f"{'=' * 80}\n")
    _print_table(results)
    print()

    # Quick summary
    if results:
        best = max(results, key=lambda r: r.annual_return_pct)
        no_liq = [r for r in results if r.liquidations == 0]
        safest = min(no_liq, key=lambda r: r.max_drawdown_pct, default=None)
        print(f"  Best annual return : {best.scenario.name} ({_fmt_pct(best.annual_return_pct)})")
        if safest:
            print(f"  Lowest drawdown    : {safest.scenario.name} ({_fmt_dd(safest.max_drawdown_pct)})")
        print()


if __name__ == "__main__":
    main()
