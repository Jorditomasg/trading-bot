#!/usr/bin/env python
"""A/B test the endogenous news-pause filter against the current optimal config.

Variants tested (per symbol):
    Control        — no pause filter (current baseline)
    ATR-only       — pause when ATR > 3× rolling mean (vol_mult ignored)
    Volume-only    — pause when volume > 5× rolling mean (atr_mult ignored)
    OR  (3× / 5×)  — either spike triggers (loose)
    AND (3× / 5×)  — both spikes required (strict)

Compare on PF, Annual return, Max DD, Sharpe, total trades, and number of
pause triggers fired. Prints a per-symbol table and a final verdict
(BETTER / NEUTRAL / WORSE) per variant.

Usage:
    python scripts/compare_pause.py
    python scripts/compare_pause.py --symbols BTCUSDT,ETHUSDT --days 1095
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
# Silence the BiasFilter "no 4h data" warnings — we DO pass df_4h, but the warnings
# come from edge-of-window slices that have no data. Not a real issue.
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.risk.news_pause import NewsPauseConfig

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


@dataclass
class Variant:
    name: str
    pause: NewsPauseConfig | None  # None = control


VARIANTS: list[Variant] = [
    Variant("Control",        None),
    # Conservative — original
    Variant("ATR3x w6",       NewsPauseConfig(enabled=True, atr_mult=3.0, vol_mult=999.0, mode="OR",  bars_after=6)),
    Variant("Vol5x w6",       NewsPauseConfig(enabled=True, atr_mult=999.0, vol_mult=5.0, mode="OR",  bars_after=6)),
    Variant("OR3x/5x w6",     NewsPauseConfig(enabled=True, atr_mult=3.0,   vol_mult=5.0, mode="OR",  bars_after=6)),
    # Aggressive thresholds — should actually intersect entries
    Variant("ATR2x w12",      NewsPauseConfig(enabled=True, atr_mult=2.0, vol_mult=999.0, mode="OR",  bars_after=12)),
    Variant("Vol3x w12",      NewsPauseConfig(enabled=True, atr_mult=999.0, vol_mult=3.0, mode="OR",  bars_after=12)),
    Variant("OR2x/3x w12",    NewsPauseConfig(enabled=True, atr_mult=2.0,   vol_mult=3.0, mode="OR",  bars_after=12)),
    Variant("AND2x/3x w12",   NewsPauseConfig(enabled=True, atr_mult=2.0,   vol_mult=3.0, mode="AND", bars_after=12)),
    # Very aggressive — to confirm the mechanism works at all
    Variant("OR1.5x/2x w24",  NewsPauseConfig(enabled=True, atr_mult=1.5,   vol_mult=2.0, mode="OR",  bars_after=24)),
]


@dataclass
class VariantResult:
    variant: Variant
    annual_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    total_trades: int
    pause_triggers: int


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare endogenous news-pause variants vs control")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help=f"Comma-separated pairs (default: {','.join(DEFAULT_SYMBOLS)})")
    p.add_argument("--days",    type=int,   default=1095, help="Lookback days (default: 1095 = 3y)")
    p.add_argument("--risk",    type=float, default=0.02, help="Risk per trade (default: 0.02 = 2%%)")
    return p.parse_args()


def _run_variant(
    variant: Variant,
    df_4h, df_1d,
    symbol: str,
    days: int,
    risk: float,
) -> VariantResult:
    config = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = risk,
        timeframe         = "4h",
        cost_per_side_pct = 0.0015,
        leverage          = 1.0,
        long_only         = True,
        news_pause        = variant.pause,
    )
    engine = BacktestEngine(config)
    bt     = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)

    return VariantResult(
        variant           = variant,
        annual_return_pct = annual,
        sharpe_ratio      = summary["sharpe_ratio"],
        max_drawdown_pct  = summary["max_drawdown_pct"],
        profit_factor     = summary["profit_factor"],
        total_trades      = summary["total_trades"],
        pause_triggers    = bt.news_pause_triggers,
    )


def _fmt_pct(v: float) -> str:
    return f"{v * 100:+.1f}%"


def _fmt_dd(v: float) -> str:
    return f"-{abs(v):.1f}%"


def _fmt_f(v: float, decimals: int = 2) -> str:
    if v == float("inf"):
        return "inf"
    return f"{v:.{decimals}f}"


def _print_table(symbol: str, results: list[VariantResult]) -> None:
    col_w   = [12, 9, 8, 9, 7, 8, 10]
    headers = ["Variant", "Annual", "Sharpe", "Max DD", "PF", "Trades", "Triggers"]

    print(f"\n── {symbol} ──")
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr_row = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"
    print(sep)
    print(hdr_row)
    print(sep)

    for r in results:
        row = [
            r.variant.name,
            _fmt_pct(r.annual_return_pct),
            _fmt_f(r.sharpe_ratio),
            _fmt_dd(r.max_drawdown_pct),
            _fmt_f(r.profit_factor),
            str(r.total_trades),
            str(r.pause_triggers),
        ]
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
    print(sep)


def _verdict(control: VariantResult, variant: VariantResult) -> str:
    """Return BETTER / NEUTRAL / WORSE based on PF + DD + annual return."""
    pf_delta     = variant.profit_factor - control.profit_factor
    annual_delta = variant.annual_return_pct - control.annual_return_pct
    dd_delta     = variant.max_drawdown_pct - control.max_drawdown_pct  # negative is better

    # BETTER: PF and Annual both up (or one up, other flat) AND DD not significantly worse.
    if pf_delta > 0.05 and annual_delta > 0.0 and dd_delta < 2.0:
        return "BETTER"
    if pf_delta < -0.05 or annual_delta < -0.02:
        return "WORSE"
    return "NEUTRAL"


def main() -> None:
    args    = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"\n{'=' * 80}")
    print(f"  PAUSE FILTER A/B TEST  —  {args.days}d  |  {args.risk * 100:.0f}% risk  |  {len(symbols)} symbols")
    print(f"{'=' * 80}")

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + 30)

    all_results: dict[str, list[VariantResult]] = {}

    for sym in symbols:
        print(f"\nFetching {sym}…", flush=True)
        try:
            df_4h = fetch_and_cache(sym, "4h", start_dt, end_dt)
            df_1d = fetch_and_cache(sym, "1d", start_dt, end_dt)
        except Exception as exc:
            print(f"  ERROR fetching {sym}: {exc}", file=sys.stderr)
            continue

        results: list[VariantResult] = []
        for v in VARIANTS:
            print(f"  → {v.name}…", flush=True)
            try:
                r = _run_variant(v, df_4h, df_1d, sym, args.days, args.risk)
                results.append(r)
            except Exception as exc:
                print(f"    ERROR: {exc}", file=sys.stderr)

        all_results[sym] = results
        _print_table(sym, results)

    # ── Final verdict per variant ────────────────────────────────────────────
    print(f"\n\n{'=' * 80}")
    print("  VERDICT PER VARIANT (vs Control)")
    print(f"{'=' * 80}\n")

    for sym, results in all_results.items():
        if not results:
            continue
        control = next((r for r in results if r.variant.name == "Control"), None)
        if control is None:
            continue
        print(f"  {sym}:")
        for r in results:
            if r.variant.name == "Control":
                continue
            v = _verdict(control, r)
            d_pf  = r.profit_factor - control.profit_factor
            d_ann = (r.annual_return_pct - control.annual_return_pct) * 100
            d_dd  = r.max_drawdown_pct - control.max_drawdown_pct
            print(
                f"    [{v:7}] {r.variant.name:12}  "
                f"ΔPF={d_pf:+.2f}  ΔAnn={d_ann:+.1f}pp  ΔDD={d_dd:+.1f}pp  "
                f"triggers={r.pause_triggers}"
            )
        print()


if __name__ == "__main__":
    main()
