"""Final validation: BTC+ETH+SOL portfolio with multiple risk × filter combos.

Confirms the SOL-diversification finding is robust to:
  - risk_per_trade (1.5% vs 2.0%)
  - momentum filter ON vs OFF
on both 6m and 3y windows. Also reports per-symbol breakdown so we can see
which symbol drives gains.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.ERROR)
for mod in ("bot.bias.filter", "bot.strategy", "bot.orchestrator", "bot.regime"):
    logging.getLogger(mod).setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine


CAPITAL = 10_000.0
COST    = 0.001
END_DT  = datetime(2026, 5, 17, tzinfo=timezone.utc)


def run(symbols, days, risk, *, momentum=True):
    start = END_DT - timedelta(days=days)
    dfs    = {s: fetch_and_cache(s, "4h", start, END_DT) for s in symbols}
    dfs_1d = {s: fetch_and_cache(s, "1d", start, END_DT) for s in symbols}
    dfs_w  = {s: fetch_and_cache(s, "1w", start - timedelta(days=154), END_DT)
              for s in symbols} if momentum else None
    cfg = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = risk,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
        ema_stop_mult     = 1.5,
        ema_tp_mult       = 5.0,
        ema_max_distance_atr = 1.0,
        ema_volume_mult   = 1.5,
        ema_require_bar_dir  = True,
        ema_require_momentum = True,
        ema_min_atr_pct   = 0.005,
        momentum_filter_enabled = momentum,
        momentum_neutral_band   = 0.08,
    )
    engine = PortfolioBacktestEngine(cfg)
    r = engine.run_portfolio(dfs, dfs_4h=dfs_1d, dfs_weekly=dfs_w)
    s = r.portfolio_summary
    annual = (s["total_pnl_pct"] / days) * 365
    dd = abs(s["max_drawdown_pct"])
    calmar = annual/dd if dd else float("nan")
    return s, annual, dd, calmar, r.per_symbol_summary


def main():
    cohorts = [
        ("BTC+ETH",      ["BTCUSDT","ETHUSDT"]),
        ("BTC+ETH+SOL",  ["BTCUSDT","ETHUSDT","SOLUSDT"]),
    ]
    print(f"Capital ${CAPITAL:,.0f}  Cost {COST*100:.2f}%/side\n")
    for days, tag in [(180, "6 MONTHS"), (1095, "3 YEARS")]:
        print(f"=== {tag} ===")
        print(f"{'Cohort':<14s} {'Risk':>5s} {'Mom':>4s} | {'PF':>5s} {'Annual':>8s} {'Total':>9s} {'DD':>5s} {'Tr':>3s} {'WR':>6s} {'Cm':>5s}")
        print("-"*90)
        for name, syms in cohorts:
            for risk in [0.015, 0.02]:
                for mom in [True, False]:
                    s, ann, dd, cm, per = run(syms, days, risk, momentum=mom)
                    mom_tag = "ON" if mom else "OFF"
                    print(f"{name:<14s} {risk*100:>4.1f}% {mom_tag:>4s} | "
                          f"{s['profit_factor']:>5.2f} {ann:>+7.1f}% {s['total_pnl_pct']:>+8.1f}% "
                          f"{dd:>4.1f}% {s['total_trades']:>3d} {s['win_rate_pct']:>5.1f}% {cm:>5.2f}", flush=True)
        # Per-symbol breakdown for BTC+ETH+SOL at best config (2%, momentum ON)
        s, ann, dd, cm, per = run(cohorts[1][1], days, 0.02, momentum=True)
        print(f"\n  Per-symbol breakdown (BTC+ETH+SOL @ 2% risk, mom=ON, {tag}):")
        for sym in cohorts[1][1]:
            ps = per.get(sym, {})
            if ps:
                pct = ps.get("total_pnl_pct", 0)
                print(f"    {sym}: PnL ${ps.get('total_pnl',0):+,.0f} ({pct:+.1f}%) "
                      f"PF {ps.get('profit_factor',0):.2f} WR {ps.get('win_rate_pct',0):.1f}% "
                      f"trades={ps.get('total_trades',0)} DD {ps.get('max_drawdown_pct',0):.1f}%")
        print()


if __name__ == "__main__":
    main()
