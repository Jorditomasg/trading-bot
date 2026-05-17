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

        Specifically checks: ema_tp_mult, ema_vol_mult, ema_bar_dir, ema_min_atr,
        ema_momentum_req. momentum_neutral_band is a direct literal in BacktestConfig,
        not a cfg_rt.get call, so we verify its presence in the source instead.
        """
        source = RUNNER_PATH.read_text()
        seed_cfg = _get_seed_values(tmp_path)
        runner_defaults = _find_cfg_rt_defaults(source)

        mismatches = []
        check_keys = [
            "ema_tp_mult",
            "ema_vol_mult",
            "ema_bar_dir",
            "ema_min_atr",       # Phase 1 follow-up: was missing (fallback 0.0 vs seed 0.005)
            "ema_momentum_req",  # Phase 1 follow-up: dashboard read "ema_momentum" (typo)
        ]
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


class TestDashboardFilterWarmup:
    """Phase 1 follow-up #2 (warmup parity): BiasFilter and MomentumFilter need
    enough historical bars at backtest_start to produce live-equivalent results.
    Without warmup, BiasFilter falls into passthrough and MomentumFilter into
    fail-open BULLISH for the first weeks → 5 extra low-quality trades on the
    user's BTC 6mo case (16/PF=0.90 vs the correct 11/PF=1.79).

    These AST checks guard against accidental regression of the warmup fix.
    """

    def test_warmup_constants_defined(self) -> None:
        """BIAS_WARMUP and MOMENTUM_WARMUP must be defined with sensible values."""
        source = RUNNER_PATH.read_text()
        assert "BIAS_WARMUP" in source, (
            "BIAS_WARMUP constant missing from backtest_runner.py — "
            "BiasFilter needs ≥22 daily bars at backtest_start (live-parity)"
        )
        assert "MOMENTUM_WARMUP" in source, (
            "MOMENTUM_WARMUP constant missing from backtest_runner.py — "
            "MomentumFilter needs ≥21 weekly bars at backtest_start (live-parity)"
        )
        # Sanity-check the values: bias ≥22 days (need 22 daily bars); momentum
        # ≥147 days (21 weekly bars = 147d). Allow some safety margin.
        assert "timedelta(days=30)" in source or "timedelta(days=29)" in source, (
            "BIAS_WARMUP should be ≥22 days (recommended: 30 for safety)"
        )
        assert "timedelta(days=154)" in source or "timedelta(days=147)" in source, (
            "MOMENTUM_WARMUP should be ≥147 days (21 weeks; recommended: 154 for safety)"
        )

    def test_bias_fetch_uses_warmup(self) -> None:
        """fetch_and_cache for bias_tf must NOT use raw start_dt."""
        source = RUNNER_PATH.read_text()
        bad_pattern = "fetch_and_cache(sym, bias_tf, start_dt, end_dt"
        assert bad_pattern not in source, (
            f"REGRESSION: {bad_pattern!r} found in backtest_runner.py. "
            "BiasFilter fetch must use start_dt - BIAS_WARMUP (or equivalent), "
            "otherwise BiasFilter falls into passthrough for first ~3 weeks of "
            "the backtest period and produces ≠live results."
        )

    def test_weekly_fetch_uses_warmup(self) -> None:
        """fetch_and_cache for 1w must NOT use raw start_dt."""
        source = RUNNER_PATH.read_text()
        bad_pattern = 'fetch_and_cache(sym, "1w", start_dt, end_dt'
        assert bad_pattern not in source, (
            f"REGRESSION: {bad_pattern!r} found in backtest_runner.py. "
            "MomentumFilter fetch must use start_dt - MOMENTUM_WARMUP (or "
            "equivalent), otherwise MomentumFilter falls into fail-open BULLISH "
            "for first ~5 months of the backtest period and produces ≠live results."
        )
