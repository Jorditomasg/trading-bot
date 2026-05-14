"""CLI entry point for the walk-forward validation audit (sub-project A).

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py --only C1
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py --train-months 12 --test-months 6 --step-months 6

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.audit.comparison import compare_configs
from bot.audit.verdict import evaluate_verdict
from bot.audit.walk_forward import (
    WalkForwardConfig,
    aggregate_metrics,
    run_all,
)
from bot.audit.report import write_markdown_report
from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("audit")


# ── Spec-locked configs (DO NOT modify mid-audit; see spec section 5) ─────────

CONFIG_C1_BASELINE = BacktestConfig(
    initial_capital         = 10_000.0,
    risk_per_trade          = 0.015,
    timeframe               = "4h",
    cost_per_side_pct       = 0.001,
    long_only               = True,
    ema_stop_mult           = 1.5,
    ema_tp_mult             = 4.5,
    ema_max_distance_atr    = 1.0,
    momentum_filter_enabled = True,
    momentum_sma_period     = 20,
    momentum_neutral_band   = 0.05,
)

CONFIG_C2_PROD = BacktestConfig(
    initial_capital         = 10_000.0,
    risk_per_trade          = 0.03,
    timeframe               = "4h",
    cost_per_side_pct       = 0.001,
    long_only               = True,
    ema_stop_mult           = 1.25,
    ema_tp_mult             = 3.5,
    ema_max_distance_atr    = 1.0,
    ema_volume_mult         = 2.0,
    ema_require_momentum    = True,
    momentum_filter_enabled = True,
    momentum_sma_period     = 20,
    momentum_neutral_band   = 0.05,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-forward validation audit")
    p.add_argument("--start", type=str, default="2022-04-01",
                   help="ISO date for the earliest train_start")
    p.add_argument("--end",   type=str, default="2026-05-01",
                   help="ISO date for the latest test_end")
    p.add_argument("--train-months", type=int, default=18)
    p.add_argument("--test-months",  type=int, default=3)
    p.add_argument("--step-months",  type=int, default=3)
    p.add_argument("--only", type=str, default=None, choices=["C1", "C2"],
                   help="Run only the named config (debugging)")
    return p.parse_args()


def _to_jsonable(window_results) -> list[dict]:
    """Convert WindowResult dataclasses to plain dicts for JSON serialization."""
    out = []
    for r in window_results:
        d = asdict(r)
        # datetime objects in nested Window
        for key in ("train_start", "train_end", "test_start", "test_end"):
            d["window"][key] = d["window"][key].isoformat()
        # Replace inf floats (JSON can't represent them)
        for k, v in d.items():
            if isinstance(v, float) and (v == float("inf") or v == float("-inf")):
                d[k] = "Infinity" if v > 0 else "-Infinity"
        out.append(d)
    return out


def main() -> int:
    args = _parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    log.info("Walk-forward audit — start=%s end=%s train=%dm test=%dm step=%dm",
             start_dt.date(), end_dt.date(), args.train_months, args.test_months, args.step_months)

    # ── Fetch / load cached klines ──────────────────────────────────────────
    log.info("Loading klines (4h + 1d + 1w) for BTCUSDT and ETHUSDT …")
    dfs        = {sym: fetch_and_cache(sym, "4h", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_bias   = {sym: fetch_and_cache(sym, "1d", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_weekly = {sym: fetch_and_cache(sym, "1w", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}

    wf_cfg = WalkForwardConfig(
        start_date   = start_dt,
        end_date     = end_dt,
        train_months = args.train_months,
        test_months  = args.test_months,
        step_months  = args.step_months,
        symbols      = ("BTCUSDT", "ETHUSDT"),
        timeframe    = "4h",
    )

    bt_configs = {"C1": CONFIG_C1_BASELINE, "C2": CONFIG_C2_PROD}
    if args.only:
        bt_configs = {args.only: bt_configs[args.only]}

    # ── Run all (window × config) ───────────────────────────────────────────
    log.info("Running %d configs over expected windows …", len(bt_configs))
    results = run_all(
        wf_config        = wf_cfg,
        backtest_configs = bt_configs,
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_weekly,
        progress_cb      = log.info,
    )
    log.info("Collected %d window results", len(results))

    # ── Aggregate per config + verdicts + comparison ────────────────────────
    summaries: dict = {}
    for name in bt_configs:
        cfg_results = [r for r in results if r.config_name == name]
        if not cfg_results:
            continue
        agg     = aggregate_metrics(cfg_results)
        verdict = evaluate_verdict(agg)
        summaries[name] = {"aggregate": agg, "verdict": verdict, "n_windows": len(cfg_results)}

    comparison = None
    if "C1" in summaries and "C2" in summaries:
        # Use window index to pair correctly
        c1 = sorted([r for r in results if r.config_name == "C1"], key=lambda r: r.window.index)
        c2 = sorted([r for r in results if r.config_name == "C2"], key=lambda r: r.window.index)
        comparison = compare_configs([r.pf for r in c1], [r.pf for r in c2], metric_name="pf")

    # ── Persist raw JSON + markdown report ──────────────────────────────────
    iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    out_data_dir   = Path("data/audits"); out_data_dir.mkdir(parents=True, exist_ok=True)
    out_docs_dir   = Path("docs/audits"); out_docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_data_dir / f"A_walk_forward_{iso}.json"
    md_path   = out_docs_dir / f"A_walk_forward_{datetime.now().strftime('%Y-%m-%d')}.md"

    payload = {
        "args":       vars(args),
        "results":    _to_jsonable(results),
        "summaries":  summaries,
        "comparison": comparison,
        "generated":  iso,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("Raw results → %s", json_path)

    write_markdown_report(payload, md_path)
    log.info("Report → %s", md_path)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
