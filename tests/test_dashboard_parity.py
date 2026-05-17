"""T11 — AST parity test: dashboard fallbacks must match seed literals.

Parses dashboard/sections/backtest_runner.py with AST; for each cfg_rt.get(key, FALLBACK)
call, asserts FALLBACK matches the corresponding value in _seed_optimized_defaults().

TDD: test is written here and becomes green only after BOTH T02 (seed) and T10 (dashboard)
have landed.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from bot.database.db import Database
from main import _seed_optimized_defaults

RUNNER_PATH = pathlib.Path(__file__).parent.parent / "dashboard" / "sections" / "backtest_runner.py"

# Keys whose fallback value in backtest_runner.py must match the seed
KEYS_TO_CHECK = {
    "ema_tp_mult",
    "ema_vol_mult",
    "ema_bar_dir",
    "momentum_neutral_band",  # hardcoded literal in BacktestConfig constructor
}


def _get_seed_values(tmp_path: pathlib.Path) -> dict[str, str]:
    """Return the full seed config from a fresh in-memory DB."""
    db = Database(str(tmp_path / "seed.db"))
    _seed_optimized_defaults(db)
    return db.get_runtime_config()


def _find_cfg_rt_defaults(source: str) -> dict[str, object]:
    """Parse source with AST; collect fallback values from cfg_rt.get(key, DEFAULT) calls."""
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "cfg_rt"):
            continue
        if len(node.args) < 2:
            continue
        key_node, default_node = node.args[0], node.args[1]
        if not isinstance(key_node, ast.Constant):
            continue
        if isinstance(default_node, ast.Constant):
            defaults[key_node.value] = default_node.value
    return defaults


class TestDashboardParityWithSeed:
    def test_backtest_runner_fallbacks_match_seed_defaults(self, tmp_path) -> None:
        """All cfg_rt.get() fallbacks in backtest_runner.py must match seed values.

        Specifically checks: ema_tp_mult, ema_vol_mult, ema_bar_dir.
        momentum_neutral_band is a direct literal in BacktestConfig, not a cfg_rt.get call,
        so we verify its presence in the source instead.
        """
        source = RUNNER_PATH.read_text()
        seed_cfg = _get_seed_values(tmp_path)
        runner_defaults = _find_cfg_rt_defaults(source)

        mismatches = []
        check_keys = ["ema_tp_mult", "ema_vol_mult", "ema_bar_dir"]
        for key in check_keys:
            if key not in runner_defaults:
                mismatches.append(f"Key {key!r} not found in backtest_runner.py cfg_rt.get() calls")
                continue
            if key not in seed_cfg:
                mismatches.append(f"Key {key!r} not found in seed defaults")
                continue

            runner_val = runner_defaults[key]
            seed_val   = seed_cfg[key]

            # Normalize: seed stores strings; runner may store float or string
            # Convert runner fallback to string for comparison
            if isinstance(runner_val, float):
                runner_val_str = str(runner_val)
            elif isinstance(runner_val, bool):
                runner_val_str = "true" if runner_val else "false"
            else:
                runner_val_str = str(runner_val)

            if runner_val_str != seed_val:
                mismatches.append(
                    f"Mismatch for {key!r}: runner fallback={runner_val!r} "
                    f"(→ '{runner_val_str}'), seed='{seed_val}'"
                )

        # Also verify momentum_neutral_band=0.08 appears as literal in the BacktestConfig block
        assert "0.08" in source, "momentum_neutral_band literal 0.08 not found in backtest_runner.py"

        assert not mismatches, "Fallback/seed mismatches:\n" + "\n".join(mismatches)
