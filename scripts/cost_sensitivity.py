#!/usr/bin/env python
"""Cost-sensitivity sweep: how much does fee assumption change the backtest result?

Backtest BTC 3y at multiple cost_per_side_pct levels:
    0.05%  — Binance VIP-1+ maker
    0.075% — Binance VIP-0 maker (default Binance fee)
    0.10%  — VIP-0 taker
    0.125% — pessimistic with slippage
    0.15%  — current backtest assumption (likely over-estimate)
    0.20%  — extreme pessimistic

Usage:
    python scripts/cost_sensitivity.py
    python scripts/cost_sensitivity.py --risk 0.04
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

COST_LEVELS = [0.0005, 0.00075, 0.0010, 0.00125, 0.0015, 0.0020]


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days",   type=int,   default=1095)
    p.add_argument("--risk",   type=float, default=0.04)
    return p.parse_args()


def _run(cost: float, df_4h, df_1d, symbol: str, days: int, risk: float):
    cfg = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = risk,
        timeframe         = "4h",
        cost_per_side_pct = cost,
        leverage          = 1.0,
        long_only         = True,
    )
    engine  = BacktestEngine(cfg)
    bt      = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)
    closed  = [t for t in bt.trades if t.get("exit_reason") is not None]
    wins    = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    win_rate = (100.0 * wins / len(closed)) if closed else 0.0
    return {
        "annual":   annual,
        "pf":       summary["profit_factor"],
        "dd":       summary["max_drawdown_pct"],
        "sharpe":   summary["sharpe_ratio"],
        "trades":   summary["total_trades"],
        "win_rate": win_rate,
    }


def main():
    args = _parse_args()
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + 30)

    print(f"\nFetching {args.symbol}…")
    df_4h = fetch_and_cache(args.symbol, "4h", start_dt, end_dt)
    df_1d = fetch_and_cache(args.symbol, "1d", start_dt, end_dt)

    print(f"\n{'=' * 90}")
    print(f"  COST SENSITIVITY  —  {args.symbol}, {args.days}d, 4h, long-only, risk={args.risk*100:.0f}%, EMA")
    print(f"{'=' * 90}\n")

    headers = ["Cost/side", "Round trip", "Annual", "PF", "Sharpe", "Max DD", "Trades", "Win%"]
    col_w   = [10, 12, 9, 7, 8, 9, 8, 7]
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr     = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"
    print(sep); print(hdr); print(sep)

    baseline_cost = 0.0015
    baseline = None
    for cost in COST_LEVELS:
        r = _run(cost, df_4h, df_1d, args.symbol, args.days, args.risk)
        if abs(cost - baseline_cost) < 1e-9:
            baseline = r
        row = [
            f"{cost*100:.3f}%",
            f"{cost*200:.3f}%",
            f"{r['annual']*100:+.1f}%",
            f"{r['pf']:.2f}",
            f"{r['sharpe']:.2f}",
            f"-{abs(r['dd']):.1f}%",
            str(r['trades']),
            f"{r['win_rate']:.1f}%",
        ]
        marker = "  ← current backtest assumption" if abs(cost - baseline_cost) < 1e-9 else ""
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|" + marker)
    print(sep)

    print(f"\n  Reference fee tiers (Binance Spot):")
    print(f"    VIP-0:  0.075% maker / 0.075% taker  (round trip ~0.15% if both taker)")
    print(f"    VIP-0 with BNB:  0.0563% / 0.0563%   (-25% discount paying with BNB)")
    print(f"    VIP-1:  0.072% / 0.072%              (>$1M 30d volume)")
    print(f"\n  Realistic round-trip cost for retail: 0.10–0.15%")
    print(f"  Current backtest (0.30% round trip) is LIKELY pessimistic by 0.05–0.20pp per trade.\n")


if __name__ == "__main__":
    main()
