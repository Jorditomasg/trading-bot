#!/usr/bin/env python
"""Validate multi-asset BTC+ETH backtest vs BTC-alone baseline.

Strategy: split risk per symbol so combined max exposure equals current single-symbol risk.

Configurations tested:
    A. BTC alone @ 4% risk             (current production baseline)
    B. BTC alone @ 2% risk             (sanity check, half-risk)
    C. ETH alone @ 4% risk             (reference)
    D. BTC+ETH @ 2% per symbol         (portfolio: max combined exposure = 4%)
    E. BTC+ETH @ 4% per symbol         (aggressive: max combined = 8%)

Verdict criterion for D vs A: D must beat A on Sharpe AND keep DD within +3pp.
Annual return need not strictly exceed A — equal annual with lower DD is a win.

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_multi_asset.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.scenario_runner import compute_annual_return


@dataclass
class Result:
    name: str
    annual: float
    sharpe: float
    dd:     float
    pf:     float
    trades: int
    final:  float
    notes:  str = ""


def _run_single(symbol: str, df_primary, df_bias, risk: float, days: int, label: str) -> Result:
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=risk, timeframe="4h", long_only=True,
    )
    e  = BacktestEngine(cfg)
    bt = e.run(df=df_primary, df_4h=df_bias, symbol=symbol)
    s  = e.summary(bt)
    return Result(
        name   = label,
        annual = compute_annual_return(bt.initial_capital, bt.final_capital, days),
        sharpe = s["sharpe_ratio"],
        dd     = s["max_drawdown_pct"],
        pf     = s["profit_factor"],
        trades = s["total_trades"],
        final  = bt.final_capital,
    )


def _run_portfolio(dfs: dict, dfs_bias: dict, risk: float, days: int, label: str) -> Result:
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=risk, timeframe="4h", long_only=True,
    )
    e  = PortfolioBacktestEngine(cfg)
    pr = e.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    ps     = pr.portfolio_summary
    total_trades = sum(len(ts) for ts in pr.per_symbol_trades.values())

    return Result(
        name   = label,
        annual = annual,
        sharpe = ps.get("sharpe_ratio", 0.0),
        dd     = ps.get("max_drawdown_pct", 0.0),
        pf     = ps.get("profit_factor", 0.0),
        trades = total_trades,
        final  = pr.final_capital,
        notes  = " · ".join(
            f"{sym}: {len(ts)} trades"
            for sym, ts in sorted(pr.per_symbol_trades.items())
        ),
    )


def _print(r: Result, baseline: Result | None = None) -> None:
    delta = ""
    if baseline is not None and r is not baseline:
        d_ann = (r.annual - baseline.annual) * 100
        d_dd  = r.dd - baseline.dd
        d_sh  = r.sharpe - baseline.sharpe
        delta = f"  [Δann={d_ann:+.1f}pp · Δdd={d_dd:+.1f}pp · Δsharpe={d_sh:+.2f}]"
    print(
        f"  {r.name:30}  Ann={r.annual*100:+6.1f}%  Sharpe={r.sharpe:5.2f}  "
        f"DD=-{abs(r.dd):4.1f}%  PF={r.pf:.2f}  Trades={r.trades:3}{delta}"
    )
    if r.notes:
        print(f"      {r.notes}")


def main() -> None:
    days     = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  MULTI-ASSET VALIDATION  —  {days}d, 4h, long-only")
    print(f"{'=' * 100}\n")

    print("Fetching BTC and ETH data…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)
    print(f"  BTC 4h: {len(df_btc_4h):,} bars  ·  ETH 4h: {len(df_eth_4h):,} bars\n")

    print("Running configs…\n")
    a = _run_single("BTCUSDT", df_btc_4h, df_btc_1d, 0.04, days, "A. BTC alone @ 4%")
    b = _run_single("BTCUSDT", df_btc_4h, df_btc_1d, 0.02, days, "B. BTC alone @ 2%")
    c = _run_single("ETHUSDT", df_eth_4h, df_eth_1d, 0.04, days, "C. ETH alone @ 4%")

    dfs       = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias  = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    d = _run_portfolio(dfs, dfs_bias, 0.02, days, "D. BTC+ETH @ 2% each")
    e = _run_portfolio(dfs, dfs_bias, 0.04, days, "E. BTC+ETH @ 4% each (aggressive)")

    print("=" * 100)
    print("RESULTS:")
    print("=" * 100)
    _print(a)
    _print(b)
    _print(c)
    print()
    _print(d, baseline=a)
    _print(e, baseline=a)

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("VERDICT")
    print(f"{'=' * 100}")

    def verdict(label: str, candidate: Result, baseline: Result, max_dd_increase: float = 3.0) -> str:
        d_ann = (candidate.annual - baseline.annual) * 100
        d_sh  = candidate.sharpe - baseline.sharpe
        d_dd  = candidate.dd - baseline.dd
        if candidate.sharpe > baseline.sharpe and d_dd < max_dd_increase:
            return f"GO   — {label} improves Sharpe ({d_sh:+.2f}) within DD budget"
        if d_ann < -2.0 and d_sh < 0:
            return f"NO-GO — {label} worse on annual AND Sharpe"
        if d_ann > 0 and d_dd > max_dd_increase:
            return f"MARGINAL — {label} higher returns but {d_dd:+.1f}pp DD increase"
        return f"NO-GO — {label} no clear improvement"

    print(f"  D (BTC+ETH @ 2% each)  vs  A (BTC alone @ 4%):")
    print(f"    {verdict('D', d, a)}")
    print(f"  E (BTC+ETH @ 4% each)  vs  A (BTC alone @ 4%):")
    print(f"    {verdict('E', e, a, max_dd_increase=10.0)}")
    print()


if __name__ == "__main__":
    main()
