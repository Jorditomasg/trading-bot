#!/usr/bin/env python
"""Deep strategy comparison on BTC (and optionally other symbols).

Runs each candidate strategy through the same BacktestEngine machinery
(bias filter, costs, SL/TP, risk sizing) over the full lookback. Reports
side-by-side metrics so you can pick the best for the next phase.

Strategies tested:
    EMA_CROSSOVER       — current baseline
    MACD                — momentum from EMA12/26 + signal9
    SUPERTREND          — ATR trailing-band trend follower
    DONCHIAN_BREAKOUT   — N-bar high/low channel breakout
    BOLLINGER_REVERSION — BB(20,2) + RSI mean-reversion
    HEIKIN_ASHI         — N consecutive strong HA candles

Usage:
    python scripts/compare_strategies.py
    python scripts/compare_strategies.py --symbols BTCUSDT,ETHUSDT --days 1095
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.config_presets import get_strategy_configs
from bot.constants import StrategyName
from bot.strategy.bollinger_reversion import BollingerReversionConfig, BollingerReversionStrategy
from bot.strategy.donchian_breakout import DonchianConfig, DonchianBreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.heikin_ashi import HeikinAshiConfig, HeikinAshiStrategy
from bot.strategy.macd import MACDConfig, MACDStrategy
from bot.strategy.supertrend import SupertrendConfig, SupertrendStrategy

DEFAULT_SYMBOLS = ["BTCUSDT"]


@dataclass
class StrategyResult:
    name: str
    annual_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    total_trades: int
    win_rate_pct: float
    avg_pnl_per_trade: float
    final_capital: float
    error: str | None = None


def _build_strategies(long_only: bool) -> list[object]:
    """Construct each candidate. Use the 4h preset for EMA so the baseline is identical."""
    ema_cfg = dict(get_strategy_configs("4h")[StrategyName.EMA_CROSSOVER])
    ema_cfg["long_only"] = long_only

    return [
        EMACrossoverStrategy(EMACrossoverConfig(**ema_cfg)),
        MACDStrategy(MACDConfig(long_only=long_only)),
        SupertrendStrategy(SupertrendConfig(long_only=long_only)),
        DonchianBreakoutStrategy(DonchianConfig(long_only=long_only)),
        BollingerReversionStrategy(BollingerReversionConfig(long_only=long_only)),
        HeikinAshiStrategy(HeikinAshiConfig(long_only=long_only)),
    ]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deep strategy comparison on BTC and friends")
    p.add_argument("--symbols",   default=",".join(DEFAULT_SYMBOLS),
                   help=f"Comma-separated pairs (default: {','.join(DEFAULT_SYMBOLS)})")
    p.add_argument("--days",      type=int,   default=1095, help="Lookback (default 1095 = 3y)")
    p.add_argument("--risk",      type=float, default=0.02, help="Risk per trade (default 0.02 = 2%%)")
    p.add_argument("--long-only", action="store_true", default=True,
                   help="Long-only mode (default True for BTC)")
    p.add_argument("--bidir",     action="store_true",
                   help="Override long-only and allow shorts")
    return p.parse_args()


def _run_strategy(
    strategy: object,
    df_4h, df_1d,
    symbol: str,
    days: int,
    risk: float,
) -> StrategyResult:
    name = getattr(strategy, "name", strategy.__class__.__name__)
    try:
        config = BacktestConfig(
            initial_capital   = 10_000.0,
            risk_per_trade    = risk,
            timeframe         = "4h",
            cost_per_side_pct = 0.0015,
            leverage          = 1.0,
            long_only         = False,  # the strategy itself decides; engine flag only affects EMA cfg
        )
        engine = BacktestEngine(config, strategy=strategy)
        bt     = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    except Exception as exc:
        return StrategyResult(name, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, error=str(exc))

    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)

    closed = [t for t in bt.trades if t.get("exit_reason") is not None]
    wins   = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    win_rate = (100.0 * wins / len(closed)) if closed else 0.0
    avg_pnl  = (sum((t.get("pnl") or 0.0) for t in closed) / len(closed)) if closed else 0.0

    return StrategyResult(
        name              = name,
        annual_return_pct = annual,
        sharpe_ratio      = summary["sharpe_ratio"],
        max_drawdown_pct  = summary["max_drawdown_pct"],
        profit_factor     = summary["profit_factor"],
        total_trades      = summary["total_trades"],
        win_rate_pct      = win_rate,
        avg_pnl_per_trade = avg_pnl,
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


def _print_table(symbol: str, results: list[StrategyResult]) -> None:
    col_w   = [20, 9, 8, 9, 7, 8, 8, 10]
    headers = ["Strategy", "Annual", "Sharpe", "Max DD", "PF", "Trades", "WinRate", "Avg PnL"]

    print(f"\n── {symbol} ──")
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr_row = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"
    print(sep)
    print(hdr_row)
    print(sep)

    for r in results:
        if r.error:
            row = [r.name, "ERROR", "-", "-", "-", "-", "-", "-"]
            print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
            continue
        row = [
            r.name,
            _fmt_pct(r.annual_return_pct),
            _fmt_f(r.sharpe_ratio),
            _fmt_dd(r.max_drawdown_pct),
            _fmt_f(r.profit_factor),
            str(r.total_trades),
            f"{r.win_rate_pct:.1f}%",
            f"${r.avg_pnl_per_trade:+.0f}",
        ]
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
    print(sep)


def _classify(r: StrategyResult, baseline: StrategyResult) -> str:
    if r.error:
        return "ERROR"
    if r.profit_factor >= baseline.profit_factor + 0.05 and r.annual_return_pct >= baseline.annual_return_pct:
        return "BETTER"
    if r.profit_factor < 1.0 or r.annual_return_pct < 0:
        return "BAD"
    if r.profit_factor >= baseline.profit_factor - 0.05:
        return "MATCHES"
    return "WORSE"


def main() -> None:
    args      = _parse_args()
    symbols   = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    long_only = not args.bidir

    print(f"\n{'=' * 90}")
    print(
        f"  STRATEGY COMPARISON  —  {args.days}d  |  "
        f"{args.risk * 100:.0f}% risk  |  "
        f"{'long-only' if long_only else 'bidirectional'}  |  "
        f"{len(symbols)} symbol(s)"
    )
    print(f"{'=' * 90}")

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + 30)

    all_results: dict[str, list[StrategyResult]] = {}

    for sym in symbols:
        print(f"\nFetching {sym}…", flush=True)
        try:
            df_4h = fetch_and_cache(sym, "4h", start_dt, end_dt)
            df_1d = fetch_and_cache(sym, "1d", start_dt, end_dt)
        except Exception as exc:
            print(f"  ERROR fetching {sym}: {exc}", file=sys.stderr)
            continue

        results: list[StrategyResult] = []
        for strat in _build_strategies(long_only):
            print(f"  → {strat.name}…", flush=True)
            r = _run_strategy(strat, df_4h, df_1d, sym, args.days, args.risk)
            results.append(r)
            if r.error:
                print(f"    ERROR: {r.error}")
            else:
                print(
                    f"    PF={_fmt_f(r.profit_factor)}  "
                    f"Ann={_fmt_pct(r.annual_return_pct)}  "
                    f"DD={_fmt_dd(r.max_drawdown_pct)}  "
                    f"Trades={r.total_trades}  "
                    f"Win={r.win_rate_pct:.1f}%"
                )

        # Sort by PF descending; errors at the bottom
        results_sorted = sorted(
            results,
            key=lambda r: (r.error is not None, -r.profit_factor),
        )
        all_results[sym] = results_sorted
        _print_table(sym, results_sorted)

        # Classification vs baseline (EMA_CROSSOVER)
        baseline = next((r for r in results if r.name == "EMA_CROSSOVER"), None)
        if baseline is None or baseline.error:
            continue
        print(f"\n  Classification (vs EMA_CROSSOVER baseline):")
        for r in results_sorted:
            if r.error:
                print(f"    [ERROR  ] {r.name}  ← {r.error}")
                continue
            v = _classify(r, baseline)
            d_pf  = r.profit_factor - baseline.profit_factor
            d_ann = (r.annual_return_pct - baseline.annual_return_pct) * 100
            d_dd  = r.max_drawdown_pct - baseline.max_drawdown_pct
            print(
                f"    [{v:7}] {r.name:20}  "
                f"ΔPF={d_pf:+.2f}  ΔAnn={d_ann:+.1f}pp  ΔDD={d_dd:+.1f}pp"
            )

    # ── Cross-symbol summary ──────────────────────────────────────────────────
    print(f"\n\n{'=' * 90}")
    print("  CROSS-SYMBOL SUMMARY (best 2 strategies per symbol)")
    print(f"{'=' * 90}\n")
    for sym, results in all_results.items():
        viable = [r for r in results if not r.error and r.total_trades >= 15]
        viable_sorted = sorted(viable, key=lambda r: -r.profit_factor)[:2]
        if not viable_sorted:
            print(f"  {sym}: no viable strategy (≥15 trades).")
            continue
        line = " | ".join(
            f"{r.name} (PF={_fmt_f(r.profit_factor)}, Ann={_fmt_pct(r.annual_return_pct)})"
            for r in viable_sorted
        )
        print(f"  {sym}: {line}")
    print()


if __name__ == "__main__":
    main()
