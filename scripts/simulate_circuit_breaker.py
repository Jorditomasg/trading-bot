#!/usr/bin/env python
"""Estimate the effect of the production circuit_breaker on backtest DD.

The live bot has `RiskConfig.max_drawdown` (default 15%) — when portfolio
drawdown exceeds this threshold, new entries are paused until either:
  (a) drawdown recovers below threshold, or
  (b) `cooldown_hours` (default 4h) elapse since trigger.

The BacktestEngine does NOT simulate this. So the -39.5% DD shown in
backtests is the WORST-CASE without circuit-breaker protection.

This script does a post-hoc counterfactual: rebuilds the equity curve bar by
bar, blocks new entries when DD threshold is crossed, and recomputes the
DD that the live bot would actually experience.

Tests at multiple `max_drawdown` thresholds: 10%, 15% (production default),
20%, 25%.

Run:
    PYTHONPATH=. venv/bin/python scripts/simulate_circuit_breaker.py
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
class CBResult:
    threshold:    float
    annual:       float
    dd:           float
    pf:           float
    trades_kept:  int
    trades_blocked: int
    final:        float

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _simulate_with_cb(
    trades:          list[dict],
    initial_capital: float,
    threshold:       float,
    cooldown_hours:  int = 4,
) -> tuple[list[dict], list[dict]]:
    """Replay trades sequentially. Block entries when DD > threshold OR cooldown active.

    Returns (kept_trades, blocked_trades). Approximate: assumes trades close in order
    they were opened (no overlap problem; entries that would happen during a blocked
    period are simply skipped — their PnL is removed from the trajectory).
    """
    if not trades:
        return [], []

    # Sort trades by entry_time
    sorted_trades = sorted(trades, key=lambda t: pd.Timestamp(t["entry_time"]))

    capital  = initial_capital
    peak     = initial_capital
    breaker_triggered_at: pd.Timestamp | None = None
    kept:    list[dict] = []
    blocked: list[dict] = []

    for trade in sorted_trades:
        entry_t = pd.Timestamp(trade["entry_time"])
        if entry_t.tzinfo is None:
            entry_t = entry_t.tz_localize("UTC")

        # Update DD-state up to this entry
        drawdown = (peak - capital) / peak if peak > 0 else 0.0

        # Check if breaker should still be active
        breaker_active = False
        if breaker_triggered_at is not None:
            elapsed_h = (entry_t - breaker_triggered_at).total_seconds() / 3600
            if drawdown < threshold or elapsed_h >= cooldown_hours:
                breaker_triggered_at = None  # reset
            else:
                breaker_active = True

        # Trip breaker if DD crossed threshold and not already tripped
        if not breaker_active and drawdown >= threshold and breaker_triggered_at is None:
            breaker_triggered_at = entry_t
            breaker_active       = True

        if breaker_active:
            blocked.append(trade)
            continue

        # Trade goes through: update capital
        kept.append(trade)
        pnl = trade.get("pnl") or 0.0
        capital += pnl
        peak = max(peak, capital)

    return kept, blocked


def _recompute_metrics(
    trades:          list[dict],
    initial_capital: float,
    days:            int,
) -> tuple[float, float, float, float]:
    """Recompute (annual, dd, pf, final_capital) from trade list."""
    if not trades:
        return 0.0, 0.0, 0.0, initial_capital

    sorted_trades = sorted(trades, key=lambda t: pd.Timestamp(t["entry_time"]))
    capital = initial_capital
    peak    = initial_capital
    max_dd  = 0.0
    gross_win  = 0.0
    gross_loss = 0.0
    for t in sorted_trades:
        pnl = t.get("pnl") or 0.0
        capital += pnl
        peak    = max(peak, capital)
        dd      = (peak - capital) / peak if peak > 0 else 0.0
        max_dd  = max(max_dd, dd)
        if pnl > 0:
            gross_win  += pnl
        else:
            gross_loss += abs(pnl)

    annual = compute_annual_return(initial_capital, capital, days)
    pf     = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return annual, max_dd * 100, pf, capital


def main() -> None:
    days     = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  CIRCUIT BREAKER POST-HOC SIMULATION  —  BTC+ETH @ 4%, 3y")
    print(f"{'=' * 100}\n")

    print("Fetching data and running baseline backtest…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)
    dfs      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.04,
        timeframe="4h", long_only=True,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    all_trades = []
    for ts in pr.per_symbol_trades.values():
        all_trades.extend([t for t in ts if t.get("exit_reason") is not None])

    print(f"\nBaseline (no circuit breaker simulation):")
    print(f"  Trades: {len(all_trades)}")

    base_annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    base_dd     = pr.portfolio_summary.get("max_drawdown_pct", 0.0)
    base_pf     = pr.portfolio_summary.get("profit_factor", 0.0)
    print(f"  Annual: {base_annual*100:+.1f}%  DD: -{base_dd:.1f}%  PF: {base_pf:.2f}  Calmar: {base_annual*100/base_dd:.2f}")

    # ── Test at multiple thresholds ───────────────────────────────────────────
    print(f"\n{'─' * 100}")
    print(f"With circuit breaker (post-hoc simulation, cooldown=4h):")
    print("─" * 100)
    print(f"{'Threshold':>10} {'Annual':>9} {'DD':>9} {'PF':>5} {'Trades kept':>13} {'Blocked':>9} {'Calmar':>7}  Δ vs base")

    for threshold in [0.10, 0.12, 0.15, 0.18, 0.20, 0.25]:
        kept, blocked = _simulate_with_cb(all_trades, pr.initial_capital, threshold)
        annual, dd, pf, final = _recompute_metrics(kept, pr.initial_capital, days)
        calmar = (annual * 100 / dd) if dd > 0 else 0
        d_ann = (annual - base_annual) * 100
        d_dd  = dd - base_dd
        print(
            f"  {threshold*100:>5.0f}%   {annual*100:>+8.1f}% -{abs(dd):>6.1f}% {pf:>5.2f} {len(kept):>13} {len(blocked):>9} "
            f"{calmar:>7.2f}  ann{d_ann:>+5.1f}pp dd{d_dd:>+5.1f}pp"
        )

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("INTERPRETATION")
    print(f"{'=' * 100}\n")

    cb15_kept, cb15_blocked = _simulate_with_cb(all_trades, pr.initial_capital, 0.15)
    cb15_annual, cb15_dd, _, _ = _recompute_metrics(cb15_kept, pr.initial_capital, days)
    print(f"  Production default (max_drawdown=15%):")
    print(f"    Backtest as-is (no circuit breaker):  Ann={base_annual*100:+.1f}%  DD=-{base_dd:.1f}%")
    print(f"    With production circuit breaker:       Ann={cb15_annual*100:+.1f}%  DD=-{cb15_dd:.1f}%")
    print(f"    Trades blocked by breaker: {len(cb15_blocked)}/{len(all_trades)}")
    print()
    if cb15_dd < base_dd - 5:
        print(f"  ★ Production DD will likely be ~{cb15_dd:.0f}% (not {base_dd:.0f}%) thanks to circuit breaker.")
        print(f"    This is APPROXIMATE — real circuit breaker may behave slightly differently due to")
        print(f"    overlapping positions and intra-bar dynamics, but the order-of-magnitude is correct.")
    else:
        print(f"  Circuit breaker has limited effect — DDs are spread over many trades, not concentrated.")
    print()


if __name__ == "__main__":
    main()
