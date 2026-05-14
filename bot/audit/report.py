"""Markdown + Plotly HTML report writer for walk-forward audit results."""
from __future__ import annotations

import math
from pathlib import Path

import plotly.graph_objects as go


def _fmt(v: object, digits: int = 2) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        return f"{v:.{digits}f}"
    return str(v)


def _verdict_section(name: str, summary: dict) -> str:
    v        = summary["verdict"]
    bucket   = v["bucket"]
    emoji    = {"GO": "✅", "WATCH": "⚠️", "NO-GO": "❌"}[bucket]
    rows = []
    for c in v["checks"]:
        rows.append(
            f"| {c['name']} | {_fmt(c['value'], 3)} | {_fmt(c['threshold'])} | "
            f"{'✅' if c['passed'] else '❌'} | {_fmt(c['margin']*100, 1)}% |"
        )
    return (
        f"### {name}: {emoji} **{bucket}**\n\n"
        f"Windows tested: **{summary['n_windows']}**\n\n"
        "| Check | Value | Threshold | Passed | Margin |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )


def _aggregate_table(name: str, summary: dict) -> str:
    agg = summary["aggregate"]
    rows = []
    for metric in ("pf", "calmar", "sharpe", "win_rate_pct", "max_drawdown_pct", "final_pnl_pct"):
        m = agg[metric]
        ci_lo, ci_hi = m.get("ci95", (float("nan"), float("nan")))
        rows.append(
            f"| {metric} | {_fmt(m['mean'])} | {_fmt(m['median'])} | "
            f"{_fmt(m['std'])} | [{_fmt(ci_lo)}, {_fmt(ci_hi)}] | "
            f"{_fmt(m.get('worst', float('nan')))} |"
        )
    return (
        f"#### {name} — aggregate metrics\n\n"
        "| Metric | Mean | Median | Std | 95% CI | Worst |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
        + f"\n**Hit rate (PF > 1.0)**: {_fmt(agg['pf']['hit_rate']*100, 1)}%  ·  "
          f"**Sparsity (windows < 5 trades)**: "
          f"{agg['sparsity']['windows_lt_5_trades']} ({_fmt(agg['sparsity']['pct']*100, 1)}%)\n"
    )


def _per_window_table(payload: dict) -> str:
    rows = []
    for r in payload["results"]:
        rows.append(
            f"| {r['config_name']} | {r['window']['index']} | "
            f"{r['window']['test_start'][:10]} | {r['window']['test_end'][:10]} | "
            f"{_fmt(r['pf'])} | {_fmt(r['calmar'])} | {_fmt(r['sharpe'])} | "
            f"{_fmt(r['win_rate_pct'])}% | {_fmt(r['max_drawdown_pct'])}% | "
            f"{r['total_trades']} | {_fmt(r['final_pnl_pct'])}% |"
        )
    return (
        "## Per-window results\n\n"
        "| Cfg | # | Test start | Test end | PF | Calmar | Sharpe | WR | DD | n | PnL |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )


def _comparison_section(payload: dict) -> str:
    cmp = payload.get("comparison")
    if not cmp:
        return ""
    delta = cmp["delta_mean"]
    p     = cmp["p"]
    d     = cmp["cohens_d"]
    if abs(delta) >= 0.10 and p < 0.05 and abs(d) > 0.3:
        verdict = "Prefer **C2 (prod actual)**" if delta > 0 else "Prefer **C1 (baseline)**"
    elif p > 0.10:
        verdict = "**Equivalent** — no significant difference"
    else:
        verdict = "**Inconclusive** — significant but not practically meaningful"
    return (
        "## Comparison: C2 vs C1\n\n"
        f"- Δ mean PF (C2 − C1): **{_fmt(delta, 4)}**\n"
        f"- Paired t = {_fmt(cmp['t'], 3)}, df = {cmp['df']}, p = {_fmt(p, 4)}\n"
        f"- Cohen's d = {_fmt(d, 3)}\n"
        f"- N paired windows = {cmp['n']}\n\n"
        f"**Verdict**: {verdict}\n"
    )


def _equity_chart(payload: dict, output_dir: Path) -> Path | None:
    """Write per-config PF over windows as Plotly HTML. Returns relative path."""
    fig = go.Figure()
    for cfg_name in {r["config_name"] for r in payload["results"]}:
        rows = sorted(
            [r for r in payload["results"] if r["config_name"] == cfg_name],
            key=lambda r: r["window"]["index"],
        )
        fig.add_trace(go.Scatter(
            x=[r["window"]["test_start"][:10] for r in rows],
            y=[r["pf"] for r in rows],
            mode="lines+markers",
            name=cfg_name,
        ))
    fig.add_hline(y=1.0, line_dash="dot", line_color="#888", annotation_text="break-even")
    fig.update_layout(
        title="PF per test window — C1 vs C2",
        xaxis_title="Test window start",
        yaxis_title="Profit Factor",
        height=420,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "pf_over_windows.html"
    fig.write_html(html_path, include_plotlyjs="cdn", full_html=True)
    return html_path


def write_markdown_report(payload: dict, md_path: Path) -> None:
    """Write the full audit report to md_path. Charts go in a sibling folder."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    charts_dir = md_path.parent / f"{md_path.stem}_charts"
    chart_path = _equity_chart(payload, charts_dir)

    parts: list[str] = []
    parts.append("# Walk-Forward Validation Audit — Sub-Project A\n")
    parts.append(f"_Generated: {payload['generated']}_\n\n")
    parts.append("**Spec**: `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md`\n\n")

    # TL;DR
    summaries = payload.get("summaries", {})
    cmp       = payload.get("comparison")
    tldr_bits: list[str] = []
    for name, s in summaries.items():
        tldr_bits.append(f"{name}: **{s['verdict']['bucket']}**")
    if cmp:
        tldr_bits.append(f"Δ PF (C2−C1) = {_fmt(cmp['delta_mean'], 3)} (p = {_fmt(cmp['p'], 3)})")
    parts.append("## TL;DR\n\n" + "  ·  ".join(tldr_bits) + "\n\n")

    # Per-config verdicts
    parts.append("## Verdicts\n\n")
    for name, s in summaries.items():
        parts.append(_verdict_section(name, s))
        parts.append("\n")

    # Aggregate tables
    for name, s in summaries.items():
        parts.append(_aggregate_table(name, s))
        parts.append("\n")

    # Comparison
    parts.append(_comparison_section(payload))
    parts.append("\n")

    # Per-window detail
    parts.append(_per_window_table(payload))
    parts.append("\n")

    # Chart link
    if chart_path:
        parts.append(f"## Charts\n\n[PF over windows]({chart_path.relative_to(md_path.parent)})\n\n")

    # Reproduction footer
    args = payload["args"]
    parts.append("## Reproduction\n\n```bash\n")
    parts.append(
        "PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py \\\n"
        f"  --start {args['start']} --end {args['end']} \\\n"
        f"  --train-months {args['train_months']} "
        f"--test-months {args['test_months']} --step-months {args['step_months']}\n"
    )
    parts.append("```\n")

    md_path.write_text("".join(parts), encoding="utf-8")
