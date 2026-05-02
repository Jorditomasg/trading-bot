#!/usr/bin/env python
"""3-symbol portfolio validation: does adding SOL help or hurt?

User has BTC + ETH + SOL active on testnet. SOL was DISCARD in standalone scan
(PF=1.08, Ann=+4.6%). Question: does it still add diversification value, or
is it dragging the portfolio down?

Configurations tested at multiple risk levels:
    BTC alone               (single-symbol baseline)
    BTC + ETH               (2-symbol portfolio)
    BTC + ETH + SOL         (3-symbol portfolio — current testnet config)

Decision rule per risk level:
    - 3-symbol BEATS 2-symbol on Calmar (annual/DD) → KEEP SOL
    - 3-symbol HURTS Calmar by >5% → DROP SOL
    - In between → MARGINAL, user choice

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_multi_asset_3sym.py
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
    label:    str
    risk:     float
    annual:   float
    dd:       float
    pf:       float
    trades:   int
    notes:    str = ""

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _run_single(symbol: str, df_primary, df_bias, risk: float, days: int, label: str) -> Result:
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=risk, timeframe="4h", long_only=True,
    )
    e  = BacktestEngine(cfg)
    bt = e.run(df=df_primary, df_4h=df_bias, symbol=symbol)
    s  = e.summary(bt)
    return Result(
        label  = label, risk = risk,
        annual = compute_annual_return(bt.initial_capital, bt.final_capital, days),
        dd     = s["max_drawdown_pct"],
        pf     = s["profit_factor"],
        trades = s["total_trades"],
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
    notes  = " · ".join(
        f"{sym}: {len(ts)}"
        for sym, ts in sorted(pr.per_symbol_trades.items())
    )
    return Result(
        label  = label, risk = risk,
        annual = annual,
        dd     = ps.get("max_drawdown_pct", 0.0),
        pf     = ps.get("profit_factor", 0.0),
        trades = total_trades,
        notes  = notes,
    )


def main() -> None:
    days     = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  3-SYMBOL PORTFOLIO VALIDATION  —  BTC vs BTC+ETH vs BTC+ETH+SOL")
    print(f"  {days}d, 4h, long-only, 0.10% cost/side")
    print(f"{'=' * 100}\n")

    print("Fetching BTC, ETH, SOL data…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)
    df_sol_4h = fetch_and_cache("SOLUSDT", "4h", start_dt, end_dt)
    df_sol_1d = fetch_and_cache("SOLUSDT", "1d", start_dt, end_dt)
    print(f"  BTC: {len(df_btc_4h)} · ETH: {len(df_eth_4h)} · SOL: {len(df_sol_4h)} bars\n")

    risks = [0.02, 0.03, 0.04]

    print(f"{'─' * 100}")
    print(f"{'Configuration':<35} {'Risk':>6} {'Annual':>10} {'DD':>9} {'PF':>6} {'Trades':>8} {'Calmar':>8}")
    print(f"{'─' * 100}")

    all_results: dict[float, dict[str, Result]] = {}

    for risk in risks:
        a = _run_single("BTCUSDT", df_btc_4h, df_btc_1d, risk, days, "BTC alone")

        dfs_2      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
        dfs_2_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}
        d = _run_portfolio(dfs_2, dfs_2_bias, risk, days, "BTC+ETH")

        dfs_3      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h, "SOLUSDT": df_sol_4h}
        dfs_3_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d, "SOLUSDT": df_sol_1d}
        t = _run_portfolio(dfs_3, dfs_3_bias, risk, days, "BTC+ETH+SOL (testnet config)")

        all_results[risk] = {"single": a, "two": d, "three": t}

        for r in [a, d, t]:
            print(
                f"{r.label:<35} {r.risk*100:>5.0f}% {r.annual*100:>+9.1f}% "
                f"-{abs(r.dd):>6.1f}% {r.pf:>6.2f} {r.trades:>8}  {r.calmar:>7.2f}"
            )
            if r.notes:
                print(f"      → {r.notes}")
        print()

    # ── Verdict per risk level ────────────────────────────────────────────────
    print(f"{'=' * 100}")
    print("VERDICT (SOL contribution to the portfolio)")
    print(f"{'=' * 100}\n")

    for risk, results in all_results.items():
        two   = results["two"]
        three = results["three"]
        d_calmar = three.calmar - two.calmar
        d_annual = (three.annual - two.annual) * 100
        d_dd     = three.dd - two.dd

        if three.calmar > two.calmar * 1.02:
            verdict = "KEEP SOL — improves Calmar"
        elif three.calmar < two.calmar * 0.95:
            verdict = "DROP SOL — degrades Calmar"
        else:
            verdict = "MARGINAL — SOL is roughly neutral"

        print(f"  Risk {risk*100:.0f}%:  ΔCalmar={d_calmar:+.2f}  ΔAnn={d_annual:+.1f}pp  ΔDD={d_dd:+.1f}pp  →  {verdict}")

    # ── Bottom-line recommendation ────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("BOTTOM-LINE: improvement vs current production (BTC alone)")
    print(f"{'=' * 100}\n")

    print(f"{'Setup':<30} {'Risk':>6} {'Annual':>9} {'DD':>9} {'Calmar':>8} {'Δ vs BTC alone'}")
    for risk, results in all_results.items():
        a, t = results["single"], results["three"]
        d_ann = (t.annual - a.annual) * 100
        d_dd  = t.dd - a.dd
        d_cal = t.calmar - a.calmar
        print(
            f"{'BTC alone':<30} {risk*100:>5.0f}% {a.annual*100:>+8.1f}% -{abs(a.dd):>6.1f}% {a.calmar:>7.2f}  baseline"
        )
        print(
            f"{'BTC+ETH+SOL (your testnet)':<30} {risk*100:>5.0f}% {t.annual*100:>+8.1f}% -{abs(t.dd):>6.1f}% {t.calmar:>7.2f}  "
            f"Δann={d_ann:+.1f}pp · Δdd={d_dd:+.1f}pp · Δcalmar={d_cal:+.2f}"
        )
        print()


if __name__ == "__main__":
    main()
