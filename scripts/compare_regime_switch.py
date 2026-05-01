#!/usr/bin/env python
"""Validate regime-based strategy switching before changing the live bot.

Compares 4 configurations on BTC 3y at the user's risk level:
    EMA legacy    — current production behavior (TRENDING→EMA, others HOLD)
    EMA via map   — TRENDING→EMA, RANGING→EMA, VOLATILE→EMA  (sanity check)
    Donchian pure — VOLATILE only → Donchian (rest HOLD)
    SWITCH A      — TRENDING→EMA, RANGING→EMA, VOLATILE→Donchian (the candidate)

The verdict criterion: SWITCH A must beat EMA legacy on BOTH Annual return AND
Sharpe AND keep DD within +2pp. Only then is integration justified.

Usage:
    python scripts/compare_regime_switch.py
    python scripts/compare_regime_switch.py --risk 0.04 --days 1095
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)
logging.getLogger("bot.strategy").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.scenario_runner import compute_annual_return
from bot.config_presets import get_strategy_configs
from bot.constants import StrategyName
from bot.regime.detector import MarketRegime
from bot.strategy.donchian_breakout import DonchianBreakoutStrategy, DonchianConfig
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy


@dataclass
class ConfigResult:
    name: str
    annual_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float
    total_trades: int
    win_rate_pct: float
    trades_by_strategy: dict
    trades_by_regime: dict


def _build_ema(long_only: bool) -> EMACrossoverStrategy:
    cfg = dict(get_strategy_configs("4h")[StrategyName.EMA_CROSSOVER])
    cfg["long_only"] = long_only
    return EMACrossoverStrategy(EMACrossoverConfig(**cfg))


def _build_donchian(long_only: bool) -> DonchianBreakoutStrategy:
    return DonchianBreakoutStrategy(DonchianConfig(long_only=long_only))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days",   type=int,   default=1095)
    p.add_argument("--risk",   type=float, default=0.04, help="Risk per trade (default 0.04 = 4%%)")
    return p.parse_args()


def _run(name: str, engine: BacktestEngine, df_4h, df_1d, symbol: str, days: int) -> ConfigResult:
    bt      = engine.run(df=df_4h, df_4h=df_1d, symbol=symbol)
    summary = engine.summary(bt)
    annual  = compute_annual_return(bt.initial_capital, bt.final_capital, days)

    closed = [t for t in bt.trades if t.get("exit_reason") is not None]
    wins   = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    win_rate = (100.0 * wins / len(closed)) if closed else 0.0

    by_strategy = Counter(t.get("strategy", "?") for t in bt.trades)
    by_regime   = Counter(t.get("regime", "?")   for t in bt.trades)

    return ConfigResult(
        name              = name,
        annual_return_pct = annual,
        sharpe_ratio      = summary["sharpe_ratio"],
        max_drawdown_pct  = summary["max_drawdown_pct"],
        profit_factor     = summary["profit_factor"],
        total_trades      = summary["total_trades"],
        win_rate_pct      = win_rate,
        trades_by_strategy = dict(by_strategy),
        trades_by_regime   = dict(by_regime),
    )


def _print_table(results: list[ConfigResult]) -> None:
    col_w   = [22, 9, 8, 9, 7, 8, 8]
    headers = ["Configuration", "Annual", "Sharpe", "Max DD", "PF", "Trades", "WinRate"]
    sep     = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr     = "|" + "|".join(f" {h.ljust(w)} " for h, w in zip(headers, col_w)) + "|"
    print(sep); print(hdr); print(sep)
    for r in results:
        row = [
            r.name,
            f"{r.annual_return_pct*100:+.1f}%",
            f"{r.sharpe_ratio:.2f}",
            f"-{abs(r.max_drawdown_pct):.1f}%",
            f"{r.profit_factor:.2f}",
            str(r.total_trades),
            f"{r.win_rate_pct:.1f}%",
        ]
        print("|" + "|".join(f" {v.ljust(w)} " for v, w in zip(row, col_w)) + "|")
    print(sep)


def _verdict(legacy: ConfigResult, candidate: ConfigResult) -> tuple[str, list[str]]:
    """Return (decision, reasons[]). Decision: GO / NO-GO / MARGINAL."""
    reasons = []
    annual_better = candidate.annual_return_pct > legacy.annual_return_pct
    sharpe_better = candidate.sharpe_ratio      > legacy.sharpe_ratio
    pf_better     = candidate.profit_factor     > legacy.profit_factor
    dd_increase   = candidate.max_drawdown_pct  - legacy.max_drawdown_pct  # positive = worse
    trades_more   = candidate.total_trades      > legacy.total_trades

    reasons.append(f"Annual return: {'↑' if annual_better else '↓'}  "
                   f"({legacy.annual_return_pct*100:+.1f}% → {candidate.annual_return_pct*100:+.1f}%)")
    reasons.append(f"Sharpe ratio:  {'↑' if sharpe_better else '↓'}  "
                   f"({legacy.sharpe_ratio:.2f} → {candidate.sharpe_ratio:.2f})")
    reasons.append(f"Profit factor: {'↑' if pf_better else '↓'}  "
                   f"({legacy.profit_factor:.2f} → {candidate.profit_factor:.2f})")
    reasons.append(f"Max DD:        {'↑ (worse)' if dd_increase > 0 else '↓ (better)'}  "
                   f"({legacy.max_drawdown_pct:.1f}% → {candidate.max_drawdown_pct:.1f}%)")
    reasons.append(f"Trade count:   {'↑' if trades_more else '↓'}  "
                   f"({legacy.total_trades} → {candidate.total_trades})")

    if annual_better and sharpe_better and dd_increase < 2.0:
        return "GO", reasons
    if not annual_better and not sharpe_better:
        return "NO-GO", reasons
    return "MARGINAL", reasons


def main() -> None:
    args = _parse_args()
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=args.days + 30)

    print(f"\nFetching {args.symbol}…")
    df_4h = fetch_and_cache(args.symbol, "4h", start_dt, end_dt)
    df_1d = fetch_and_cache(args.symbol, "1d", start_dt, end_dt)

    print(f"\n{'=' * 90}")
    print(f"  REGIME-SWITCH VALIDATION  —  {args.symbol}, {args.days}d, 4h, long-only, risk={args.risk*100:.0f}%")
    print(f"{'=' * 90}\n")

    bt_config = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = args.risk,
        timeframe         = "4h",
        cost_per_side_pct = 0.0015,
        leverage          = 1.0,
        long_only         = True,
    )

    # ── Build configurations ──────────────────────────────────────────────────
    # 1. EMA legacy: production behavior (no regime map → only TRENDING fires)
    eng_legacy = BacktestEngine(bt_config)

    # 2. EMA via map (TRENDING + RANGING + VOLATILE all use EMA): sanity check
    ema_full_map = {
        MarketRegime.TRENDING: _build_ema(long_only=True),
        MarketRegime.RANGING:  _build_ema(long_only=True),
        MarketRegime.VOLATILE: _build_ema(long_only=True),
    }
    eng_ema_full = BacktestEngine(bt_config, strategies_by_regime=ema_full_map)

    # 3. Donchian only on VOLATILE (rest HOLD)
    donchian_only_map = {
        MarketRegime.VOLATILE: _build_donchian(long_only=True),
    }
    eng_donchian_only = BacktestEngine(bt_config, strategies_by_regime=donchian_only_map)

    # 4. SWITCH A: TRENDING+RANGING→EMA, VOLATILE→Donchian
    switch_a_map = {
        MarketRegime.TRENDING: _build_ema(long_only=True),
        MarketRegime.RANGING:  _build_ema(long_only=True),
        MarketRegime.VOLATILE: _build_donchian(long_only=True),
    }
    eng_switch_a = BacktestEngine(bt_config, strategies_by_regime=switch_a_map)

    # ── Run all ───────────────────────────────────────────────────────────────
    print("Running 4 configurations…\n")
    results = [
        _run("EMA legacy (status quo)", eng_legacy,        df_4h, df_1d, args.symbol, args.days),
        _run("EMA all regimes",         eng_ema_full,      df_4h, df_1d, args.symbol, args.days),
        _run("Donchian VOLATILE only",  eng_donchian_only, df_4h, df_1d, args.symbol, args.days),
        _run("SWITCH A (candidate)",    eng_switch_a,      df_4h, df_1d, args.symbol, args.days),
    ]

    _print_table(results)

    # ── Trade attribution (where did SWITCH A's trades come from?) ────────────
    print()
    switch_a = results[-1]
    legacy   = results[0]
    print(f"  SWITCH A trade attribution:")
    print(f"    by strategy: {switch_a.trades_by_strategy}")
    print(f"    by regime:   {switch_a.trades_by_regime}")
    print(f"  EMA legacy trade attribution:")
    print(f"    by strategy: {legacy.trades_by_strategy}")
    print(f"    by regime:   {legacy.trades_by_regime}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    decision, reasons = _verdict(legacy, switch_a)
    print(f"\n{'=' * 90}")
    print(f"  VERDICT: {decision}  (SWITCH A vs EMA legacy)")
    print(f"{'=' * 90}")
    for r in reasons:
        print(f"  • {r}")

    print()
    if decision == "GO":
        print("  ✓ Switching is justified — integrate VOLATILE→Donchian into the live bot.")
    elif decision == "NO-GO":
        print("  ✗ Switching does NOT improve outcomes — keep EMA legacy as is.")
    else:
        print("  ~ Marginal improvement — consider whether the operational complexity is worth it.")
    print()


if __name__ == "__main__":
    main()
