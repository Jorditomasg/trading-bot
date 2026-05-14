"""Walk-forward validation framework.

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from bot.audit.walk_forward import (
    WalkForwardConfig,
    Window,
    WindowResult,
    aggregate_metrics,
    extract_window_metrics,
    run_all,
    run_window,
    split_windows,
)
from bot.audit.verdict import VerdictThresholds, evaluate_verdict
from bot.audit.comparison import (
    cohens_d_paired,
    compare_configs,
    paired_t_test,
)
from bot.audit.report import write_markdown_report

__all__ = [
    "WalkForwardConfig",
    "Window",
    "WindowResult",
    "VerdictThresholds",
    "aggregate_metrics",
    "cohens_d_paired",
    "compare_configs",
    "evaluate_verdict",
    "extract_window_metrics",
    "paired_t_test",
    "run_all",
    "run_window",
    "split_windows",
    "write_markdown_report",
]
