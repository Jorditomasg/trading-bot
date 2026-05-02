#!/usr/bin/env python
"""Multi-symbol scan with the current optimal BTC config.

Runs the live-bot's validated configuration (4h, long_only, no momentum filter,
spot, current EMA params from config_presets) against a basket of pairs and
prints a side-by-side comparison sorted by profit factor.

Goal: identify which symbols survive as candidates for further work
(new strategies, parameter optimization, multi-asset portfolio).

Usage:
    python scripts/scan_symbols.py
    python scripts/scan_symbols.py --days 1095 --risk 0.02
    python scripts/scan_symbols.py --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT"]


@dataclass
class SymbolResult:
    symbol: str
    annual_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    total_trades: int
    win_rate_pct: float
    final_capital: float
    error: str | None = None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare current optimal config across multiple symbols")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help=f"Comma-separated pairs (default: {','.join(DEFAULT_SYMBOLS)})")
    p.add_argument("--days",    type=int,   default=1095, help="Lookback in days (default: 1095 = 3 years)")
    p.add_argument("--risk",    type=float, default=0.02, help="Risk per trade fraction (default: 0.02 = 2%%)")
    return p.parse_args()


def _run_symbol(symbol: str, days: int, risk: float) -> SymbolResult:
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    try:
        df_4h = fetch_and_cache(symbol, "4h", start_dt, end_dt)
        df_1d = fetch_and_cache(symbol, "1d", start_dt, end_dt)
    except Exception as exc:
        return SymbolResult(symbol, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, error=f"fetch failed: {exc}")

    if df_4h.empty or df_1d.empty:
        return SymbolResult(symbol, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, error="empty data")

    config = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = risk,
        timeframe         = "4h",
        leverage          = 1.0,
        long_only         = True,
    )
    engine = BacktestEngine(config)

    try:
        bt = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    except Exception as exc:
        return SymbolResult(symbol, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, error=f"backtest failed: {exc}")

    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)

    closed_trades = [t for t in bt.trades if t.get("action") == "CLOSE"]
    wins = sum(1 for t in closed_trades if (t.get("pnl") or 0.0) > 0)
    win_rate = (100.0 * wins / len(closed_trades)) if closed_trades else 0.0

    return SymbolResult(
        symbol            = symbol,
        annual_return_pct = annual,
        sharpe_ratio      = summary["sharpe_ratio"],
        max_drawdown_pct  = summary["max_drawdown_pct"],
        profit_factor     = summary["profit_factor"],
        total_trades      = summary["total_trades"],
        win_rate_pct      = win_rate,
        final_capital     = bt.final_capital,
    )


def _fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def _fmt_dd(v: float) -> str:
    return f"-{abs(v):.1f}%"


def _fmt_f(v: float, decimals: int = 2) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.{decimals}f}"


def _print_table(results: list[SymbolResult]) -> None:
    col_w   = [10, 9, 8, 9, 7, 8, 9]
    headers = ["Symbol", "Annual", "Sharpe", "Max DD", "PF", "Trades", "WinRate"]

    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr_row = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"

    print(sep)
    print(hdr_row)
    print(sep)

    for r in results:
        if r.error:
            row = [r.symbol, "ERROR", "-", "-", "-", "-", "-"]
            print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
            continue
        row = [
            r.symbol,
            _fmt_pct(r.annual_return_pct),
            _fmt_f(r.sharpe_ratio),
            _fmt_dd(r.max_drawdown_pct),
            _fmt_f(r.profit_factor),
            str(r.total_trades),
            f"{r.win_rate_pct:.1f}%",
        ]
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")

    print(sep)


def _classify(r: SymbolResult) -> str:
    """Tag each symbol: KEEP / WATCH / DISCARD based on PF and annual return."""
    if r.error:
        return "ERROR"
    if r.profit_factor >= 1.4 and r.annual_return_pct >= 0.15 and r.total_trades >= 20:
        return "KEEP"
    if r.profit_factor >= 1.2 and r.annual_return_pct >= 0.05:
        return "WATCH"
    return "DISCARD"


def main() -> None:
    args    = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"\n{'=' * 80}")
    print(f"  MULTI-SYMBOL SCAN (current optimal: 4h, long_only, dist=1.0, TP=4.5, SL=1.5)")
    print(f"  {args.days}d lookback  |  {args.risk * 100:.0f}% risk/trade  |  {len(symbols)} pairs")
    print(f"{'=' * 80}\n")

    results: list[SymbolResult] = []
    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}…", flush=True)
        r = _run_symbol(sym, args.days, args.risk)
        results.append(r)
        if r.error:
            print(f"  → ERROR: {r.error}")
        else:
            print(
                f"  → PF={_fmt_f(r.profit_factor)}  "
                f"Ann={_fmt_pct(r.annual_return_pct)}  "
                f"DD={_fmt_dd(r.max_drawdown_pct)}  "
                f"Trades={r.total_trades}"
            )

    # Sort viable results by PF descending; errors at the bottom.
    results_sorted = sorted(
        results,
        key=lambda r: (r.error is not None, -r.profit_factor),
    )

    print(f"\n{'=' * 80}")
    print("  RESULTS (sorted by Profit Factor)")
    print(f"{'=' * 80}\n")
    _print_table(results_sorted)

    # Verdict
    print(f"\n{'=' * 80}")
    print("  VERDICT")
    print(f"{'=' * 80}\n")
    print("  KEEP    : PF ≥ 1.4 AND Annual ≥ 15% AND Trades ≥ 20")
    print("  WATCH   : PF ≥ 1.2 AND Annual ≥ 5%")
    print("  DISCARD : everything else\n")

    for r in results_sorted:
        verdict = _classify(r)
        if r.error:
            print(f"  [{verdict:7}] {r.symbol}  ← {r.error}")
        else:
            print(
                f"  [{verdict:7}] {r.symbol}  "
                f"PF={_fmt_f(r.profit_factor)}  Ann={_fmt_pct(r.annual_return_pct)}  "
                f"DD={_fmt_dd(r.max_drawdown_pct)}  Trades={r.total_trades}"
            )
    print()

    keepers = [r.symbol for r in results_sorted if _classify(r) == "KEEP"]
    watchers = [r.symbol for r in results_sorted if _classify(r) == "WATCH"]
    if keepers:
        print(f"  → Survivors for next phase (new strategies): {', '.join(keepers)}")
    elif watchers:
        print(f"  → No KEEPers; WATCH list: {', '.join(watchers)}")
    else:
        print("  → No surviving symbols at current config — try widening params.")
    print()


if __name__ == "__main__":
    main()
