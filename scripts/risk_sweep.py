#!/usr/bin/env python
"""Run EMA_CROSSOVER and DONCHIAN_BREAKOUT at multiple risk levels.

Answers: at the risk level where EMA gives 40% annual, what does Donchian give?

Usage:
    python scripts/risk_sweep.py
    python scripts/risk_sweep.py --symbol BTCUSDT --days 1095
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.config_presets import get_strategy_configs
from bot.constants import StrategyName
from bot.strategy.donchian_breakout import DonchianConfig, DonchianBreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy

RISK_LEVELS = [0.01, 0.02, 0.03, 0.04, 0.05]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days",   type=int, default=1095)
    return p.parse_args()


def _build_ema(long_only: bool):
    cfg = dict(get_strategy_configs("4h")[StrategyName.EMA_CROSSOVER])
    cfg["long_only"] = long_only
    return EMACrossoverStrategy(EMACrossoverConfig(**cfg))


def _run(strategy, df_4h, df_1d, symbol: str, days: int, risk: float) -> dict:
    config = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = risk,
        timeframe         = "4h",
        leverage          = 1.0,
        long_only         = False,
    )
    engine  = BacktestEngine(config, strategy=strategy)
    bt      = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)
    return {
        "annual": annual,
        "pf":     summary["profit_factor"],
        "dd":     summary["max_drawdown_pct"],
        "sharpe": summary["sharpe_ratio"],
        "trades": summary["total_trades"],
    }


def main() -> None:
    args = _parse_args()
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + 30)

    print(f"\nFetching {args.symbol}…")
    df_4h = fetch_and_cache(args.symbol, "4h", start_dt, end_dt)
    df_1d = fetch_and_cache(args.symbol, "1d", start_dt, end_dt)

    ema      = _build_ema(long_only=True)
    donchian = DonchianBreakoutStrategy(DonchianConfig(long_only=True))

    print(f"\n{'=' * 90}")
    print(f"  RISK SWEEP — {args.symbol}, {args.days}d, 4h, long-only")
    print(f"{'=' * 90}\n")

    headers = ["Risk %", "Strategy", "Annual", "PF", "Sharpe", "Max DD", "Trades"]
    col_w   = [8, 20, 9, 7, 8, 9, 8]
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr_row = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"
    print(sep)
    print(hdr_row)
    print(sep)

    summary_lines: list[str] = []

    for risk in RISK_LEVELS:
        ema_r  = _run(ema,      df_4h, df_1d, args.symbol, args.days, risk)
        don_r  = _run(donchian, df_4h, df_1d, args.symbol, args.days, risk)

        for name, r in [("EMA_CROSSOVER", ema_r), ("DONCHIAN_BREAKOUT", don_r)]:
            row = [
                f"{risk*100:.0f}%",
                name,
                f"{r['annual']*100:+.1f}%",
                f"{r['pf']:.2f}",
                f"{r['sharpe']:.2f}",
                f"-{abs(r['dd']):.1f}%",
                str(r['trades']),
            ]
            print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
        print(sep)

        summary_lines.append(
            f"  Risk {risk*100:.0f}%:  EMA Ann={ema_r['annual']*100:+.1f}% (PF={ema_r['pf']:.2f}, DD=-{ema_r['dd']:.1f}%)  "
            f"|  DON Ann={don_r['annual']*100:+.1f}% (PF={don_r['pf']:.2f}, DD=-{don_r['dd']:.1f}%)  "
            f"|  Δ={(don_r['annual']-ema_r['annual'])*100:+.1f}pp"
        )

    print(f"\n{'=' * 90}")
    print(f"  SUMMARY (Donchian Δ vs EMA at same risk)")
    print(f"{'=' * 90}")
    for line in summary_lines:
        print(line)
    print()


if __name__ == "__main__":
    main()
