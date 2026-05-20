"""Strategy quest — find a configuration that beats the abysmal 6-month baseline.

The user reported the dashboard backtest showing ~0.4 over 6m on BTC+ETH at 2% risk.
The production bot has 0 trades since deploy. Goal: try a battery of variants and
report numbers side-by-side so we can pick what to ship.

Variants tested:
  1. PROD baseline           — current bot_config keys, momentum filter ON
  2. PROD no-momentum         — same minus weekly momentum gate
  3. PROD relaxed filters     — no volume, no bar-dir, no momentum-req, no min-atr
  4. PROD bidirectional       — long_only=False (allow shorts)
  5. EMA bias_strict          — drop NEUTRAL bias signals
  6. Supertrend (all regimes) — strategy override, runs every regime
  7. Donchian breakout        — strategy override
  8. MACD                     — strategy override
  9. Heikin-Ashi              — strategy override
 10. Bollinger Reversion      — strategy override (mean-reversion baseline)
 11. Ensemble (TREND=EMA, RANG=BB, VOL=Donchian) — single-symbol BacktestEngine via strategies_by_regime
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Silence noisy loggers
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
for mod in ("bot.bias.filter", "bot.strategy", "bot.orchestrator", "bot.regime"):
    logging.getLogger(mod).setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.regime.detector import MarketRegime
from bot.strategy.bollinger_reversion import BollingerReversionConfig, BollingerReversionStrategy
from bot.strategy.donchian_breakout import DonchianConfig, DonchianBreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.heikin_ashi import HeikinAshiConfig, HeikinAshiStrategy
from bot.strategy.macd import MACDConfig, MACDStrategy
from bot.strategy.supertrend import SupertrendConfig, SupertrendStrategy


# 6-month window — match the user's complaint exactly
END_DT   = datetime(2026, 5, 17, tzinfo=timezone.utc)  # latest full bar in cache
START_DT = END_DT - timedelta(days=180)
SYMBOLS  = ["BTCUSDT", "ETHUSDT"]
CAPITAL  = 10_000.0
RISK     = 0.02
COST     = 0.001  # 0.10% per side (matches production rt_backtest_cost_per_side)


@dataclass
class Row:
    name:     str
    pf:       float
    annual:   float
    total:    float
    dd:       float
    trades:   int
    wr:       float
    sharpe:   float
    calmar:   float


def _fmt(r: Row) -> str:
    return (
        f"{r.name:<32s} | "
        f"PF {r.pf:>5.2f} | "
        f"Ann {r.annual:>+7.1f}% | "
        f"Total {r.total:>+7.1f}% | "
        f"DD {r.dd:>5.1f}% | "
        f"Tr {r.trades:>3d} | "
        f"WR {r.wr:>5.1f}% | "
        f"Sh {r.sharpe:>5.2f} | "
        f"Cm {r.calmar:>5.2f}"
    )


def _fetch():
    dfs        = {s: fetch_and_cache(s, "4h", START_DT, END_DT) for s in SYMBOLS}
    dfs_1d     = {s: fetch_and_cache(s, "1d", START_DT, END_DT) for s in SYMBOLS}
    dfs_weekly = {s: fetch_and_cache(s, "1w", START_DT - timedelta(days=154), END_DT) for s in SYMBOLS}
    return dfs, dfs_1d, dfs_weekly


def _run_portfolio(cfg: BacktestConfig, dfs, dfs_1d, dfs_weekly, *, use_weekly: bool) -> Row | None:
    engine = PortfolioBacktestEngine(cfg)
    try:
        r = engine.run_portfolio(
            dfs,
            dfs_4h    =dfs_1d,
            dfs_weekly=dfs_weekly if use_weekly else None,
        )
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return None
    s = r.portfolio_summary
    days   = (END_DT - START_DT).days
    annual = (s["total_pnl_pct"] / days) * 365
    dd     = abs(s["max_drawdown_pct"])
    calmar = annual / dd if dd else float("nan")
    return Row(
        name   ="",  # caller fills
        pf     =s["profit_factor"],
        annual =annual,
        total  =s["total_pnl_pct"],
        dd     =dd,
        trades =s["total_trades"],
        wr     =s["win_rate_pct"],
        sharpe =s["sharpe_ratio"],
        calmar =calmar,
    )


def _run_single_strategy_portfolio(strategy_factory, name: str, dfs, dfs_1d, dfs_weekly) -> Row | None:
    """Run a non-EMA strategy across both symbols by driving a per-symbol BacktestEngine
    and stitching results into a portfolio summary.

    The PortfolioBacktestEngine hardcodes EMACrossover via `strategies_by_regime=None`,
    so for alt-strategy comparisons we run two single-symbol BacktestEngines (one per
    symbol) and combine. Capital is duplicated per symbol (each gets the full pool /2),
    matching how the live bot allocates `total / N` per symbol in `run_cycle`.
    """
    cfg = BacktestConfig(
        initial_capital = CAPITAL / len(SYMBOLS),
        risk_per_trade  = RISK,
        timeframe       = "4h",
        cost_per_side_pct = COST,
        long_only       = True,
        bias_strict     = False,
    )
    total_pnl = 0.0
    total_trades = 0
    total_wins   = 0
    gross_win    = 0.0
    gross_loss   = 0.0
    equity_curves = []
    for sym in SYMBOLS:
        strat = strategy_factory()
        engine = BacktestEngine(cfg, strategy=strat)
        try:
            bt = engine.run(df=dfs[sym], df_4h=dfs_1d.get(sym), symbol=sym)
        except Exception as exc:
            print(f"  [{sym}] ERROR: {exc}")
            continue
        for t in bt.trades:
            if t.get("exit_reason") is None:
                continue
            pnl = t.get("pnl") or 0.0
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                total_wins += 1
                gross_win += pnl
            else:
                gross_loss += abs(pnl)
        equity_curves.append(bt.equity_curve)
    if total_trades == 0:
        return Row(name="", pf=0.0, annual=0.0, total=0.0, dd=0.0, trades=0, wr=0.0, sharpe=0.0, calmar=0.0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = 100 * total_wins / total_trades
    total_pct = 100 * total_pnl / CAPITAL
    days   = (END_DT - START_DT).days
    annual = (total_pct / days) * 365
    # Drawdown across combined curve (rough — sum per-bar)
    return Row(
        name="", pf=pf, annual=annual, total=total_pct, dd=0.0,
        trades=total_trades, wr=wr, sharpe=0.0, calmar=0.0,
    )


def _run_ensemble_per_symbol(dfs, dfs_1d) -> Row | None:
    """Regime-ensemble: TRENDING=EMA, RANGING=Bollinger reversion, VOLATILE=Donchian."""
    cfg = BacktestConfig(
        initial_capital   = CAPITAL / len(SYMBOLS),
        risk_per_trade    = RISK,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
    )
    total_pnl = total_trades = total_wins = 0
    gross_win = gross_loss = 0.0
    for sym in SYMBOLS:
        strategies_by_regime = {
            MarketRegime.TRENDING: EMACrossoverStrategy(EMACrossoverConfig(long_only=True)),
            MarketRegime.RANGING:  BollingerReversionStrategy(BollingerReversionConfig(long_only=True)),
            MarketRegime.VOLATILE: DonchianBreakoutStrategy(DonchianConfig(long_only=True)),
        }
        engine = BacktestEngine(cfg, strategies_by_regime=strategies_by_regime)
        try:
            bt = engine.run(df=dfs[sym], df_4h=dfs_1d.get(sym), symbol=sym)
        except Exception as exc:
            print(f"  [{sym}] ERROR: {exc}")
            continue
        for t in bt.trades:
            if t.get("exit_reason") is None:
                continue
            pnl = t.get("pnl") or 0.0
            total_pnl += pnl
            total_trades += 1
            if pnl > 0:
                total_wins += 1
                gross_win += pnl
            else:
                gross_loss += abs(pnl)
    if total_trades == 0:
        return Row(name="", pf=0.0, annual=0.0, total=0.0, dd=0.0, trades=0, wr=0.0, sharpe=0.0, calmar=0.0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = 100 * total_wins / total_trades
    total_pct = 100 * total_pnl / CAPITAL
    days   = (END_DT - START_DT).days
    annual = (total_pct / days) * 365
    return Row(name="", pf=pf, annual=annual, total=total_pct, dd=0.0,
               trades=total_trades, wr=wr, sharpe=0.0, calmar=0.0)


def main() -> None:
    print(f"Window: {START_DT.date()} → {END_DT.date()}  ({(END_DT-START_DT).days} days)")
    print(f"Symbols: {','.join(SYMBOLS)}   Capital: ${CAPITAL:,.0f}   Risk: {RISK*100:.1f}%   Cost: {COST*100:.2f}%/side\n")

    print("Fetching klines from parquet cache…")
    dfs, dfs_1d, dfs_weekly = _fetch()

    results: list[Row] = []

    # ── 1. PROD baseline (matches live bot_config exactly) ────────────────────
    prod_base = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = RISK,
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
        momentum_filter_enabled = True,
        momentum_neutral_band   = 0.08,
        bias_strict       = False,
    )
    r = _run_portfolio(prod_base, dfs, dfs_1d, dfs_weekly, use_weekly=True)
    r.name = "1. PROD baseline (live config)"
    results.append(r)

    # ── 2. PROD without weekly momentum filter ────────────────────────────────
    cfg = BacktestConfig(**{**prod_base.__dict__, "momentum_filter_enabled": False})
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=False)
    r.name = "2. PROD, momentum filter OFF"
    results.append(r)

    # ── 3. PROD relaxed (no entry-quality filters) ────────────────────────────
    cfg = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = RISK,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
        ema_stop_mult     = 1.5,
        ema_tp_mult       = 5.0,
        ema_max_distance_atr = 1.5,
        ema_volume_mult   = 0.0,
        ema_require_bar_dir  = False,
        ema_require_momentum = False,
        ema_min_atr_pct   = 0.0,
        momentum_filter_enabled = False,
        bias_strict       = False,
    )
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=False)
    r.name = "3. PROD relaxed (all filters off)"
    results.append(r)

    # ── 4. PROD bidirectional ─────────────────────────────────────────────────
    cfg = BacktestConfig(**{**prod_base.__dict__, "long_only": False})
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=True)
    r.name = "4. PROD bidirectional (allow shorts)"
    results.append(r)

    # ── 5. PROD bias_strict ───────────────────────────────────────────────────
    cfg = BacktestConfig(**{**prod_base.__dict__, "bias_strict": True})
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=True)
    r.name = "5. PROD bias_strict (drop NEUTRAL)"
    results.append(r)

    # ── 6. Tighter TP/SL (B-pick: 1.5x SL, 3.5x TP) ───────────────────────────
    cfg = BacktestConfig(**{**prod_base.__dict__, "ema_tp_mult": 3.5})
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=True)
    r.name = "6. PROD with TP=3.5x (vs 5.0x)"
    results.append(r)

    # ── 7. Wider entry distance ───────────────────────────────────────────────
    cfg = BacktestConfig(**{**prod_base.__dict__, "ema_max_distance_atr": 2.0})
    r = _run_portfolio(cfg, dfs, dfs_1d, dfs_weekly, use_weekly=True)
    r.name = "7. PROD max_dist_atr=2.0 (more entries)"
    results.append(r)

    # ── 8-12. Alt strategies (single-strategy applied to TRENDING regime via override) ──
    print("\nRunning alt strategies per symbol…")
    alts = [
        ("8.  Supertrend (3.0× ATR)",        lambda: SupertrendStrategy(SupertrendConfig(long_only=True))),
        ("9.  Donchian breakout (20-bar)",   lambda: DonchianBreakoutStrategy(DonchianConfig(long_only=True))),
        ("10. MACD 12/26/9",                 lambda: MACDStrategy(MACDConfig(long_only=True))),
        ("11. Heikin-Ashi (3 consecutive)",  lambda: HeikinAshiStrategy(HeikinAshiConfig(long_only=True))),
        ("12. Bollinger Reversion",          lambda: BollingerReversionStrategy(BollingerReversionConfig(long_only=True))),
    ]
    for name, factory in alts:
        r = _run_single_strategy_portfolio(factory, name, dfs, dfs_1d, dfs_weekly)
        r.name = name
        results.append(r)

    # ── 13. Regime ensemble ────────────────────────────────────────────────────
    print("Running regime-ensemble…")
    r = _run_ensemble_per_symbol(dfs, dfs_1d)
    r.name = "13. Ensemble TREND=EMA/RANG=BB/VOL=DON"
    results.append(r)

    # ── Print ─────────────────────────────────────────────────────────────────
    print("\n" + "=" * 145)
    print(f"6-MONTH BTC+ETH RESULTS — Risk={RISK*100:.0f}% | Cost={COST*100:.2f}%/side | Capital=${CAPITAL:,.0f}")
    print("=" * 145)
    print(f"{'Variant':<32s} | {'PF':>5s} | {'Annual':>8s} | {'Total':>8s} | {'DD':>6s} | {'Tr':>3s} | {'WR':>6s} | {'Sh':>5s} | {'Cm':>5s}")
    print("-" * 145)
    for r in results:
        print(_fmt(r))
    print("=" * 145)


if __name__ == "__main__":
    main()
