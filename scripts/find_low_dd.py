#!/usr/bin/env python
"""Search for parameter combinations that reduce drawdown on BTC+ETH portfolio.

Tests 12 hand-picked configurations across the levers known to affect DD:
    - risk_per_trade (4% / 3% / 2%)
    - ema_stop_mult  (tighter SL kills losers earlier)
    - ema_tp_mult    (closer TP locks profit earlier)

Each config is evaluated by Calmar (annual / DD), the standard risk-adjusted
return metric. Higher Calmar = better DD-adjusted performance.

The output sorts configs by DD ascending so you can pick a Pareto-optimal
trade-off between absolute return and drawdown tolerance.

Run:
    PYTHONPATH=. venv/bin/python scripts/find_low_dd.py
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


@dataclass
class Config:
    label: str
    risk:  float
    sl:    float
    tp:    float
    note:  str = ""


CONFIGS: list[Config] = [
    # ── Baseline ──────────────────────────────────────────────────────────────
    Config("BASELINE",         0.04, 1.5,  4.5, "current production target"),
    # ── Lower risk only ───────────────────────────────────────────────────────
    Config("Risk 3%",          0.03, 1.5,  4.5, "linear reduction"),
    Config("Risk 2%",          0.02, 1.5,  4.5, "half risk"),
    # ── Tighter SL only ───────────────────────────────────────────────────────
    Config("SL 1.0 (tight)",   0.04, 1.0,  4.5, "kill losers fast"),
    Config("SL 1.25",          0.04, 1.25, 4.5, "moderate tighten"),
    # ── Closer TP only ────────────────────────────────────────────────────────
    Config("TP 3.5 (close)",   0.04, 1.5,  3.5, "lock profit early"),
    Config("TP 4.0",           0.04, 1.5,  4.0, "moderate"),
    # ── Combined moderate (hypothesis: best Calmar) ───────────────────────────
    Config("SL1.25 TP4.0 R3%", 0.03, 1.25, 4.0, "balanced"),
    Config("SL1.25 TP4.5 R3%", 0.03, 1.25, 4.5, "balanced+winners"),
    Config("SL1.0 TP3.5 R4%",  0.04, 1.0,  3.5, "strict tight"),
    # ── Combined conservative ─────────────────────────────────────────────────
    Config("SL1.0 TP4.5 R2%",  0.02, 1.0,  4.5, "very conservative"),
    Config("SL1.25 TP4.0 R2%", 0.02, 1.25, 4.0, "moderate conservative"),
]


@dataclass
class Result:
    config:     Config
    annual:     float
    dd:         float
    pf:         float
    trades:     int
    win_rate:   float

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _run(c: Config, dfs, dfs_bias, days: int) -> Result:
    cfg = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = c.risk,
        timeframe         = "4h",
        long_only         = True,
        ema_stop_mult     = c.sl,
        ema_tp_mult       = c.tp,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    ps     = pr.portfolio_summary
    total  = sum(len(ts) for ts in pr.per_symbol_trades.values())

    all_closed = []
    for ts in pr.per_symbol_trades.values():
        all_closed.extend([t for t in ts if t.get("exit_reason") is not None])
    wins = sum(1 for t in all_closed if (t.get("pnl") or 0) > 0)
    wr = (100.0 * wins / len(all_closed)) if all_closed else 0.0

    return Result(
        config=c, annual=annual,
        dd=ps.get("max_drawdown_pct", 0.0),
        pf=ps.get("profit_factor", 0.0),
        trades=total, win_rate=wr,
    )


def main() -> None:
    days     = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  LOW-DD SEARCH — BTC+ETH portfolio, 3y, 4h, long-only")
    print(f"{'=' * 100}\n")

    print("Fetching data…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)
    dfs      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    print(f"\nRunning {len(CONFIGS)} configurations…\n")

    results: list[Result] = []
    for i, c in enumerate(CONFIGS, 1):
        print(f"  [{i}/{len(CONFIGS)}] {c.label}…", flush=True)
        r = _run(c, dfs, dfs_bias, days)
        results.append(r)

    baseline = next(r for r in results if r.config.label == "BASELINE")

    # ── Sort by DD ascending ──────────────────────────────────────────────────
    print(f"\n{'─' * 100}")
    print(f"RESULTS — sorted by lowest DD")
    print(f"{'─' * 100}")
    print(
        f"{'Configuration':<22} {'Risk':>5} {'SL':>5} {'TP':>5} "
        f"{'Annual':>9} {'DD':>9} {'PF':>5} {'WR':>6} {'Calmar':>7} {'Δ vs base':>15}"
    )
    print("─" * 100)
    for r in sorted(results, key=lambda x: x.dd):
        d_ann = (r.annual - baseline.annual) * 100
        d_dd  = r.dd - baseline.dd
        marker = "  ← BASELINE" if r is baseline else ""
        print(
            f"{r.config.label:<22} {r.config.risk*100:>4.0f}% {r.config.sl:>5.2f} {r.config.tp:>5.2f} "
            f"{r.annual*100:>+8.1f}% -{abs(r.dd):>6.1f}% {r.pf:>5.2f} {r.win_rate:>5.1f}% "
            f"{r.calmar:>7.2f}  ann{d_ann:>+5.1f}pp dd{d_dd:>+5.1f}pp{marker}"
        )

    # ── Sort by Calmar descending ─────────────────────────────────────────────
    print(f"\n{'─' * 100}")
    print(f"RESULTS — sorted by Calmar (best risk-adjusted)")
    print(f"{'─' * 100}")
    for r in sorted(results, key=lambda x: -x.calmar):
        marker = "  ← BASELINE" if r is baseline else ""
        print(
            f"  {r.calmar:>5.2f}  Calmar  |  "
            f"{r.config.label:<22}  Ann={r.annual*100:>+5.1f}%  DD=-{r.dd:>4.1f}%  "
            f"PF={r.pf:.2f}  WR={r.win_rate:>4.1f}%  trades={r.trades}{marker}"
        )

    # ── Pareto frontier ───────────────────────────────────────────────────────
    print(f"\n{'─' * 100}")
    print(f"PARETO FRONTIER (configs not strictly dominated on (annual, DD))")
    print(f"{'─' * 100}")
    pareto = []
    for r in results:
        dominated = False
        for other in results:
            if other is r:
                continue
            # other dominates r if it has better annual AND better (lower) DD
            if other.annual >= r.annual and other.dd <= r.dd and (other.annual > r.annual or other.dd < r.dd):
                dominated = True
                break
        if not dominated:
            pareto.append(r)

    pareto.sort(key=lambda x: x.dd)
    for r in pareto:
        print(
            f"  {r.config.label:<22}  Ann={r.annual*100:>+5.1f}%  DD=-{r.dd:>4.1f}%  "
            f"Calmar={r.calmar:.2f}  {r.config.note}"
        )

    # ── Recommendations ───────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 100}\n")

    best_calmar = max(results, key=lambda r: r.calmar)
    lowest_dd = min(results, key=lambda r: r.dd)
    best_for_high_return = max(
        (r for r in results if r.dd <= baseline.dd + 2),
        key=lambda r: r.annual,
        default=None,
    )

    print(f"  Best Calmar (risk-adjusted return):")
    print(f"    {best_calmar.config.label}  →  Ann={best_calmar.annual*100:+.1f}%  "
          f"DD=-{best_calmar.dd:.1f}%  Calmar={best_calmar.calmar:.2f}")

    print(f"\n  Lowest DD config:")
    print(f"    {lowest_dd.config.label}  →  Ann={lowest_dd.annual*100:+.1f}%  "
          f"DD=-{lowest_dd.dd:.1f}%  Calmar={lowest_dd.calmar:.2f}")

    if best_for_high_return:
        print(f"\n  Best annual within baseline's DD (+2pp tolerance):")
        print(f"    {best_for_high_return.config.label}  →  Ann={best_for_high_return.annual*100:+.1f}%  "
              f"DD=-{best_for_high_return.dd:.1f}%  Calmar={best_for_high_return.calmar:.2f}")

    print()


if __name__ == "__main__":
    main()
