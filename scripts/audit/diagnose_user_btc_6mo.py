"""Diagnose user's BTC 6mo backtest result (PF=0.90, WR=25%, 16 trades).

Steps:
 1. Read seeds from the live DB and contrast with CONFIG_C3_LIVE.
 2. Reproduce the user's BTC-only 6mo backtest using the seeded config.
 3. Run walk-forward BTC-only over the available 3yr cache, classify each
    window by 30-day return regime, and locate where the user's 6mo lands.
 4. Print a verdict: is this window a typical/atypical bucket, or did the
    config diverge from C3_LIVE?

Run:
    PYTHONPATH=. .venv/bin/python3 scripts/audit/diagnose_user_btc_6mo.py
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from bot.audit.regime_classifier import RegimeLabel, classify_window
from bot.audit.walk_forward import (
    WalkForwardConfig, Window, aggregate_metrics, run_all, split_windows,
)
from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("diag")

REPORT_END   = datetime(2026, 5, 17, tzinfo=timezone.utc)
SIX_MO_START = REPORT_END - timedelta(days=180)
HISTORY_START = datetime(2023, 1, 1, tzinfo=timezone.utc)  # 28mo of windows

DB_PATH = "trading_bot.db"


def _read_seeds_from_db() -> dict[str, str]:
    """Read bot_config KV pairs that govern backtest behaviour."""
    if not Path(DB_PATH).exists():
        return {}
    keys = [
        "risk_per_trade", "ema_stop_mult", "ema_tp_mult", "ema_max_dist_atr",
        "ema_vol_mult", "ema_bar_dir", "ema_momentum_req",
        "momentum_neutral_band", "backtest_cost_per_side",
    ]
    out = {}
    with sqlite3.connect(DB_PATH) as cx:
        for k in keys:
            row = cx.execute(
                "SELECT value FROM bot_config WHERE key=?", (k,)
            ).fetchone()
            if row:
                out[k] = row[0]
    return out


def _seeds_to_config(seeds: dict[str, str]) -> BacktestConfig:
    """Build a BacktestConfig from DB seeds, falling back to C3_LIVE defaults."""
    def _f(key, default):
        return float(seeds.get(key, default))
    def _b(key, default):
        return seeds.get(key, str(default)).lower() == "true"
    return BacktestConfig(
        initial_capital         = 10_000.0,
        risk_per_trade          = _f("risk_per_trade", 0.015),
        timeframe               = "4h",
        cost_per_side_pct       = _f("backtest_cost_per_side", 0.001),
        long_only               = True,
        ema_stop_mult           = _f("ema_stop_mult", 1.5),
        ema_tp_mult             = _f("ema_tp_mult", 5.0),
        ema_max_distance_atr    = _f("ema_max_dist_atr", 1.0),
        ema_volume_mult         = _f("ema_vol_mult", 1.5),
        ema_require_bar_dir     = _b("ema_bar_dir", True),
        ema_require_momentum    = _b("ema_momentum_req", True),
        momentum_filter_enabled = True,
        momentum_sma_period     = 20,
        momentum_neutral_band   = _f("momentum_neutral_band", 0.08),
    )


def _c3_live() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.015, timeframe="4h",
        cost_per_side_pct=0.001, long_only=True,
        ema_stop_mult=1.5, ema_tp_mult=5.0, ema_max_distance_atr=1.0,
        ema_volume_mult=1.5, ema_require_bar_dir=True, ema_require_momentum=True,
        momentum_filter_enabled=True, momentum_sma_period=20,
        momentum_neutral_band=0.08,
    )


def _classify(df_4h: pd.DataFrame, window_start: datetime, label: str) -> RegimeLabel:
    """Best-effort window classifier wrapping the audit primitive."""
    class _W:
        def __init__(self, ts):
            self.test_start = ts
    try:
        return classify_window(_W(window_start), df_4h)
    except Exception as exc:                                # pragma: no cover
        log.warning("regime classifier failed for %s: %s", label, exc)
        return RegimeLabel.FLAT


def _run_single(cfg: BacktestConfig, start: datetime, end: datetime) -> dict:
    # cache columns: open_time, open, high, low, close, volume (RangeIndex)
    df = fetch_and_cache("BTCUSDT", "4h", start - timedelta(days=20), end)
    df_bias = fetch_and_cache("BTCUSDT", "1d", start - timedelta(days=20), end)
    df_w = fetch_and_cache("BTCUSDT", "1w", start - timedelta(days=40), end)
    df = df[df["open_time"] >= start].reset_index(drop=True)
    engine = BacktestEngine(cfg)
    res = engine.run(df, df_4h=df_bias, df_weekly=df_w)
    return engine.summary(res)


def main() -> int:
    seeds = _read_seeds_from_db()
    cfg_seeded = _seeds_to_config(seeds)
    cfg_c3 = _c3_live()

    print("\n" + "═" * 72)
    print(" DIAGNOSTIC — User's BTC 6mo backtest (reported PF=0.90, WR=25%)")
    print("═" * 72)

    # ── STEP 1 — DB seeds vs C3_LIVE ────────────────────────────────────────
    print("\n[1] DB seeds vs CONFIG_C3_LIVE")
    print("-" * 72)
    print(f"  {'Param':<28} {'DB seed':>14} {'C3_LIVE':>14} {'match?':>10}")
    fields = [
        ("risk_per_trade",          cfg_seeded.risk_per_trade,          cfg_c3.risk_per_trade),
        ("ema_stop_mult",           cfg_seeded.ema_stop_mult,           cfg_c3.ema_stop_mult),
        ("ema_tp_mult",             cfg_seeded.ema_tp_mult,             cfg_c3.ema_tp_mult),
        ("ema_max_distance_atr",    cfg_seeded.ema_max_distance_atr,    cfg_c3.ema_max_distance_atr),
        ("ema_volume_mult",         cfg_seeded.ema_volume_mult,         cfg_c3.ema_volume_mult),
        ("ema_require_bar_dir",     cfg_seeded.ema_require_bar_dir,     cfg_c3.ema_require_bar_dir),
        ("ema_require_momentum",    cfg_seeded.ema_require_momentum,    cfg_c3.ema_require_momentum),
        ("momentum_neutral_band",   cfg_seeded.momentum_neutral_band,   cfg_c3.momentum_neutral_band),
        ("cost_per_side_pct",       cfg_seeded.cost_per_side_pct,       cfg_c3.cost_per_side_pct),
    ]
    any_mismatch = False
    for name, a, b in fields:
        ok = (a == b)
        if not ok:
            any_mismatch = True
        print(f"  {name:<28} {str(a):>14} {str(b):>14} {'✅' if ok else '❌':>10}")
    if not seeds:
        print("  (no DB found at trading_bot.db — using C3_LIVE defaults)")
    elif any_mismatch:
        print("\n  ⚠️  At least one DB seed diverges from C3_LIVE.")
    else:
        print("\n  ✅ DB seeds match C3_LIVE 1:1 (Phase 1 parity confirmed).")

    # ── STEP 2 — reproduce user's window ─────────────────────────────────────
    print("\n[2] Reproducing user's 6mo BTC backtest")
    print("-" * 72)
    summary = _run_single(cfg_c3, SIX_MO_START, REPORT_END)
    print(f"  PF             = {summary['profit_factor']:.2f}")
    print(f"  WR             = {summary['win_rate_pct']:.1f}%")
    print(f"  Trades         = {summary['total_trades']}")
    print(f"  Net PnL        = {summary['total_pnl_pct']:+.2f}%")
    print(f"  Max DD         = {summary['max_drawdown_pct']:.1f}%")
    print(f"  Sharpe         = {summary['sharpe_ratio']:.2f}")
    print(f"  User reported  : PF=0.90, WR=25%, 16 trades, DD=6.5%, PnL=-1.5%, Sharpe=-0.22")
    delta_pf = abs(summary["profit_factor"] - 0.90)
    if delta_pf < 0.1:
        print("  ✅ Reproduction within ±0.10 PF tolerance.")
    else:
        print(f"  ⚠️  Repro PF diverges by {delta_pf:.2f} — dashboard may use different config.")

    # ── STEP 3 — regime classification ────────────────────────────────────────
    print("\n[3] Window regime classification")
    print("-" * 72)
    df_4h_full = fetch_and_cache("BTCUSDT", "4h",
                                 HISTORY_START - timedelta(days=60), REPORT_END)
    regime = _classify(df_4h_full, SIX_MO_START, "user-6mo")
    print(f"  User's 6mo (start={SIX_MO_START.date()}) regime: {regime.value}")

    # ── STEP 4 — walk-forward BTC-only, locate the bucket ────────────────────
    print("\n[4] Walk-forward BTC-only (rolling 3mo windows over the cache)")
    print("-" * 72)
    wf = WalkForwardConfig(
        start_date   = HISTORY_START - timedelta(days=540),
        end_date     = REPORT_END,
        train_months = 6,    # short warm-up — we want as many test windows as possible
        test_months  = 3,
        step_months  = 3,
        symbols      = ("BTCUSDT",),
        timeframe    = "4h",
    )
    df_btc_4h_pad = fetch_and_cache(
        "BTCUSDT", "4h",
        (HISTORY_START - timedelta(days=540)) - timedelta(days=60), REPORT_END,
    )
    df_btc_1d_pad = fetch_and_cache(
        "BTCUSDT", "1d",
        (HISTORY_START - timedelta(days=540)) - timedelta(days=60), REPORT_END,
    )
    df_btc_1w_pad = fetch_and_cache(
        "BTCUSDT", "1w",
        (HISTORY_START - timedelta(days=540)) - timedelta(days=120), REPORT_END,
    )
    results = run_all(
        wf,
        backtest_configs={"C3": cfg_c3},
        dfs={"BTCUSDT": df_btc_4h_pad},
        dfs_bias={"BTCUSDT": df_btc_1d_pad},
        dfs_weekly={"BTCUSDT": df_btc_1w_pad},
    )
    if not results:
        print("  ⚠️  No windows produced — cache too short.")
        return 1

    pfs = [r.pf for r in results]
    win_rates = [r.win_rate_pct for r in results]
    n_trades = [r.total_trades for r in results]
    dds = [r.max_drawdown_pct for r in results]

    print(f"  windows={len(results)}  pf_mean={sum(pfs)/len(pfs):.2f}  "
          f"pf_min={min(pfs):.2f}  pf_max={max(pfs):.2f}")
    print(f"  wr_mean={sum(win_rates)/len(win_rates):.1f}%  "
          f"trades_per_window_mean={sum(n_trades)/len(n_trades):.1f}  "
          f"dd_worst={max(dds):.1f}%")

    below_1 = sum(1 for p in pfs if p < 1.0)
    print(f"  windows with PF<1.0: {below_1}/{len(results)}")

    print("\n  Per-window detail (3mo each):")
    print(f"  {'#':>2} {'test_start':<12} {'regime':<6} {'PF':>5} {'WR%':>5} "
          f"{'trades':>6} {'DD%':>5} {'PnL%':>6}")
    for i, r in enumerate(results):
        wstart_dt = r.window.test_start if r.window.test_start.tzinfo \
                    else r.window.test_start.replace(tzinfo=timezone.utc)
        wregime = _classify(df_btc_4h_pad, wstart_dt, f"w{i}")
        print(f"  {i:>2} {wstart_dt.date()!s:<12} {wregime.value:<6} "
              f"{r.pf:>5.2f} {r.win_rate_pct:>5.1f} "
              f"{r.total_trades:>6} {r.max_drawdown_pct:>5.1f} "
              f"{r.final_pnl_pct:>+6.1f}")

    # ── STEP 5 — verdict ──────────────────────────────────────────────────────
    print("\n[5] Verdict")
    print("-" * 72)
    user_pf = summary["profit_factor"]
    window_count = len(results)
    same_or_worse = sum(1 for p in pfs if p <= user_pf)
    pct_below = 100 * same_or_worse / window_count
    pf_mean = sum(pfs) / len(pfs)
    if user_pf < 1.0 and pct_below >= 30:
        print(f"  6mo PF={user_pf:.2f} is within the expected bucket of bad windows.")
        print(f"  {same_or_worse}/{window_count} ({pct_below:.0f}%) of WF windows had PF≤{user_pf:.2f}.")
        print(f"  WF mean PF={pf_mean:.2f}. Single-window variance is the signal here — NOT a bug.")
    elif user_pf < pf_mean - 0.3:
        print(f"  6mo PF={user_pf:.2f} is materially worse than WF mean PF={pf_mean:.2f}.")
        print( "  Possible drivers: window-specific regime, atypical liquidity, or remaining config drift.")
    else:
        print(f"  6mo PF={user_pf:.2f} is close to WF mean PF={pf_mean:.2f}.")
        print( "  This is the strategy's normal performance — variance fluctuates window-to-window.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
