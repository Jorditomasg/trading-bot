"""Run C3_BOTH challenger audit and generate PHASE2_SUMMARY.

Reuses existing JSON results for ADX25, ADX30, EMA200 (already computed).
Only runs C3_BOTH from scratch.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/audit/run_c3_both_and_summary.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from bot.audit.comparison import champion_vs_challenger
from bot.audit.kill_switch import evaluate_adx_kill_switch
from bot.audit.walk_forward import WalkForwardConfig, aggregate_metrics, run_all
from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from scripts.audit.run_walk_forward import CONFIG_C3_LIVE

logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(message)s")
for _noisy in ("bot.regime.detector", "bot.bias.filter", "bot.strategy.ema_crossover",
               "bot.backtest.engine", "bot.backtest.portfolio_engine"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

log = logging.getLogger("phase2-both")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
log.addHandler(_handler)

_START = "2022-04-01"
_END   = "2026-05-01"
DATE_STR = "2026-05-17"

OUT_DIR  = Path("docs/audits")
DATA_DIR = Path("data/audits")


def _load_existing(label: str) -> dict | None:
    path = DATA_DIR / f"CC_{DATE_STR}_{label}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def _write_report(label: str, data: dict, args_info: dict) -> Path:
    v = data["verdict"]
    k = data["kill_switch"]
    kill_line = (
        f"> **KILL-SWITCH TRIGGERED** — only {k['total_trades']} trades "
        f"(threshold {k['threshold']}). Challenger rejected regardless of stats.\n\n"
        if k["triggered"] else ""
    )
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    final_verdict = v["verdict"]

    md = f"""# Phase 2 Acceptance Audit — C3 vs {label}

*Generated: {now_str}*

**Verdict: {final_verdict}**

{kill_line}## Summary

| Metric | Champion (C3_LIVE) | Challenger ({label}) |
|--------|---------------------|----------------------|
| Mean PF | {v['champion_pf_mean']:.4f} | {v['challenger_pf_mean']:.4f} |
| Mean Calmar | {v['champion_calmar_mean']:.4f} | {v['challenger_calmar_mean']:.4f} |
| Total Trades (chal) | — | {k['total_trades']} |

## Paired Statistical Tests

| Test | t-stat | p-value | Cohen's d |
|------|--------|---------|-----------|
| Profit Factor | {v['pf_t_stat']:.4f} | {v['pf_p_value']:.4f} | {v['pf_cohen_d']:.4f} |
| Calmar Ratio  | {v['calmar_t_stat']:.4f} | {v['calmar_p_value']:.4f} | {v['calmar_cohen_d']:.4f} |

*Positive Cohen's d → challenger outperforms champion.*

## Per-Window Win/Loss/Tie (Calmar)

| Wins (challenger) | Losses (challenger) | Ties |
|-------------------|---------------------|------|
| {v['wins']} | {v['losses']} | {v['ties']} |

## Kill-Switch

| Triggered | Total Trades | Threshold |
|-----------|-------------|-----------|
| {k['triggered']} | {k['total_trades']} | {k['threshold']} |

## Acceptance Criteria (design D9)

- ADOPT: Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins ≥ 60% of windows
- REJECT: challenger loses ≥ 60% of windows OR kill-switch triggered
- INCONCLUSIVE: all other cases

## Config

- Date range: {args_info['start']} → {args_info['end']}
- Train/Test/Step months: {args_info['train']}/{args_info['test']}/{args_info['step']}
- Champion: C3_LIVE | Challenger: {label}
"""
    path = OUT_DIR / f"CC_{DATE_STR}_{label}.md"
    path.write_text(md)
    return path


def _write_summary(all_data: dict[str, dict]) -> Path:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    labels = ["C3_ADX25", "C3_ADX30", "C3_EMA200", "C3_BOTH"]

    summary_lines = [
        "| Challenger | Verdict | Calmar p | Cohen's d | Wins/Losses | Calmar (champ/chal) | Kill-Switch |",
        "|------------|---------|----------|-----------|-------------|---------------------|-------------|"
    ]

    adopted = []
    rejected = []
    inconclusive = []

    for label in labels:
        d = all_data.get(label)
        if d is None:
            continue
        v = d["verdict"]
        k = d["kill_switch"]
        verdict = v["verdict"]
        if verdict == "ADOPT":
            adopted.append(label)
        elif verdict == "REJECT":
            rejected.append(label)
        else:
            inconclusive.append(label)

        summary_lines.append(
            f"| {label} | **{verdict}** | {v['calmar_p_value']:.4f} | "
            f"{v['calmar_cohen_d']:+.4f} | {v['wins']}/{v['losses']} | "
            f"{v['champion_calmar_mean']:.2f} / {v['challenger_calmar_mean']:.2f} | "
            f"{'YES' if k['triggered'] else 'no'} ({k['total_trades']}) |"
        )

    if adopted:
        overall = "ADOPT"
        recommendation = f"Recommended: enable {adopted[0]} for next change. See per-challenger reports."
    elif not inconclusive:
        overall = "REJECT"
        recommendation = "All Phase 2 filters underperform C3_LIVE. Keep defaults OFF."
    else:
        overall = "INCONCLUSIVE"
        recommendation = "Mixed verdicts. ADX gates degrade Calmar. EMA200 neutral. Keep defaults OFF pending longer test."

    md = f"""# Phase 2 Acceptance Audit — Summary

*Generated: {now_str}*

Champion: **C3_LIVE** | Date range: {_START} → {_END}

## Results

{chr(10).join(summary_lines)}

## Acceptance Criteria (design D9)

- **ADOPT**: Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins ≥ 60% of windows
- **REJECT**: challenger loses ≥ 60% of windows OR kill-switch triggered
- **INCONCLUSIVE**: all other cases

## Overall Verdict: {overall}

{recommendation}
"""
    path = OUT_DIR / f"CC_{DATE_STR}_PHASE2_SUMMARY.md"
    path.write_text(md)
    return path


def main() -> int:
    start_dt = datetime.fromisoformat(_START).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(_END).replace(tzinfo=timezone.utc)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    args_info = {"start": _START, "end": _END, "train": 18, "test": 3, "step": 3}

    # Load existing results
    all_data: dict[str, dict] = {}
    for label in ["C3_ADX25", "C3_ADX30", "C3_EMA200"]:
        d = _load_existing(label)
        if d:
            all_data[label] = d
            log.info("Loaded existing: %s → %s", label, d["verdict"]["verdict"])
        else:
            log.warning("Missing existing data for %s", label)

    # Run C3_BOTH from scratch
    log.info("Loading klines for C3_BOTH run …")
    symbols  = ("BTCUSDT", "ETHUSDT")
    dfs      = {sym: fetch_and_cache(sym, "4h", start_dt, end_dt) for sym in symbols}
    dfs_bias = {sym: fetch_and_cache(sym, "1d", start_dt, end_dt) for sym in symbols}
    dfs_wk   = {sym: fetch_and_cache(sym, "1w", start_dt, end_dt) for sym in symbols}
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

    # Champion walk-forward
    log.info("Running champion (C3_LIVE) …")
    import dataclasses
    c3_both_cfg = dataclasses.replace(CONFIG_C3_LIVE, ema_min_entry_adx=25.0, ema_require_ema200_alignment=True)

    champ_results_all = run_all(
        wf_config        = wf_cfg,
        backtest_configs = {"C3_LIVE": CONFIG_C3_LIVE},
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_wk,
        progress_cb      = log.info,
    )
    champ_results = sorted(
        [r for r in champ_results_all if r.config_name == "C3_LIVE"],
        key=lambda r: r.window.index,
    )
    log.info("Champion windows: %d", len(champ_results))

    # C3_BOTH walk-forward
    log.info("Running C3_BOTH (ADX=25 + EMA200) …")
    both_results_all = run_all(
        wf_config        = wf_cfg,
        backtest_configs = {"C3_BOTH": c3_both_cfg},
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_wk,
        progress_cb      = log.info,
    )
    both_results = sorted(
        [r for r in both_results_all if r.config_name == "C3_BOTH"],
        key=lambda r: r.window.index,
    )
    log.info("C3_BOTH windows: %d", len(both_results))

    if not both_results:
        log.error("No C3_BOTH results — aborting")
        return 2

    kill_switch = evaluate_adx_kill_switch(both_results, min_trades=30)
    verdict = champion_vs_challenger(champ_results, both_results)
    final_verdict = "REJECT" if kill_switch["triggered"] else verdict.verdict

    log.info(
        "C3_BOTH → %s | Calmar p=%.4f d=%.4f wins=%d losses=%d trades=%d",
        final_verdict, verdict.calmar_p_value, verdict.calmar_cohen_d,
        verdict.wins, verdict.losses, kill_switch["total_trades"],
    )

    data_both = {
        "champion": "C3_LIVE",
        "challenger": "C3_BOTH",
        "verdict": {
            "champion_pf_mean":        verdict.champion_pf_mean,
            "challenger_pf_mean":      verdict.challenger_pf_mean,
            "champion_calmar_mean":    verdict.champion_calmar_mean,
            "challenger_calmar_mean":  verdict.challenger_calmar_mean,
            "pf_t_stat":               verdict.pf_t_stat,
            "pf_p_value":              verdict.pf_p_value,
            "calmar_t_stat":           verdict.calmar_t_stat,
            "calmar_p_value":          verdict.calmar_p_value,
            "pf_cohen_d":              verdict.pf_cohen_d,
            "calmar_cohen_d":          verdict.calmar_cohen_d,
            "wins":                    verdict.wins,
            "losses":                  verdict.losses,
            "ties":                    verdict.ties,
            "verdict":                 final_verdict,
        },
        "kill_switch":  kill_switch,
        "generated":    datetime.now(timezone.utc).isoformat(),
    }

    json_path = DATA_DIR / f"CC_{DATE_STR}_C3_BOTH.json"
    json_path.write_text(json.dumps(data_both, indent=2, default=str))
    log.info("JSON → %s", json_path)

    all_data["C3_BOTH"] = data_both

    # Write C3_BOTH markdown report
    md_path = _write_report("C3_BOTH", data_both, args_info)
    log.info("Report → %s", md_path)

    # Write overall summary
    summary_path = _write_summary(all_data)
    log.info("Summary → %s", summary_path)

    # Print final table
    log.info("=== Phase 2 Acceptance Summary ===")
    for label in ["C3_ADX25", "C3_ADX30", "C3_EMA200", "C3_BOTH"]:
        d = all_data.get(label)
        if d:
            v = d["verdict"]
            log.info(
                "  %-12s → %-14s  calmar p=%.4f d=%+.4f  wins=%d/%d  calmar=%.2f/%.2f",
                label, v["verdict"],
                v["calmar_p_value"], v["calmar_cohen_d"],
                v["wins"], v["losses"],
                v["champion_calmar_mean"], v["challenger_calmar_mean"],
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
