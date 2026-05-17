"""CLI entry point for champion-challenger walk-forward comparison (sub-project B).

Runs the walk-forward harness for both a champion and a challenger config, then calls
champion_vs_challenger() to produce a statistical comparison report.

Exit codes:
  0 → ADOPT (challenger wins, statistically significant)
  1 → REJECT (champion dominates or kill-switch triggered)
  2 → INCONCLUSIVE

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_champion_vs_challenger.py \
        --champion C1 --challenger C3
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_champion_vs_challenger.py \
        --champion C3 --challenger C3_ADX35 \
        --start 2022-04-01 --end 2026-05-01

Available config names (from run_walk_forward.py):
  C1  — CONFIG_C1_BASELINE (spec-locked, gotcha #32)
  C2  — CONFIG_C2_PROD (spec-locked, gotcha #32)
  C3  — CONFIG_C3_LIVE (Phase 1 baseline)

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md (Phase 3)
Design: D9 in sdd/win-rate-uplift-2026-05/design
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.audit.comparison import champion_vs_challenger
from bot.audit.kill_switch import evaluate_adx_kill_switch
from bot.audit.walk_forward import WalkForwardConfig, aggregate_metrics, run_all
from bot.backtest.cache import fetch_and_cache
from scripts.audit.run_walk_forward import (
    CONFIG_C1_BASELINE,
    CONFIG_C2_PROD,
    CONFIG_C3_LIVE,
    _to_jsonable,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("cc-audit")

# All available named configs (extend here for future challengers)
_NAMED_CONFIGS = {
    "C1": CONFIG_C1_BASELINE,
    "C2": CONFIG_C2_PROD,
    "C3": CONFIG_C3_LIVE,
}

# Exit-code mapping
_EXIT_CODES = {"ADOPT": 0, "REJECT": 1, "INCONCLUSIVE": 2}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Champion-challenger walk-forward comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--champion",    type=str, required=True,
                   choices=list(_NAMED_CONFIGS), help="Named champion config")
    p.add_argument("--challenger",  type=str, required=True,
                   choices=list(_NAMED_CONFIGS), help="Named challenger config")
    p.add_argument("--start",       type=str, default="2022-04-01",
                   help="ISO date for the earliest train_start (default: 2022-04-01)")
    p.add_argument("--end",         type=str, default="2026-05-01",
                   help="ISO date for the latest test_end (default: 2026-05-01)")
    p.add_argument("--train-months",type=int, default=18)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--step-months", type=int, default=3)
    p.add_argument("--min-trades",  type=int, default=30,
                   help="Kill-switch: minimum total trades across all windows (default: 30)")
    return p.parse_args()


def _verdict_to_markdown(
    verdict_obj,
    kill_switch: dict,
    champ_agg:   dict,
    chal_agg:    dict,
    champ_name:  str,
    chal_name:   str,
    args:        argparse.Namespace,
) -> str:
    """Render a markdown report from a ComparatorVerdict."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    v = verdict_obj

    kill_line = (
        f"> **KILL-SWITCH TRIGGERED** — only {kill_switch['total_trades']} trades "
        f"(threshold {kill_switch['threshold']}). Challenger rejected regardless of stats.\n\n"
        if kill_switch["triggered"] else ""
    )

    return f"""# Champion-Challenger Audit — {champ_name} vs {chal_name}

*Generated: {now_str}*

**Verdict: {v.verdict}**

{kill_line}## Summary

| Metric | Champion ({champ_name}) | Challenger ({chal_name}) |
|--------|--------------------------|---------------------------|
| Mean PF | {v.champion_pf_mean:.4f} | {v.challenger_pf_mean:.4f} |
| Mean Calmar | {v.champion_calmar_mean:.4f} | {v.challenger_calmar_mean:.4f} |

## Paired Statistical Tests

| Test | t-stat | p-value | Cohen's d |
|------|--------|---------|-----------|
| Profit Factor | {v.pf_t_stat:.4f} | {v.pf_p_value:.4f} | {v.pf_cohen_d:.4f} |
| Calmar Ratio  | {v.calmar_t_stat:.4f} | {v.calmar_p_value:.4f} | {v.calmar_cohen_d:.4f} |

*Positive Cohen's d → challenger outperforms champion.*

## Per-Window Win/Loss/Tie (Calmar)

| Wins (challenger) | Losses (challenger) | Ties |
|-------------------|---------------------|------|
| {v.wins} | {v.losses} | {v.ties} |

## Kill-Switch

| Triggered | Total Trades | Threshold |
|-----------|-------------|-----------|
| {kill_switch['triggered']} | {kill_switch['total_trades']} | {kill_switch['threshold']} |

## Acceptance Criteria (design D9)

- ADOPT: Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins ≥ 60% of windows
- REJECT: challenger loses ≥ 60% of windows OR kill-switch triggered
- INCONCLUSIVE: all other cases

## Config Parameters

- Date range: {args.start} → {args.end}
- Train months: {args.train_months} | Test months: {args.test_months} | Step months: {args.step_months}
- Champion: {champ_name} | Challenger: {chal_name}
"""


def main() -> int:
    args = _parse_args()

    if args.champion == args.challenger:
        log.error("champion and challenger must be different configs")
        return 1

    champ_bt_cfg = _NAMED_CONFIGS[args.champion]
    chal_bt_cfg  = _NAMED_CONFIGS[args.challenger]

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    log.info("Champion-challenger: %s vs %s, range=%s→%s",
             args.champion, args.challenger, start_dt.date(), end_dt.date())

    # ── Fetch / load cached klines ──────────────────────────────────────────
    log.info("Loading klines (4h + 1d + 1w) for BTCUSDT and ETHUSDT …")
    dfs        = {sym: fetch_and_cache(sym, "4h", start_dt, end_dt)
                  for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_bias   = {sym: fetch_and_cache(sym, "1d", start_dt, end_dt)
                  for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_weekly = {sym: fetch_and_cache(sym, "1w", start_dt, end_dt)
                  for sym in ("BTCUSDT", "ETHUSDT")}

    wf_cfg = WalkForwardConfig(
        start_date   = start_dt,
        end_date     = end_dt,
        train_months = args.train_months,
        test_months  = args.test_months,
        step_months  = args.step_months,
        symbols      = ("BTCUSDT", "ETHUSDT"),
        timeframe    = "4h",
    )

    bt_configs = {args.champion: champ_bt_cfg, args.challenger: chal_bt_cfg}

    # ── Run all windows for both configs ────────────────────────────────────
    log.info("Running walk-forward for both configs …")
    all_results = run_all(
        wf_config        = wf_cfg,
        backtest_configs = bt_configs,
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_weekly,
        progress_cb      = log.info,
    )
    log.info("Collected %d window results", len(all_results))

    champ_results = sorted(
        [r for r in all_results if r.config_name == args.champion],
        key=lambda r: r.window.index,
    )
    chal_results  = sorted(
        [r for r in all_results if r.config_name == args.challenger],
        key=lambda r: r.window.index,
    )

    if not champ_results or not chal_results:
        log.error("No results for one or both configs — check date range / kline cache")
        return 1

    # ── Kill-switch evaluation ───────────────────────────────────────────────
    kill_switch = evaluate_adx_kill_switch(chal_results, min_trades=args.min_trades)
    if kill_switch["triggered"]:
        log.critical(
            "ADX kill-switch TRIGGERED: challenger produced only %d total trades "
            "(threshold=%d). Verdict → REJECT.",
            kill_switch["total_trades"], kill_switch["threshold"],
        )

    # ── Champion-challenger comparison ───────────────────────────────────────
    log.info("Running champion-challenger statistical comparison …")
    try:
        verdict = champion_vs_challenger(champ_results, chal_results)
    except ValueError as exc:
        log.error("champion_vs_challenger failed: %s", exc)
        return 1

    # Override verdict to REJECT when kill-switch triggered
    final_verdict_str = "REJECT" if kill_switch["triggered"] else verdict.verdict

    log.info(
        "VERDICT: %s | Calmar p=%.4f d=%.4f | wins=%d losses=%d ties=%d",
        final_verdict_str,
        verdict.calmar_p_value,
        verdict.calmar_cohen_d,
        verdict.wins, verdict.losses, verdict.ties,
    )

    # ── Aggregates per config ────────────────────────────────────────────────
    champ_agg = aggregate_metrics(champ_results)
    chal_agg  = aggregate_metrics(chal_results)

    # ── Persist raw JSON + markdown report ──────────────────────────────────
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    iso_str  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")

    out_data_dir = Path("data/audits")
    out_docs_dir = Path("docs/audits")
    out_data_dir.mkdir(parents=True, exist_ok=True)
    out_docs_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_data_dir / f"CC_{date_str}.json"
    md_path   = out_docs_dir / f"CC_{date_str}.md"

    payload = {
        "champion":       args.champion,
        "challenger":     args.challenger,
        "args":           vars(args),
        "verdict":        {
            "champion_pf_mean":       verdict.champion_pf_mean,
            "challenger_pf_mean":     verdict.challenger_pf_mean,
            "champion_calmar_mean":   verdict.champion_calmar_mean,
            "challenger_calmar_mean": verdict.challenger_calmar_mean,
            "pf_t_stat":              verdict.pf_t_stat,
            "pf_p_value":             verdict.pf_p_value,
            "calmar_t_stat":          verdict.calmar_t_stat,
            "calmar_p_value":         verdict.calmar_p_value,
            "pf_cohen_d":             verdict.pf_cohen_d,
            "calmar_cohen_d":         verdict.calmar_cohen_d,
            "wins":                   verdict.wins,
            "losses":                 verdict.losses,
            "ties":                   verdict.ties,
            "verdict":                final_verdict_str,
        },
        "kill_switch":    kill_switch,
        "champion_agg":   champ_agg,
        "challenger_agg": chal_agg,
        "champion_results":   _to_jsonable(champ_results),
        "challenger_results": _to_jsonable(chal_results),
        "generated":      iso_str,
    }

    json_path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("Raw results → %s", json_path)

    md_content = _verdict_to_markdown(
        verdict, kill_switch, champ_agg, chal_agg,
        args.champion, args.challenger, args,
    )
    md_path.write_text(md_content)
    log.info("Report → %s", md_path)

    # ── Engram persistence (best-effort; file-based fallback already done above) ──
    try:
        from mcp_plugin_engram_engram import mem_save  # type: ignore[import]
        mem_save(
            title=f"CC audit {args.champion} vs {args.challenger} — {date_str}",
            content=(
                f"**What**: Champion-challenger comparison {args.champion} vs {args.challenger}\n"
                f"**Why**: Phase 3 acceptance gate for win-rate-uplift-2026-05\n"
                f"**Where**: {md_path}, {json_path}\n"
                f"**Learned**: verdict={final_verdict_str}, "
                f"calmar_p={verdict.calmar_p_value:.4f}, "
                f"d={verdict.calmar_cohen_d:.4f}, "
                f"wins={verdict.wins}/{verdict.wins + verdict.losses + verdict.ties}"
            ),
            topic_key=f"sdd/win-rate-uplift-2026-05/cc-report",
            type="architecture",
        )
        log.info("Verdict persisted to engram (topic: sdd/win-rate-uplift-2026-05/cc-report)")
    except Exception as exc:  # noqa: BLE001
        log.debug("Engram persistence skipped (not available in this context): %s", exc)

    log.info("Done. Verdict: %s", final_verdict_str)
    return _EXIT_CODES.get(final_verdict_str, 2)


if __name__ == "__main__":
    sys.exit(main())
