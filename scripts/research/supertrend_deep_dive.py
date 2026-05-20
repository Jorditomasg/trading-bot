"""Deep-dive on Supertrend — it beat everything in the 6m quest. Now we need:

  A) Sensitivity: ATR period × multiplier grid → find robust sweet spot
  B) Robustness: same configs over 3y (not just 6m) → reject curve-fit picks
  C) With/without bias filter (4h primary → 1d bias) → measure filter help/hurt
  D) Compare to EMA crossover baseline on identical 3y window
  E) Per-symbol breakdown (BTC vs ETH)
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.ERROR)
for mod in ("bot.bias.filter", "bot.strategy", "bot.orchestrator", "bot.regime"):
    logging.getLogger(mod).setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.supertrend import SupertrendConfig, SupertrendStrategy


SYMBOLS = ["BTCUSDT", "ETHUSDT"]
RISK    = 0.02
COST    = 0.001
CAPITAL = 10_000.0
END_DT  = datetime(2026, 5, 17, tzinfo=timezone.utc)


@dataclass
class R:
    label:  str
    sym:    str
    pf:     float
    annual: float
    total:  float
    dd:     float
    tr:     int
    wr:     float
    sharpe: float
    days:   int


def _run(strategy_factory, sym: str, days: int, label: str,
         dfs_4h, dfs_1d, *, use_bias: bool, sl_mult=None, tp_mult=None) -> R:
    cfg = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = RISK,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
    )
    strat = strategy_factory()
    if sl_mult is not None:
        strat.config.stop_atr_mult = sl_mult
    if tp_mult is not None:
        strat.config.tp_atr_mult = tp_mult
    engine = BacktestEngine(cfg, strategy=strat)
    df = dfs_4h[sym]
    df_bias = dfs_1d.get(sym) if use_bias else None
    bt = engine.run(df=df, df_4h=df_bias, symbol=sym)
    s  = engine.summary(bt)
    closed = [t for t in bt.trades if t.get("exit_reason") is not None]
    wins   = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    wr     = (100*wins/len(closed)) if closed else 0.0
    total_pct = (bt.final_capital/bt.initial_capital - 1)*100
    annual    = (total_pct/days)*365
    return R(
        label  = label,
        sym    = sym,
        pf     = s["profit_factor"],
        annual = annual,
        total  = total_pct,
        dd     = abs(s["max_drawdown_pct"]),
        tr     = s["total_trades"],
        wr     = wr,
        sharpe = s["sharpe_ratio"],
        days   = days,
    )


def _print_block(title: str, rows: list[R]) -> None:
    print(f"\n── {title} ──")
    print(f"{'Variant':<40s} | {'Sym':<7s} | {'PF':>5s} | {'Annual':>8s} | {'Total':>9s} | {'DD':>5s} | {'Tr':>3s} | {'WR':>6s}")
    print("-" * 100)
    for r in rows:
        print(f"{r.label:<40s} | {r.sym:<7s} | {r.pf:>5.2f} | {r.annual:>+7.1f}% | {r.total:>+8.1f}% | {r.dd:>4.1f}% | {r.tr:>3d} | {r.wr:>5.1f}%")


def _fetch(days: int):
    start = END_DT - timedelta(days=days)
    dfs_4h = {s: fetch_and_cache(s, "4h", start, END_DT) for s in SYMBOLS}
    dfs_1d = {s: fetch_and_cache(s, "1d", start, END_DT) for s in SYMBOLS}
    return dfs_4h, dfs_1d, days


def main() -> None:
    print(f"End: {END_DT.date()}   Risk: {RISK*100:.0f}%   Cost: {COST*100:.2f}%/side")

    # ── Stage A: Supertrend ATR×Multiplier grid on 6m (focused window) ────────
    dfs_4h_6m, dfs_1d_6m, d6 = _fetch(180)
    print(f"\nStage A: Supertrend (ATR period × multiplier) grid — 6m {SYMBOLS[0]}+{SYMBOLS[1]}")
    rows = []
    for atr_p in [7, 10, 14, 20]:
        for mult in [2.0, 2.5, 3.0, 3.5, 4.0]:
            label = f"ST atr={atr_p}, mult={mult}"
            for sym in SYMBOLS:
                rows.append(_run(
                    lambda atr_p=atr_p, mult=mult: SupertrendStrategy(SupertrendConfig(
                        atr_period=atr_p, multiplier=mult, long_only=True
                    )),
                    sym, d6, label, dfs_4h_6m, dfs_1d_6m, use_bias=True
                ))
    _print_block("A. Supertrend grid on 6 months (with 1d bias filter)", rows)

    # Aggregate per-config: average PF across BTC+ETH
    print("\n  Aggregate across BTC+ETH:")
    cfg_map: dict[str, list[R]] = {}
    for r in rows:
        cfg_map.setdefault(r.label, []).append(r)
    agg = []
    for label, rs in cfg_map.items():
        avg_pf = sum(r.pf for r in rs)/len(rs)
        avg_ann = sum(r.annual for r in rs)/len(rs)
        avg_dd = sum(r.dd for r in rs)/len(rs)
        sum_tr = sum(r.tr for r in rs)
        avg_wr = sum(r.wr*r.tr for r in rs) / sum_tr if sum_tr else 0
        agg.append((label, avg_pf, avg_ann, avg_dd, sum_tr, avg_wr))
    agg.sort(key=lambda x: -x[1])
    print(f"  {'Cfg':<25s} | {'Avg PF':>6s} | {'Avg Ann':>8s} | {'Avg DD':>6s} | {'Tr':>3s} | {'WR':>6s}")
    for label, pf, ann, dd, tr, wr in agg[:10]:
        print(f"  {label:<25s} | {pf:>6.2f} | {ann:>+7.1f}% | {dd:>5.1f}% | {tr:>3d} | {wr:>5.1f}%")

    # ── Stage B: Validate top picks on 3-year window ──────────────────────────
    dfs_4h_3y, dfs_1d_3y, d36 = _fetch(1095)
    print(f"\nStage B: Validate top Supertrend configs on 3-year window")
    # Pick top 4 configs from 6m by avg PF
    top_cfgs = [a[0] for a in agg[:4]]
    rows = []
    for label in top_cfgs:
        # parse atr and mult from label
        parts = label.split()
        atr_p = int(parts[1].split('=')[1].rstrip(','))
        mult  = float(parts[2].split('=')[1])
        for sym in SYMBOLS:
            rows.append(_run(
                lambda atr_p=atr_p, mult=mult: SupertrendStrategy(SupertrendConfig(
                    atr_period=atr_p, multiplier=mult, long_only=True
                )),
                sym, d36, label, dfs_4h_3y, dfs_1d_3y, use_bias=True
            ))
    _print_block("B. Top Supertrend configs over 3 years", rows)

    # ── Stage C: Compare EMA baseline vs best Supertrend on 3 years ───────────
    print(f"\nStage C: Production EMA vs best Supertrend — 3y horizon")
    rows = []
    # PROD EMA config
    def prod_ema():
        return EMACrossoverStrategy(EMACrossoverConfig(
            long_only=True, volume_multiplier=1.5,
            require_bar_direction=True, require_ema_momentum=True,
            min_atr_pct=0.005, max_distance_atr=1.0,
            stop_atr_mult=1.5, tp_atr_mult=5.0,
        ))
    for sym in SYMBOLS:
        rows.append(_run(prod_ema, sym, d36, "EMA-prod (1.5x/5.0x)",
                          dfs_4h_3y, dfs_1d_3y, use_bias=True))
    # Best Supertrend (use top from stage B)
    best_label, best_pf = agg[0][0], agg[0][1]
    parts = best_label.split()
    best_atr  = int(parts[1].split('=')[1].rstrip(','))
    best_mult = float(parts[2].split('=')[1])
    for sym in SYMBOLS:
        rows.append(_run(
            lambda: SupertrendStrategy(SupertrendConfig(
                atr_period=best_atr, multiplier=best_mult, long_only=True
            )),
            sym, d36, f"ST best ({best_label})", dfs_4h_3y, dfs_1d_3y, use_bias=True
        ))
    # Supertrend defaults (10, 3.0)
    for sym in SYMBOLS:
        rows.append(_run(
            lambda: SupertrendStrategy(SupertrendConfig(atr_period=10, multiplier=3.0, long_only=True)),
            sym, d36, "ST default (10, 3.0)", dfs_4h_3y, dfs_1d_3y, use_bias=True
        ))
    _print_block("C. EMA-prod vs Supertrend — 3y", rows)

    # ── Stage D: Supertrend with TP/SL tweaks (3y) ─────────────────────────────
    print(f"\nStage D: Best Supertrend × SL/TP grid — 3y")
    rows = []
    for sl in [1.0, 1.5, 2.0]:
        for tp in [3.0, 4.5, 6.0]:
            label = f"ST best SL={sl} TP={tp}"
            for sym in SYMBOLS:
                rows.append(_run(
                    lambda: SupertrendStrategy(SupertrendConfig(
                        atr_period=best_atr, multiplier=best_mult, long_only=True
                    )),
                    sym, d36, label, dfs_4h_3y, dfs_1d_3y, use_bias=True,
                    sl_mult=sl, tp_mult=tp,
                ))
    _print_block("D. Supertrend SL/TP grid — 3y", rows)

    # Aggregate D
    cfg_map.clear()
    for r in rows:
        cfg_map.setdefault(r.label, []).append(r)
    agg2 = []
    for label, rs in cfg_map.items():
        avg_pf = sum(r.pf for r in rs)/len(rs)
        avg_ann = sum(r.annual for r in rs)/len(rs)
        avg_dd = sum(r.dd for r in rs)/len(rs)
        sum_tr = sum(r.tr for r in rs)
        avg_wr = sum(r.wr*r.tr for r in rs)/sum_tr if sum_tr else 0
        agg2.append((label, avg_pf, avg_ann, avg_dd, sum_tr, avg_wr))
    agg2.sort(key=lambda x: -x[1])
    print("\n  Aggregate D across BTC+ETH (sorted by Avg PF):")
    print(f"  {'Cfg':<30s} | {'Avg PF':>6s} | {'Avg Ann':>8s} | {'Avg DD':>6s} | {'Tr':>3s} | {'WR':>6s}")
    for label, pf, ann, dd, tr, wr in agg2:
        print(f"  {label:<30s} | {pf:>6.2f} | {ann:>+7.1f}% | {dd:>5.1f}% | {tr:>3d} | {wr:>5.1f}%")


if __name__ == "__main__":
    main()
