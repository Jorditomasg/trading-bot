#!/usr/bin/env python
"""Risk × Drawdown Scaler matrix — decide cap and scaler config empirically.

Tests N risk levels × M scaler configs = N×M portfolio backtests.

Baseline reference: BTC+ETH 4h, long-only, bias_strict=True, SL=1.5×ATR, TP=4.5×ATR
(matches find_low_dd_v2.py "current target" baseline).

Run with defaults (5 risks × 3 scalers × BTC+ETH × 3y):
    PYTHONPATH=. .venv/bin/python scripts/risk_scaler_matrix.py

Custom run:
    PYTHONPATH=. .venv/bin/python scripts/risk_scaler_matrix.py \\
        --risks 0.01 0.015 0.02 \\
        --symbols BTCUSDT \\
        --days 730 \\
        --scalers OFF Conservative
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.config_presets import bias_timeframe_for
from bot.risk.drawdown_scaler import DrawdownRiskConfig


DEFAULT_RISKS   = [0.015, 0.02, 0.025, 0.03, 0.04]
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DEFAULT_DAYS    = 1095
DEFAULT_SCALERS = ["OFF", "Conservative", "Moderate"]

SCALER_PRESETS: dict[str, DrawdownRiskConfig | None] = {
    "OFF":          None,
    "Conservative": DrawdownRiskConfig(
        enabled=True,
        thresholds=[0.05, 0.10, 0.15],
        multipliers=[0.50, 0.25, 0.10],
    ),
    "Moderate":     DrawdownRiskConfig(
        enabled=True,
        thresholds=[0.03, 0.07, 0.12],
        multipliers=[0.50, 0.25, 0.10],
    ),
    "Aggressive":   DrawdownRiskConfig(
        enabled=True,
        thresholds=[0.02, 0.05, 0.10],
        multipliers=[0.50, 0.25, 0.10],
    ),
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Risk × Drawdown Scaler matrix — N risks × M scalers × symbols",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--risks", nargs="+", type=float, default=DEFAULT_RISKS,
        help="Risk levels to test (decimal, e.g. 0.015 0.02)",
    )
    p.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="Symbols included in the portfolio backtest",
    )
    p.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help="Backtest lookback window in days",
    )
    p.add_argument(
        "--scalers", nargs="+", default=DEFAULT_SCALERS,
        choices=list(SCALER_PRESETS.keys()),
        help="Scaler preset names to compare",
    )
    return p.parse_args()


@dataclass
class Result:
    risk:       float
    scaler:     str
    annual:     float   # decimal (0.225 = 22.5%)
    dd:         float   # percent (20.5 = 20.5%)
    pf:         float
    sharpe:     float
    wr:         float   # percent
    trades:     int

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _run(
    risk: float,
    scaler_cfg: DrawdownRiskConfig | None,
    scaler_name: str,
    dfs: dict,
    dfs_bias: dict,
    days: int,
) -> Result:
    cfg = BacktestConfig(
        initial_capital = 10_000.0,
        risk_per_trade  = risk,
        timeframe       = "4h",
        long_only       = True,
        ema_stop_mult   = 1.5,
        ema_tp_mult     = 4.5,
        bias_strict     = True,
        dd_risk         = scaler_cfg,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    ps     = pr.portfolio_summary

    return Result(
        risk    = risk,
        scaler  = scaler_name,
        annual  = annual,
        dd      = ps.get("max_drawdown_pct", 0.0),
        pf      = ps.get("profit_factor", 0.0),
        sharpe  = ps.get("sharpe_ratio", 0.0),
        wr      = ps.get("win_rate_pct", 0.0),
        trades  = ps.get("total_trades", 0),
    )


def _print_table(results: list[Result], sort_key, title: str) -> None:
    print(f"\n{'─' * 100}")
    print(title)
    print("─" * 100)
    print(
        f"{'Risk':>6} {'Scaler':>13} "
        f"{'Annual':>9} {'DD':>9} {'PF':>6} {'Sharpe':>7} "
        f"{'WR':>6} {'Trades':>7} {'Calmar':>7}"
    )
    print("─" * 100)
    for r in sorted(results, key=sort_key):
        pf_str = f"{r.pf:.2f}" if r.pf != float("inf") else "  ∞"
        print(
            f"{r.risk*100:>5.1f}% {r.scaler:>13} "
            f"{r.annual*100:>+8.1f}% -{abs(r.dd):>6.1f}% "
            f"{pf_str:>6} {r.sharpe:>7.2f} "
            f"{r.wr:>5.1f}% {r.trades:>7} {r.calmar:>7.2f}"
        )


def main() -> None:
    args     = _parse_args()
    risks    = args.risks
    symbols  = args.symbols
    days     = args.days
    scalers  = {name: SCALER_PRESETS[name] for name in args.scalers}

    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  RISK × DRAWDOWN SCALER MATRIX")
    print(
        f"  {'+'.join(symbols)} portfolio, 4h, {days}d, bias_strict=True, long-only, "
        f"SL=1.5×ATR, TP=4.5×ATR"
    )
    print(f"{'=' * 100}\n")

    print("Fetching data…")
    dfs:      dict[str, object] = {}
    dfs_bias: dict[str, object] = {}
    for sym in symbols:
        bias_tf       = bias_timeframe_for("4h")  # 4h primary → 1d bias
        dfs[sym]      = fetch_and_cache(sym, "4h",   start_dt, end_dt)
        dfs_bias[sym] = fetch_and_cache(sym, bias_tf, start_dt, end_dt)

    total_runs = len(risks) * len(scalers)
    print(f"\nRunning {total_runs} configurations…\n")

    results: list[Result] = []
    i = 0
    for scaler_name, scaler_cfg in scalers.items():
        for risk in risks:
            i += 1
            print(
                f"  [{i:2d}/{total_runs}] risk={risk*100:>4.1f}% scaler={scaler_name:<13}…",
                flush=True,
            )
            results.append(_run(risk, scaler_cfg, scaler_name, dfs, dfs_bias, days))

    # Grouped: by scaler then by risk (matrix view)
    _print_table(
        results,
        sort_key=lambda r: (list(scalers.keys()).index(r.scaler), r.risk),
        title="MATRIX VIEW — grouped by scaler, then by risk",
    )

    # Sorted by Calmar (best risk-adjusted return)
    _print_table(
        results,
        sort_key=lambda r: -r.calmar,
        title="RANKED BY CALMAR (Annual / DD) — higher is better",
    )

    # Verdict — pick the highest configured risk with OFF scaler as the
    # natural reference point (matches what `find_low_dd_v2.py` calls
    # BASELINE), or fall back to the first OFF result if no high-risk
    # OFF run is in scope.
    off_results  = [r for r in results if r.scaler == "OFF"]
    baseline     = max(off_results, key=lambda r: r.risk) if off_results else None
    best         = max(results, key=lambda x: x.calmar)
    safest       = min(results, key=lambda x: x.dd)

    print(f"\n{'=' * 100}")
    print("VERDICT")
    print(f"{'=' * 100}\n")
    if baseline:
        print(
            f"  Baseline ({baseline.risk*100:.1f}% / OFF):"
            f"   Ann={baseline.annual*100:>+5.1f}%  DD=-{baseline.dd:>4.1f}%  "
            f"PF={baseline.pf:.2f}  Calmar={baseline.calmar:.2f}"
        )
    print(
        f"  Best Calmar:"
        f"          risk={best.risk*100:.1f}%  scaler={best.scaler}  →  "
        f"Ann={best.annual*100:+.1f}%  DD=-{best.dd:.1f}%  Calmar={best.calmar:.2f}"
    )
    print(
        f"  Safest (min DD):"
        f"      risk={safest.risk*100:.1f}%  scaler={safest.scaler}  →  "
        f"Ann={safest.annual*100:+.1f}%  DD=-{safest.dd:.1f}%  Calmar={safest.calmar:.2f}"
    )
    if baseline and best is not baseline:
        d_ann = (best.annual - baseline.annual) * 100
        d_dd  = best.dd - baseline.dd
        d_cal = best.calmar - baseline.calmar
        print(
            f"\n  Δ best vs baseline:"
            f"   ann{d_ann:+.1f}pp   dd{d_dd:+.1f}pp   calmar{d_cal:+.2f}"
        )
    print()


if __name__ == "__main__":
    main()
