#!/usr/bin/env python
"""Multi-period robustness validation for BTC+ETH portfolio at 4% risk.

A strategy is ROBUST only if it's profitable in most distinct market regimes,
not just on a single continuous backtest. This splits the 3-year window into
6 sub-periods of ~6 months and runs the portfolio in each independently.

Each sub-period covers a distinct macro regime:
    P1: 2022 H1   (Luna collapse, bear market start)
    P2: 2022 H2   (FTX collapse, deep bear)
    P3: 2023 H1   (recovery start, banking crisis)
    P4: 2023 H2   (continued recovery)
    P5: 2024 H1   (BTC rally, ETF approval, halving)
    P6: 2024 H2   (Trump election rally, consolidation)
    (P7: 2025 H1 if data available — most recent period)

Verdict criterion: the portfolio should be profitable (annual > 0) in at
least 5 of the 7 periods. If profitable in fewer, the strategy is fragile.

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_multi_period.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.scenario_runner import compute_annual_return


@dataclass
class Period:
    label: str
    start: str   # ISO date string
    end:   str
    note:  str   # macro context


PERIODS: list[Period] = [
    Period("2022 H1", "2022-05-01", "2022-10-31", "Luna crash, early bear"),
    Period("2022 H2", "2022-11-01", "2023-04-30", "FTX collapse, deep bear"),
    Period("2023 H1", "2023-05-01", "2023-10-31", "SVB crisis, recovery"),
    Period("2023 H2", "2023-11-01", "2024-04-30", "Recovery accelerates"),
    Period("2024 H1", "2024-05-01", "2024-10-31", "ETF approval, halving"),
    Period("2024 H2", "2024-11-01", "2025-04-30", "Trump election rally"),
    Period("2025 H1", "2025-05-01", "2026-04-30", "Most recent, ongoing"),
]


@dataclass
class Result:
    period:  Period
    annual:  float    # annualized for the period
    pnl:     float    # absolute PnL %
    dd:      float
    pf:      float
    trades:  int
    win_rate: float
    final_capital: float


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = pd.Timestamp(start, tz="UTC")
    e = pd.Timestamp(end,   tz="UTC")
    df = df.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    mask = (df["open_time"] >= s) & (df["open_time"] <= e)
    return df[mask].reset_index(drop=True)


def _run_period(period: Period, dfs: dict, dfs_bias: dict, risk: float) -> Result | None:
    p_dfs      = {sym: _slice(df, period.start, period.end) for sym, df in dfs.items()}
    p_dfs_bias = {sym: _slice(df, period.start, period.end) for sym, df in dfs_bias.items()}

    # Skip period if any symbol has insufficient data
    for sym, d in p_dfs.items():
        if len(d) < 200:   # need at least ~33 days at 4h
            print(f"  [{period.label}] SKIP — {sym} has only {len(d)} bars")
            return None

    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=risk, timeframe="4h", long_only=True,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=p_dfs, dfs_4h=p_dfs_bias)

    days = (pd.Timestamp(period.end, tz="UTC") - pd.Timestamp(period.start, tz="UTC")).days
    annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    pnl_pct = (pr.final_capital - pr.initial_capital) / pr.initial_capital * 100

    ps = pr.portfolio_summary
    total_trades = sum(len(ts) for ts in pr.per_symbol_trades.values())

    # Compute win rate from all trades
    all_closed = []
    for ts in pr.per_symbol_trades.values():
        all_closed.extend([t for t in ts if t.get("exit_reason") is not None])
    wins = sum(1 for t in all_closed if (t.get("pnl") or 0) > 0)
    win_rate = (100.0 * wins / len(all_closed)) if all_closed else 0.0

    return Result(
        period=period, annual=annual, pnl=pnl_pct,
        dd=ps.get("max_drawdown_pct", 0.0), pf=ps.get("profit_factor", 0.0),
        trades=total_trades, win_rate=win_rate, final_capital=pr.final_capital,
    )


def main() -> None:
    risk = 0.04   # user's current risk

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = datetime(2022, 4, 1, tzinfo=timezone.utc)  # cover all periods

    print(f"\n{'=' * 100}")
    print(f"  MULTI-PERIOD ROBUSTNESS — BTC+ETH portfolio @ {risk*100:.0f}% risk, 4h, long-only")
    print(f"{'=' * 100}\n")

    print("Fetching data (covering all 7 periods)…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)

    dfs      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    print(f"\n{'Period':<12} {'Note':<32} {'Trades':>7} {'WR':>6} {'PnL':>9} {'Annual':>9} {'DD':>9} {'PF':>5}")
    print("─" * 100)

    results: list[Result] = []
    for p in PERIODS:
        r = _run_period(p, dfs, dfs_bias, risk)
        if r is None:
            continue
        results.append(r)
        ann_str = f"{r.annual*100:+8.1f}%" if r.trades > 0 else "      —  "
        pf_str  = f"{r.pf:5.2f}"   if r.pf != float('inf') and r.trades > 0 else "  —  "
        wr_str  = f"{r.win_rate:5.1f}%" if r.trades > 0 else "    — "
        dd_str  = f"-{abs(r.dd):6.1f}%" if r.dd > 0 else "  -0.0%"
        print(
            f"{r.period.label:<12} {r.period.note:<32} {r.trades:>7} {wr_str} "
            f"{r.pnl:>+8.1f}% {ann_str} {dd_str} {pf_str}"
        )

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("VERDICT")
    print(f"{'=' * 100}")

    profitable = [r for r in results if r.pnl > 0]
    losing     = [r for r in results if r.pnl <= 0]

    print(f"  Periods tested:      {len(results)}")
    print(f"  Profitable periods:  {len(profitable)}/{len(results)}")
    print(f"  Losing periods:      {len(losing)}/{len(results)}")

    if losing:
        print(f"\n  Losing periods detail:")
        for r in losing:
            print(f"    {r.period.label}  {r.period.note}  →  PnL {r.pnl:+.1f}%, DD -{r.dd:.1f}%")

    pos_periods = len(profitable)
    total       = len(results)
    if pos_periods >= total - 1:
        verdict = "ROBUST — profitable in all/almost-all distinct market regimes"
    elif pos_periods >= total * 0.7:
        verdict = "ACCEPTABLE — profitable in most regimes, fragile in one or two"
    elif pos_periods >= total * 0.5:
        verdict = "MARGINAL — barely profitable across regimes, susceptible to bad cycles"
    else:
        verdict = "FRAGILE — fails in most distinct regimes, do not rely on it"

    print(f"\n  Verdict: {verdict}")

    # Aggregate stats
    total_pnl = sum(r.pnl for r in results)
    avg_dd    = sum(r.dd for r in results) / len(results) if results else 0
    print(f"\n  Sum of period PnL:     {total_pnl:+.1f}%   (sequential, not compounded)")
    print(f"  Average period DD:     -{avg_dd:.1f}%")
    worst = min(results, key=lambda r: r.pnl) if results else None
    if worst:
        print(f"  Worst period:          {worst.period.label} ({worst.period.note}) — PnL {worst.pnl:+.1f}%, DD -{worst.dd:.1f}%")
    print()


if __name__ == "__main__":
    main()
