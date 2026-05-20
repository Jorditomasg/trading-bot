"""EMA + Supertrend combination strategy.

Hypothesis: the 6m drought is because EMA crossover is too restrictive. Supertrend
caught additional moves that EMA missed (Stage A showed 14 trades vs EMA's 11 with
PF 1.31). Add Supertrend as an *additional* entry trigger; SL/TP and bias use the
same machinery as the EMA path.

Implements a CompositeStrategy that fires BUY if either:
  - EMA crossover/continuation says BUY, OR
  - Supertrend just flipped to up direction
and HOLD otherwise.

SL/TP from EMA's ATR multipliers (1.5/5.0). Long-only.

Tested on:
  6m  — match the user's complaint window
  3y  — robustness check against curve-fit
Per-symbol BTC and ETH.
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
from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.levels import calculate_levels
from bot.strategy.signal_factory import buy_signal, hold_signal, sell_signal
from bot.strategy.supertrend import SupertrendConfig, SupertrendStrategy, _compute_supertrend


SYMBOLS = ["BTCUSDT", "ETHUSDT"]
RISK    = 0.02
COST    = 0.001
CAPITAL = 10_000.0
END_DT  = datetime(2026, 5, 17, tzinfo=timezone.utc)


@dataclass
class CompositeConfig:
    # EMA defaults — production-equivalent
    ema_fast:          int   = 9
    ema_slow:          int   = 21
    ema_max_dist_atr:  float = 1.0
    ema_volume_mult:   float = 1.5
    ema_require_bar:   bool  = True
    ema_require_mom:   bool  = True
    ema_min_atr_pct:   float = 0.005
    # Supertrend — Stage A 6m sweet spot
    st_atr_period:     int   = 7
    st_multiplier:     float = 2.0
    # Common
    atr_period:        int   = 14
    stop_atr_mult:     float = 1.5
    tp_atr_mult:       float = 5.0
    long_only:         bool  = True


class CompositeEMASupertrend(BaseStrategy):
    """OR-gate: BUY if either EMA or Supertrend says BUY (using EMA's SL/TP framing)."""
    def __init__(self, cfg: CompositeConfig = CompositeConfig()) -> None:
        self.cfg = cfg
        self._ema = EMACrossoverStrategy(EMACrossoverConfig(
            fast_period       = cfg.ema_fast,
            slow_period       = cfg.ema_slow,
            atr_period        = cfg.atr_period,
            max_distance_atr  = cfg.ema_max_dist_atr,
            stop_atr_mult     = cfg.stop_atr_mult,
            tp_atr_mult       = cfg.tp_atr_mult,
            volume_multiplier = cfg.ema_volume_mult,
            min_atr_pct       = cfg.ema_min_atr_pct,
            require_bar_direction = cfg.ema_require_bar,
            require_ema_momentum  = cfg.ema_require_mom,
            long_only         = cfg.long_only,
        ))
        self._st = SupertrendStrategy(SupertrendConfig(
            atr_period    = cfg.st_atr_period,
            multiplier    = cfg.st_multiplier,
            stop_atr_mult = cfg.stop_atr_mult,
            tp_atr_mult   = cfg.tp_atr_mult,
            long_only     = cfg.long_only,
        ))

    @property
    def name(self) -> str:
        return "EMA_OR_SUPERTREND"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        ema_sig = self._ema.generate_signal(df)
        st_sig  = self._st.generate_signal(df)
        if ema_sig.action == "BUY":
            return ema_sig
        if st_sig.action == "BUY":
            # Re-stamp with EMA-style SL/TP so the rest of the pipeline behaves identically
            atr_s = compute_atr(df, self.cfg.atr_period)
            current_atr = float(atr_s.iloc[-1])
            price = float(df["close"].iloc[-1])
            sl, tp = calculate_levels("BUY", price, current_atr, self.cfg.stop_atr_mult, self.cfg.tp_atr_mult)
            return buy_signal(strength=max(st_sig.strength, 0.65), stop_loss=sl, take_profit=tp, atr=current_atr)
        if not self.cfg.long_only:
            if ema_sig.action == "SELL":
                return ema_sig
            if st_sig.action == "SELL":
                atr_s = compute_atr(df, self.cfg.atr_period)
                current_atr = float(atr_s.iloc[-1])
                price = float(df["close"].iloc[-1])
                sl, tp = calculate_levels("SELL", price, current_atr, self.cfg.stop_atr_mult, self.cfg.tp_atr_mult)
                return sell_signal(strength=max(st_sig.strength, 0.65), stop_loss=sl, take_profit=tp, atr=current_atr)
        return hold_signal(atr=ema_sig.atr)


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


def _run(strategy_factory, sym: str, days: int, dfs_4h, dfs_1d, *, label: str) -> R:
    cfg = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = RISK,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
    )
    strat = strategy_factory()
    engine = BacktestEngine(cfg, strategy=strat)
    bt = engine.run(df=dfs_4h[sym], df_4h=dfs_1d.get(sym), symbol=sym)
    s  = engine.summary(bt)
    closed = [t for t in bt.trades if t.get("exit_reason") is not None]
    wins   = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    wr     = (100*wins/len(closed)) if closed else 0.0
    total_pct = (bt.final_capital/bt.initial_capital - 1)*100
    annual    = (total_pct/days)*365
    return R(label, sym, s["profit_factor"], annual, total_pct,
             abs(s["max_drawdown_pct"]), s["total_trades"], wr)


def _print(rows: list[R], title: str) -> None:
    print(f"\n── {title} ──")
    print(f"{'Variant':<32s} | {'Sym':<7s} | {'PF':>5s} | {'Annual':>8s} | {'Total':>9s} | {'DD':>5s} | {'Tr':>3s} | {'WR':>6s}")
    print("-" * 95)
    for r in rows:
        print(f"{r.label:<32s} | {r.sym:<7s} | {r.pf:>5.2f} | {r.annual:>+7.1f}% | {r.total:>+8.1f}% | {r.dd:>4.1f}% | {r.tr:>3d} | {r.wr:>5.1f}%")


def _agg(rows: list[R]) -> tuple[float, float, float, int, float]:
    """Combine per-symbol metrics into a portfolio-style summary by averaging
    PF / annual / DD and summing trades, weighted by trade count for WR.
    """
    if not rows: return 0,0,0,0,0
    pf  = sum(r.pf  for r in rows)/len(rows)
    ann = sum(r.annual for r in rows)/len(rows)
    dd  = sum(r.dd for r in rows)/len(rows)
    tr  = sum(r.tr for r in rows)
    wr  = sum(r.wr*r.tr for r in rows)/tr if tr else 0
    return pf, ann, dd, tr, wr


def main() -> None:
    print(f"Risk: {RISK*100:.0f}%   Cost: {COST*100:.2f}%/side", flush=True)

    for days, tag in [(180, "6 MONTHS"), (1095, "3 YEARS")]:
        print(f"\n{'='*95}\n{tag} window\n{'='*95}", flush=True)
        start = END_DT - timedelta(days=days)
        dfs_4h = {s: fetch_and_cache(s, "4h", start, END_DT) for s in SYMBOLS}
        dfs_1d = {s: fetch_and_cache(s, "1d", start, END_DT) for s in SYMBOLS}

        cases = [
            ("EMA-prod baseline", lambda: EMACrossoverStrategy(EMACrossoverConfig(
                long_only=True, volume_multiplier=1.5, require_bar_direction=True,
                require_ema_momentum=True, min_atr_pct=0.005, max_distance_atr=1.0,
                stop_atr_mult=1.5, tp_atr_mult=5.0,
            ))),
            ("EMA-prod TP=3.5", lambda: EMACrossoverStrategy(EMACrossoverConfig(
                long_only=True, volume_multiplier=1.5, require_bar_direction=True,
                require_ema_momentum=True, min_atr_pct=0.005, max_distance_atr=1.0,
                stop_atr_mult=1.5, tp_atr_mult=3.5,
            ))),
            ("EMA-prod relaxed (no mom/bar)", lambda: EMACrossoverStrategy(EMACrossoverConfig(
                long_only=True, volume_multiplier=1.5, require_bar_direction=False,
                require_ema_momentum=False, min_atr_pct=0.005, max_distance_atr=1.0,
                stop_atr_mult=1.5, tp_atr_mult=5.0,
            ))),
            ("EMA+ST OR-gate (ST 7,2.0)", lambda: CompositeEMASupertrend(CompositeConfig(
                ema_fast=9, ema_slow=21, ema_max_dist_atr=1.0,
                ema_volume_mult=1.5, ema_require_bar=True, ema_require_mom=True,
                ema_min_atr_pct=0.005, st_atr_period=7, st_multiplier=2.0,
                stop_atr_mult=1.5, tp_atr_mult=5.0, long_only=True,
            ))),
            ("EMA+ST OR-gate (ST 10,3.0)", lambda: CompositeEMASupertrend(CompositeConfig(
                ema_fast=9, ema_slow=21, ema_max_dist_atr=1.0,
                ema_volume_mult=1.5, ema_require_bar=True, ema_require_mom=True,
                ema_min_atr_pct=0.005, st_atr_period=10, st_multiplier=3.0,
                stop_atr_mult=1.5, tp_atr_mult=5.0, long_only=True,
            ))),
            ("EMA+ST OR-gate TP=3.5 (10,3.0)", lambda: CompositeEMASupertrend(CompositeConfig(
                ema_fast=9, ema_slow=21, ema_max_dist_atr=1.0,
                ema_volume_mult=1.5, ema_require_bar=True, ema_require_mom=True,
                ema_min_atr_pct=0.005, st_atr_period=10, st_multiplier=3.0,
                stop_atr_mult=1.5, tp_atr_mult=3.5, long_only=True,
            ))),
        ]
        all_rows: list[R] = []
        for label, factory in cases:
            for sym in SYMBOLS:
                all_rows.append(_run(factory, sym, days, dfs_4h, dfs_1d, label=label))
            print(f"  {label}: done", flush=True)
        _print(all_rows, f"Per-symbol — {tag}")

        # Aggregate per label
        print(f"\n  Aggregate (BTC+ETH avg) — {tag}:")
        print(f"  {'Variant':<32s} | {'Avg PF':>6s} | {'Avg Ann':>8s} | {'Avg DD':>6s} | {'Tr':>3s} | {'WR':>6s}")
        by_label: dict[str, list[R]] = {}
        for r in all_rows:
            by_label.setdefault(r.label, []).append(r)
        for label in [c[0] for c in cases]:
            pf, ann, dd, tr, wr = _agg(by_label[label])
            print(f"  {label:<32s} | {pf:>6.2f} | {ann:>+7.1f}% | {dd:>5.1f}% | {tr:>3d} | {wr:>5.1f}%")


if __name__ == "__main__":
    main()
