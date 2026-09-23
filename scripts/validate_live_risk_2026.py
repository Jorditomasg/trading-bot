#!/usr/bin/env python
"""Is the live risk_per_trade=0.025 still defensible on 2026 data?

Context (2026-07-30 audit): the `bot_config` KV table has
`rt_risk_per_trade = 0.025`, but the validated baseline in CLAUDE.md is 0.015
("the seeded 1.5% trades return for survivability"). The Risk×DD table that
justified higher risk predates the gotcha #40 fix, so its absolute numbers are
~N× inflated for multi-symbol runs and cannot be used to defend 2.5%.

This re-runs the comparison on the live symbol set (BTC+ETH+SOL, 4h) under the
TRUE ÷N capital allocation, over both a 3-year window and the recent regime
where the live bot actually lost money.

Run:
    PYTHONPATH=. python scripts/validate_live_risk_2026.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.scenario_runner import compute_annual_return

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
RISKS = [0.010, 0.015, 0.020, 0.025]
CAPITAL = 10_000.0


@dataclass
class Result:
    window: str
    risk: float
    annual: float
    dd: float
    pf: float
    trades: int
    win_rate: float

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _load(symbols, start, end):
    data = {}
    for sym in symbols:
        primary = fetch_and_cache(sym, "4h", start, end)
        bias = fetch_and_cache(sym, "1d", start, end)
        weekly = fetch_and_cache(sym, "1w", start, end)
        if primary.empty:
            raise SystemExit(f"No cached 4h data for {sym}")
        data[sym] = (primary, bias, weekly)
    return data


def _run(data, risk, window, days) -> Result:
    cfg = BacktestConfig(
        initial_capital=CAPITAL,
        risk_per_trade=risk,
        timeframe="4h",
        long_only=True,
    )
    engine = PortfolioBacktestEngine(config=cfg)
    pr = engine.run_portfolio(
        dfs={s: d[0] for s, d in data.items()},
        dfs_4h={s: d[1] for s, d in data.items()},
        dfs_weekly={s: d[2] for s, d in data.items()},
    )
    ps = pr.portfolio_summary
    trades = [t for ts in pr.per_symbol_trades.values() for t in ts]
    wins = [t for t in trades if (t.get("pnl") or 0) > 0]
    return Result(
        window=window,
        risk=risk,
        annual=compute_annual_return(pr.initial_capital, pr.final_capital, days),
        dd=ps.get("max_drawdown_pct", 0.0),
        pf=ps.get("profit_factor", 0.0),
        trades=len(trades),
        win_rate=(len(wins) / len(trades) * 100.0) if trades else 0.0,
    )


def main() -> None:
    now = datetime.now(timezone.utc)
    windows = {
        "3y": now - timedelta(days=3 * 365),
        "12m": now - timedelta(days=365),
    }

    rows: list[Result] = []
    for label, start in windows.items():
        print(f"\nLoading {label} ({start.date()} → {now.date()})…")
        data = _load(SYMBOLS, start, now)
        days = (now - start).days
        for risk in RISKS:
            r = _run(data, risk, label, days)
            rows.append(r)
            print(
                f"  risk={risk:.3f}  ann={r.annual * 100:+6.1f}%  "
                f"dd={r.dd:5.1f}%  pf={r.pf:.2f}  calmar={r.calmar:.2f}  "
                f"n={r.trades}  wr={r.win_rate:.0f}%"
            )

    print("\n" + "=" * 78)
    print(f"{'window':>7} {'risk':>7} {'annual':>9} {'maxDD':>8} {'PF':>6} "
          f"{'Calmar':>8} {'trades':>7} {'win%':>6}")
    print("-" * 78)
    for r in rows:
        print(f"{r.window:>7} {r.risk:>7.3f} {r.annual * 100:>8.1f}% {r.dd:>7.1f}% "
              f"{r.pf:>6.2f} {r.calmar:>8.2f} {r.trades:>7} {r.win_rate:>5.0f}%")
    print("=" * 78)

    for label in windows:
        sub = [r for r in rows if r.window == label]
        if not sub:
            continue
        live = next((r for r in sub if r.risk == 0.025), None)
        base = next((r for r in sub if r.risk == 0.015), None)
        if live and base:
            print(
                f"\n[{label}] live 2.5% vs validated 1.5%: "
                f"Calmar {live.calmar:.2f} vs {base.calmar:.2f}, "
                f"maxDD {live.dd:.1f}% vs {base.dd:.1f}%"
            )


if __name__ == "__main__":
    main()
