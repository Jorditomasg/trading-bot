"""Phase 2 acceptance audit — champion (C3_LIVE) vs 4 challengers.

Challengers:
  C3_ADX25   — C3_LIVE + min_entry_adx=25 (ADX gate)
  C3_ADX30   — C3_LIVE + min_entry_adx=30 (stricter ADX gate)
  C3_EMA200  — C3_LIVE + require_ema200_alignment=True
  C3_BOTH    — C3_LIVE + min_entry_adx=25 + require_ema200_alignment=True

Uses the existing BTCUSDT+ETHUSDT 4h parquet cache — no network calls.

Exit codes:
  0 → at least one challenger ADOPT
  1 → all challengers REJECT
  2 → INCONCLUSIVE (mixed)

Usage:
    PYTHONPATH=. .venv/bin/python scripts/audit/run_phase2_acceptance.py
"""
from __future__ import annotations

import json
import logging
import sys
from copy import copy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.audit.comparison import champion_vs_challenger
from bot.audit.kill_switch import evaluate_adx_kill_switch
from bot.audit.walk_forward import WalkForwardConfig, aggregate_metrics, run_all
from bot.backtest.cache import fetch_and_cache
from scripts.audit.run_walk_forward import CONFIG_C3_LIVE, _to_jsonable

logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(message)s")
# Suppress per-bar INFO noise from regime detector, bias filter, and strategy
for _noisy in ("bot.regime.detector", "bot.bias.filter", "bot.strategy.ema_crossover",
               "bot.backtest.engine", "bot.backtest.portfolio_engine"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# Raise our own logger to INFO so progress is visible
log = logging.getLogger("phase2-audit")
log.setLevel(logging.INFO)
# Add a handler so our INFO messages print even with WARNING root level
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
log.addHandler(_handler)

_START = "2022-04-01"
_END   = "2026-05-01"

# ── Define Phase 2 challengers (all based on C3_LIVE) ────────────────────────

def _make_challenger(label: str, **overrides):
    """Return (label, BacktestConfig) by copying C3_LIVE and applying overrides."""
    import dataclasses
    base = dataclasses.replace(CONFIG_C3_LIVE, **overrides)
    return label, base


CHALLENGERS = [
    _make_challenger("C3_ADX25",  ema_min_entry_adx=25.0),
    _make_challenger("C3_ADX30",  ema_min_entry_adx=30.0),
    _make_challenger("C3_EMA200", ema_require_ema200_alignment=True),
    _make_challenger("C3_BOTH",   ema_min_entry_adx=25.0, ema_require_ema200_alignment=True),
]


def _write_report(
    label: str,
    verdict,
    kill_switch: dict,
    champ_agg: dict,
    chal_agg: dict,
    final_verdict_str: str,
    args_info: dict,
    out_dir: Path,
    date_str: str,
) -> Path:
    """Write a markdown report for one challenger comparison."""
    v = verdict
    kill_line = (
        f"> **KILL-SWITCH TRIGGERED** — only {kill_switch['total_trades']} trades "
        f"(threshold {kill_switch['threshold']}). Challenger rejected regardless of stats.\n\n"
        if kill_switch["triggered"] else ""
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    md = f"""# Phase 2 Acceptance Audit — C3 vs {label}

*Generated: {now_str}*

**Verdict: {final_verdict_str}**

{kill_line}## Summary

| Metric | Champion (C3_LIVE) | Challenger ({label}) |
|--------|---------------------|----------------------|
| Mean PF | {v.champion_pf_mean:.4f} | {v.challenger_pf_mean:.4f} |
| Mean Calmar | {v.champion_calmar_mean:.4f} | {v.challenger_calmar_mean:.4f} |
| Total Trades (chal) | — | {kill_switch['total_trades']} |

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

## Config

- Date range: {args_info['start']} → {args_info['end']}
- Train/Test/Step months: {args_info['train']}/{args_info['test']}/{args_info['step']}
- Champion: C3_LIVE | Challenger: {label}
"""
    path = out_dir / f"CC_{date_str}_{label}.md"
    path.write_text(md)
    return path


def main() -> int:
    start_dt = datetime.fromisoformat(_START).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(_END).replace(tzinfo=timezone.utc)

    log.info("Phase 2 acceptance audit: C3_LIVE vs 4 challengers")
    log.info("Range: %s → %s", start_dt.date(), end_dt.date())

    # ── Load cached klines (no network calls) ──────────────────────────────────
    log.info("Loading klines from parquet cache …")
    symbols    = ("BTCUSDT", "ETHUSDT")
    dfs        = {sym: fetch_and_cache(sym, "4h",  start_dt, end_dt) for sym in symbols}
    dfs_bias   = {sym: fetch_and_cache(sym, "1d",  start_dt, end_dt) for sym in symbols}
    dfs_weekly = {sym: fetch_and_cache(sym, "1w",  start_dt, end_dt) for sym in symbols}
    log.info("Klines loaded: BTC=%d bars, ETH=%d bars", len(dfs["BTCUSDT"]), len(dfs["ETHUSDT"]))

    wf_cfg = WalkForwardConfig(
        start_date   = start_dt,
        end_date     = end_dt,
        train_months = 18,
        test_months  = 3,
        step_months  = 3,
        symbols      = symbols,
        timeframe    = "4h",
    )

    # ── Run champion once, then run each challenger ───────────────────────────
    log.info("Running champion (C3_LIVE) walk-forward …")
    champ_results_all = run_all(
        wf_config        = wf_cfg,
        backtest_configs = {"C3_LIVE": CONFIG_C3_LIVE},
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_weekly,
        progress_cb      = log.info,
    )
    champ_results = sorted(
        [r for r in champ_results_all if r.config_name == "C3_LIVE"],
        key=lambda r: r.window.index,
    )
    log.info("Champion windows: %d", len(champ_results))

    date_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir   = Path("docs/audits")
    data_dir  = Path("data/audits")
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    verdicts: dict[str, str] = {}
    summaries: list[dict] = []

    args_info = {"start": _START, "end": _END, "train": 18, "test": 3, "step": 3}

    for label, chal_cfg in CHALLENGERS:
        log.info("--- Running challenger: %s ---", label)
        chal_results_all = run_all(
            wf_config        = wf_cfg,
            backtest_configs = {label: chal_cfg},
            dfs              = dfs,
            dfs_bias         = dfs_bias,
            dfs_weekly       = dfs_weekly,
            progress_cb      = log.info,
        )
        chal_results = sorted(
            [r for r in chal_results_all if r.config_name == label],
            key=lambda r: r.window.index,
        )

        if not chal_results:
            log.error("No results for challenger %s — skipping", label)
            verdicts[label] = "ERROR"
            continue

        kill_switch = evaluate_adx_kill_switch(chal_results, min_trades=30)
        if kill_switch["triggered"]:
            log.critical(
                "Kill-switch TRIGGERED for %s: %d trades (threshold %d)",
                label, kill_switch["total_trades"], kill_switch["threshold"],
            )

        try:
            verdict = champion_vs_challenger(champ_results, chal_results)
        except ValueError as exc:
            log.error("champion_vs_challenger failed for %s: %s", label, exc)
            verdicts[label] = "ERROR"
            continue

        final_verdict_str = "REJECT" if kill_switch["triggered"] else verdict.verdict
        verdicts[label] = final_verdict_str

        log.info(
            "%s → %s | Calmar p=%.4f d=%.4f wins=%d losses=%d",
            label, final_verdict_str,
            verdict.calmar_p_value, verdict.calmar_cohen_d,
            verdict.wins, verdict.losses,
        )

        # Write per-challenger markdown report
        md_path = _write_report(
            label, verdict, kill_switch,
            aggregate_metrics(champ_results),
            aggregate_metrics(chal_results),
            final_verdict_str, args_info, out_dir, date_str,
        )
        log.info("Report → %s", md_path)

        # Save JSON
        json_path = data_dir / f"CC_{date_str}_{label}.json"
        json_path.write_text(json.dumps({
            "champion": "C3_LIVE",
            "challenger": label,
            "verdict": {
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
            "kill_switch": kill_switch,
            "generated":   datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
        log.info("JSON → %s", json_path)

        summaries.append({
            "label":       label,
            "verdict":     final_verdict_str,
            "calmar_p":    verdict.calmar_p_value,
            "calmar_d":    verdict.calmar_cohen_d,
            "wins":        verdict.wins,
            "losses":      verdict.losses,
            "champ_calmar": verdict.champion_calmar_mean,
            "chal_calmar":  verdict.challenger_calmar_mean,
            "kill_switch_triggered": kill_switch["triggered"],
            "total_trades": kill_switch["total_trades"],
        })

    # ── Overall summary ────────────────────────────────────────────────────────
    log.info("=== Phase 2 Acceptance Summary ===")
    for s in summaries:
        log.info(
            "  %-12s → %-14s  calmar p=%.4f d=%+.4f  wins=%d  trades=%d",
            s["label"], s["verdict"], s["calmar_p"], s["calmar_d"],
            s["wins"], s["total_trades"],
        )

    adopted = [v for v in verdicts.values() if v == "ADOPT"]
    rejected = [v for v in verdicts.values() if v == "REJECT"]

    if adopted:
        log.info("Result: at least one challenger ADOPT → consider enabling for next change")
        exit_code = 0
    elif rejected and len(rejected) == len(verdicts):
        log.info("Result: all challengers REJECT → keep defaults OFF")
        exit_code = 1
    else:
        log.info("Result: INCONCLUSIVE — mixed verdicts, review per-challenger reports")
        exit_code = 2

    # Write overall summary markdown
    summary_lines = ["| Challenger | Verdict | Calmar p | Cohen's d | Wins/Losses | Calmar (champ/chal) | Kill-Switch |",
                     "|------------|---------|----------|-----------|-------------|---------------------|-------------|"]
    for s in summaries:
        summary_lines.append(
            f"| {s['label']} | **{s['verdict']}** | {s['calmar_p']:.4f} | "
            f"{s['calmar_d']:+.4f} | {s['wins']}/{s['losses']} | "
            f"{s['champ_calmar']:.2f} / {s['chal_calmar']:.2f} | "
            f"{'YES' if s['kill_switch_triggered'] else 'no'} ({s['total_trades']}) |"
        )

    summary_md = f"""# Phase 2 Acceptance Audit — Summary

*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*

Champion: **C3_LIVE** | Date range: {_START} → {_END}

## Results

{chr(10).join(summary_lines)}

## Acceptance Criteria (design D9)

- **ADOPT**: Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins ≥ 60% of windows
- **REJECT**: challenger loses ≥ 60% of windows OR kill-switch triggered
- **INCONCLUSIVE**: all other cases

## Overall Verdict: {"ADOPT (best challenger)" if adopted else "REJECT" if len(rejected) == len(verdicts) else "INCONCLUSIVE"}

{"Recommended: enable the best ADOPT challenger. See per-challenger reports for details." if adopted else "All Phase 2 filters underperform C3_LIVE. Keep defaults OFF." if len(rejected) == len(verdicts) else "Review per-challenger reports before deciding."}
"""
    summary_path = out_dir / f"CC_{date_str}_PHASE2_SUMMARY.md"
    summary_path.write_text(summary_md)
    log.info("Summary → %s", summary_path)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
