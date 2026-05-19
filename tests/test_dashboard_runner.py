"""T09 — Failing tests for dashboard backtest_runner fallback values.

Verifies that dashboard fallbacks match the live-seeded values per REQ-PARITY-2/3/4.
TDD: these tests must FAIL before T10 implementation.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

# The runtime config lookups moved from `dashboard/sections/backtest_runner.py`
# (with parameter name `cfg_rt`) to `bot/backtest/portfolio_runner.py` (with
# parameter name `runtime_cfg`) as part of the dashboard/runner extraction.
# The Streamlit wrapper now delegates all real work to that pure module, so the
# AST checks below scan it directly. `tests/test_parity_runtime.py` runs the
# same checks at runtime — these AST checks remain as a fast literal guard.
RUNNER_PATH = (
    pathlib.Path(__file__).parent.parent / "bot" / "backtest" / "portfolio_runner.py"
)

# Names accepted as the runtime-config mapping argument. Listed in priority
# order — we accept both for backward compatibility with any code that still
# uses the old `cfg_rt` name.
_RT_PARAM_NAMES = ("runtime_cfg", "cfg_rt")


def _read_source() -> str:
    return RUNNER_PATH.read_text()


def _find_cfg_rt_defaults(source: str) -> dict[str, object]:
    """Parse the source with AST and collect fallback values from
    `<rt>.get(key, DEFAULT)` calls, where `<rt>` is one of `_RT_PARAM_NAMES`.
    """
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id in _RT_PARAM_NAMES):
            continue
        if len(node.args) < 2:
            continue
        key_node = node.args[0]
        default_node = node.args[1]
        if not isinstance(key_node, ast.Constant):
            continue
        key = key_node.value
        if isinstance(default_node, ast.Constant):
            defaults[key] = default_node.value
    return defaults


class TestBacktestRunnerFallbacks:
    def test_backtest_runner_fallback_values(self) -> None:
        """ema_tp_mult fallback must be 5.0 (not 4.5) and ema_vol_mult must be 1.5 (not 2.0)."""
        source = _read_source()
        defaults = _find_cfg_rt_defaults(source)

        assert "ema_tp_mult" in defaults, "cfg_rt.get('ema_tp_mult', ...) not found in backtest_runner.py"
        assert defaults["ema_tp_mult"] == 5.0, (
            f"ema_tp_mult fallback must be 5.0 (live seed), got {defaults['ema_tp_mult']!r}"
        )

        assert "ema_vol_mult" in defaults, "cfg_rt.get('ema_vol_mult', ...) not found in backtest_runner.py"
        assert defaults["ema_vol_mult"] == 1.5, (
            f"ema_vol_mult fallback must be 1.5 (live seed), got {defaults['ema_vol_mult']!r}"
        )

    def test_backtest_runner_bar_dir_default(self) -> None:
        """ema_bar_dir fallback must be 'true' (not 'false') per REQ-PARITY-3."""
        source = _read_source()
        defaults = _find_cfg_rt_defaults(source)

        assert "ema_bar_dir" in defaults, "cfg_rt.get('ema_bar_dir', ...) not found in backtest_runner.py"
        assert defaults["ema_bar_dir"] == "true", (
            f"ema_bar_dir fallback must be 'true', got {defaults['ema_bar_dir']!r}"
        )

    def test_fee_default_reads_db_seed(self) -> None:
        """Fee default must NOT be hardcoded 0.07.

        Per REQ-PARITY-4: the hardcoded 0.07 fee default must be removed and replaced
        with a DB-seed-driven default. The literal 0.07 must not appear as a fallback.
        """
        source = _read_source()

        # The old hardcoded value was: value=0.07
        # We check that 0.07 does NOT appear as a simple float fallback in number_input
        # Instead, it should read from cfg_rt.get("backtest_cost_per_side", 0.001)
        tree = ast.parse(source)

        # Check that 0.07 appears as default value for cost_pct (old pattern to eliminate)
        # We look for number_input calls with value=0.07
        hardcoded_07_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "number_input":
                    for kw in node.keywords:
                        if kw.arg == "value" and isinstance(kw.value, ast.Constant):
                            if kw.value.value == 0.07:
                                hardcoded_07_found = True

        assert not hardcoded_07_found, (
            "Hardcoded value=0.07 found in number_input — must be replaced with DB-seed-driven default"
        )
