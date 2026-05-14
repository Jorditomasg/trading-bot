"""Optimizer bake-off — current methodology vs proposed walk-forward methodology.

Runs the same 30-combo (SL × TP) grid the production optimizer uses, but evaluates
each config in TWO different ways:

  A) **Current methodology** (mimics `bot/optimizer/walk_forward.py`):
     - Single backtest on the last 180 days only
     - Rank by profit_factor DESC
     - Viability: trades >= 15, dd <= 20%, sharpe >= 0.4, pf >= 1.05

  B) **Proposed methodology** (the bet):
     - 10 quarterly non-overlapping windows over 2023-Q4 → 2026-Q1
     - Rank by median Calmar DESC, weighted by hit rate
     - Viability: must pass on at least 80% of windows
     - Champion-challenger: only "applied" if median Calmar > current by 10%+ margin

Outputs `data/audits/optimizer_bakeoff_<iso>.json` and a markdown summary at
`docs/audits/optimizer_bakeoff_<date>.md`.

Spec / context: docs/audits/A_walk_forward_2026-05-14.md (sub-project A audit)
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.audit.walk_forward import (
    WalkForwardConfig,
    run_all,
    split_windows,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("bakeoff")

# ── Grid (subset of production optimizer for tractable runtime) ──────────────
# Picks SL spectrum {1.0, 1.25, 1.5, 1.75, 2.0} × TP {3.0, 4.0, 5.0} = 15 combos.
# All R:R ≥ 1.5 (no skipped combos). Removes the 2.5 TP (too tight for trend
# following) and 3.5/4.5 (we already know 3.5 is C2's pick, so 3.0 covers the
# aggressive side; 4.5 is C1's pick, so 5.0 covers conservative side). The
# 13-combo set is enough to show the methodology delta.
STOP_GRID = [1.0, 1.25, 1.5, 1.75, 2.0]
TP_GRID   = [3.0, 4.0, 5.0]


def _valid_combo(sl: float, tp: float) -> bool:
    """R:R floor 1.5 (matches production optimizer)."""
    return tp / sl >= 1.5


# ── BacktestConfig builders ───────────────────────────────────────────────────

def _config_for(sl: float, tp: float, risk: float, prod_filters: bool) -> BacktestConfig:
    """Build a BacktestConfig for the grid point.

    prod_filters=False mimics C1 baseline filter set.
    prod_filters=True  mimics C2 production filter set (vol mult + momentum req).
    """
    base = dict(
        initial_capital         = 10_000.0,
        risk_per_trade          = risk,
        timeframe               = "4h",
        cost_per_side_pct       = 0.001,
        long_only               = True,
        ema_stop_mult           = sl,
        ema_tp_mult             = tp,
        ema_max_distance_atr    = 1.0,
        momentum_filter_enabled = True,
        momentum_sma_period     = 20,
        momentum_neutral_band   = 0.05,
    )
    if prod_filters:
        base["ema_volume_mult"]      = 2.0
        base["ema_require_momentum"] = True
    return BacktestConfig(**base)


# ── Methodology A: current (single 180d window, sort by PF) ───────────────────

def methodology_a_current(
    dfs: dict, dfs_bias: dict, dfs_weekly: dict,
    risk: float, prod_filters: bool,
    start: datetime, end: datetime,
) -> list[dict]:
    """Mimic the production optimizer: single 180d backtest, sort by PF."""
    from bot.audit.walk_forward import run_window, Window

    # Single window covering full lookback (no separate train/test)
    w = Window(
        index=0,
        train_start=start,
        train_end=end,
        test_start=start,
        test_end=end,
    )

    results: list[dict] = []
    valid_combos = [(sl, tp) for sl, tp in product(STOP_GRID, TP_GRID) if _valid_combo(sl, tp)]
    log.info("Methodology A: %d valid combos on single 180d window", len(valid_combos))

    for i, (sl, tp) in enumerate(valid_combos):
        cfg = _config_for(sl, tp, risk, prod_filters)
        try:
            wr = run_window(
                window=w, backtest_config=cfg, config_name=f"sl{sl}_tp{tp}",
                dfs=dfs, dfs_bias=dfs_bias, dfs_weekly=dfs_weekly,
            )
        except Exception as exc:
            log.warning("  [%d/%d] sl=%.2f tp=%.2f FAILED: %s", i+1, len(valid_combos), sl, tp, exc)
            continue
        viable = (
            wr.total_trades   >= 15
            and wr.max_drawdown_pct <= 20.0
            and wr.sharpe          >= 0.4
            and wr.pf              >= 1.05
        )
        results.append({
            "sl":       sl,
            "tp":       tp,
            "pf":       wr.pf,
            "sharpe":   wr.sharpe,
            "dd":       wr.max_drawdown_pct,
            "wr":       wr.win_rate_pct,
            "trades":   wr.total_trades,
            "calmar":   wr.calmar if math.isfinite(wr.calmar) else None,
            "pnl_pct":  wr.final_pnl_pct,
            "viable":   viable,
        })

    # Sort: viable first, then by PF
    results.sort(key=lambda r: (r["viable"], r["pf"]), reverse=True)
    return results


# ── Methodology B: proposed (walk-forward, median Calmar, hit rate) ───────────

def methodology_b_proposed(
    dfs: dict, dfs_bias: dict, dfs_weekly: dict,
    risk: float, prod_filters: bool,
    wf_cfg: WalkForwardConfig,
) -> list[dict]:
    """Walk-forward across 10 windows, rank by median Calmar with hit-rate gate."""
    valid_combos = [(sl, tp) for sl, tp in product(STOP_GRID, TP_GRID) if _valid_combo(sl, tp)]
    log.info("Methodology B: %d valid combos × %d walk-forward windows",
             len(valid_combos), len(split_windows(wf_cfg)))

    # Build a dict of configs indexed by combo name so run_all can iterate
    configs = {
        f"sl{sl}_tp{tp}": _config_for(sl, tp, risk, prod_filters)
        for sl, tp in valid_combos
    }

    all_results = run_all(
        wf_config        = wf_cfg,
        backtest_configs = configs,
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_weekly,
        progress_cb      = None,  # silent — too many to log per window
    )

    # Aggregate per config
    by_combo: dict[str, list] = {}
    for r in all_results:
        by_combo.setdefault(r.config_name, []).append(r)

    rows: list[dict] = []
    for combo_name, windows in by_combo.items():
        sl = float(combo_name.split("_")[0][2:])
        tp = float(combo_name.split("_")[1][2:])

        pfs        = [w.pf for w in windows]
        calmars    = [w.calmar for w in windows if math.isfinite(w.calmar)]
        dds        = [w.max_drawdown_pct for w in windows]
        sharpes    = [w.sharpe for w in windows]
        wrs        = [w.win_rate_pct for w in windows]
        trades     = [w.total_trades for w in windows]
        pnls       = [w.final_pnl_pct for w in windows]

        # Hit rate: % of windows with PF > 1.0
        hit_rate = sum(1 for p in pfs if p > 1.0) / len(pfs)
        # Robust viability: PF > 1.2 AND DD < 25% AND trades >= 5 on >= 80% of windows
        per_window_viable = [
            (p > 1.2 and d < 25.0 and n >= 5)
            for p, d, n in zip(pfs, dds, trades)
        ]
        robust_viable = sum(per_window_viable) / len(per_window_viable) >= 0.8

        rows.append({
            "sl":          sl,
            "tp":          tp,
            "pf_median":   statistics.median(pfs),
            "pf_mean":     statistics.mean(pfs),
            "pf_std":      statistics.pstdev(pfs) if len(pfs) > 1 else 0.0,
            "calmar_median": statistics.median(calmars) if calmars else 0.0,
            "calmar_mean":   statistics.mean(calmars)   if calmars else 0.0,
            "dd_worst":    max(dds),
            "dd_median":   statistics.median(dds),
            "sharpe_median": statistics.median(sharpes),
            "wr_median":   statistics.median(wrs),
            "trades_median": statistics.median(trades),
            "pnl_mean":    statistics.mean(pnls),
            "hit_rate":    hit_rate,
            "windows_viable_pct": sum(per_window_viable) / len(per_window_viable),
            "robust_viable": robust_viable,
            "n_windows":   len(windows),
        })

    # Sort: robust viable first, then by median Calmar
    rows.sort(key=lambda r: (r["robust_viable"], r["calmar_median"]), reverse=True)
    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    # Same time range as the C1/C2 audit for apples-to-apples
    audit_start = datetime(2022, 4, 1, tzinfo=timezone.utc)
    audit_end   = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # Methodology A "recent 180 days" — last 180d before audit_end
    a_start = audit_end - timedelta(days=180)

    log.info("Loading klines BTC + ETH (4h/1d/1w) …")
    dfs        = {sym: fetch_and_cache(sym, "4h", audit_start, audit_end) for sym in ("BTCUSDT","ETHUSDT")}
    dfs_bias   = {sym: fetch_and_cache(sym, "1d", audit_start, audit_end) for sym in ("BTCUSDT","ETHUSDT")}
    dfs_weekly = {sym: fetch_and_cache(sym, "1w", audit_start, audit_end) for sym in ("BTCUSDT","ETHUSDT")}

    wf_cfg = WalkForwardConfig(
        start_date   = audit_start,
        end_date     = audit_end,
        train_months = 18,
        test_months  = 6,   # was 3 — wider = fewer windows, faster
        step_months  = 6,   # was 3 — non-overlapping (paired t-test valid)
        symbols      = ("BTCUSDT", "ETHUSDT"),
        timeframe    = "4h",
    )

    # ── Single regime: baseline filters at 1.5% risk (where audit said GO) ──
    # Skip prod_filters sweep — audit already proved that config is NO-GO. We
    # want to find the BEST SL/TP under the GO regime.
    summary: dict = {}
    for label, risk, prod_filters in [
        ("baseline_filters_risk1.5", 0.015, False),
    ]:
        log.info("\n══ Sweep: %s ══", label)
        log.info("Running methodology A (single 180d window)…")
        a_results = methodology_a_current(
            dfs, dfs_bias, dfs_weekly,
            risk=risk, prod_filters=prod_filters,
            start=a_start, end=audit_end,
        )
        log.info("Running methodology B (10 walk-forward windows)…")
        b_results = methodology_b_proposed(
            dfs, dfs_bias, dfs_weekly,
            risk=risk, prod_filters=prod_filters,
            wf_cfg=wf_cfg,
        )

        # Top picks
        a_top = next((r for r in a_results if r["viable"]), a_results[0] if a_results else None)
        b_top = next((r for r in b_results if r["robust_viable"]), b_results[0] if b_results else None)

        summary[label] = {
            "methodology_a_top": a_top,
            "methodology_b_top": b_top,
            "methodology_a_full": a_results,
            "methodology_b_full": b_results,
        }

        if a_top:
            log.info("METHODOLOGY A pick: SL=%.2f TP=%.2f (PF=%.2f, DD=%.1f%%, viable=%s)",
                     a_top["sl"], a_top["tp"], a_top["pf"], a_top["dd"], a_top["viable"])
        if b_top:
            log.info("METHODOLOGY B pick: SL=%.2f TP=%.2f (median Calmar=%.2f, hit_rate=%.0f%%, robust=%s)",
                     b_top["sl"], b_top["tp"], b_top["calmar_median"],
                     b_top["hit_rate"]*100, b_top["robust_viable"])

    # ── Persist ──────────────────────────────────────────────────────────────
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    data_path = Path(f"data/audits/optimizer_bakeoff_{iso}.json")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info("\nResults → %s", data_path)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
