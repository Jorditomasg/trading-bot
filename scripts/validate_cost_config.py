#!/usr/bin/env python
"""End-to-end validation of the backtest cost config (DB-backed).

Tests:
1. Fallback path: no DB → resolve_cost_per_side() returns FALLBACK_COST_PER_SIDE
2. Seed path: create temp DB, run seeding, verify key persisted
3. Read path: get_backtest_cost_per_side() returns seeded value
4. Update path: change via set_runtime_config, verify reflected
5. Engine path: BacktestConfig with explicit override + with default both work
6. Dashboard form code: import without errors (smoke test only)

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_cost_config.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Use a temp DB for the test so we don't touch any real one
_TMP_DIR = tempfile.mkdtemp(prefix="trading_bot_test_")
_TMP_DB  = str(Path(_TMP_DIR) / "test.db")
os.environ["DB_PATH"] = _TMP_DB

# Late imports: must come AFTER setting DB_PATH
from bot.backtest.cost import FALLBACK_COST_PER_SIDE, resolve_cost_per_side
from bot.database.db import Database


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    print(f"\n{'=' * 80}")
    print(f"  COST CONFIG VALIDATION")
    print(f"  Temp DB: {_TMP_DB}")
    print(f"{'=' * 80}\n")

    failures: list[str] = []

    # ── 1. Fallback path (DB exists but no key seeded yet) ────────────────────
    print("1. Fallback path:")
    db = Database(_TMP_DB)  # creates empty schema, no rt_ keys
    fallback_value = db.get_backtest_cost_per_side()
    if not check("Empty DB returns fallback 0.001",
                 abs(fallback_value - FALLBACK_COST_PER_SIDE) < 1e-9,
                 f"got {fallback_value}"):
        failures.append("fallback")

    helper_value = resolve_cost_per_side(_TMP_DB)
    if not check("resolve_cost_per_side() returns same fallback",
                 abs(helper_value - FALLBACK_COST_PER_SIDE) < 1e-9,
                 f"got {helper_value}"):
        failures.append("helper-fallback")

    # ── 2. Seed path (replicate main.py _seed_optimal_defaults) ───────────────
    print("\n2. Seed path:")
    db.set_runtime_config(backtest_cost_per_side="0.001")
    cfg = db.get_runtime_config()
    if not check("Seeded key exists in runtime config",
                 "backtest_cost_per_side" in cfg,
                 f"keys: {list(cfg.keys())}"):
        failures.append("seed-key")
    if not check("Seeded value is 0.001 string",
                 cfg.get("backtest_cost_per_side") == "0.001",
                 f"got {cfg.get('backtest_cost_per_side')!r}"):
        failures.append("seed-value")

    # ── 3. Read path ──────────────────────────────────────────────────────────
    print("\n3. Read path:")
    read_value = db.get_backtest_cost_per_side()
    if not check("get_backtest_cost_per_side() returns 0.001 after seed",
                 abs(read_value - 0.001) < 1e-9,
                 f"got {read_value}"):
        failures.append("read")

    # ── 4. Update path (simulate dashboard CONFIG save) ───────────────────────
    print("\n4. Update path (simulate dashboard CONFIG save):")
    new_value = 0.0012   # user wants 0.12% per side (slightly more conservative)
    db.set_runtime_config(backtest_cost_per_side=str(new_value))
    after = db.get_backtest_cost_per_side()
    if not check("After update, value reads 0.0012",
                 abs(after - new_value) < 1e-9,
                 f"got {after}"):
        failures.append("update-read")

    helper_after = resolve_cost_per_side(_TMP_DB)
    if not check("resolve_cost_per_side() reflects update",
                 abs(helper_after - new_value) < 1e-9,
                 f"got {helper_after}"):
        failures.append("update-helper")

    # ── 5. Engine path ────────────────────────────────────────────────────────
    print("\n5. Engine path:")
    from bot.backtest.engine import BacktestConfig

    # Default (with our DB_PATH env set, BacktestConfig should pick up 0.0012)
    cfg_default = BacktestConfig(timeframe="4h")
    if not check("BacktestConfig() default reads 0.0012 from DB",
                 abs(cfg_default.cost_per_side_pct - new_value) < 1e-9,
                 f"got {cfg_default.cost_per_side_pct}"):
        failures.append("engine-default")

    # Explicit override still works (caller can pin a value)
    cfg_explicit = BacktestConfig(timeframe="4h", cost_per_side_pct=0.0008)
    if not check("BacktestConfig(cost_per_side_pct=0.0008) override works",
                 abs(cfg_explicit.cost_per_side_pct - 0.0008) < 1e-9,
                 f"got {cfg_explicit.cost_per_side_pct}"):
        failures.append("engine-override")

    # ── 6. Dashboard form: static analysis (streamlit may not be installed) ───
    print("\n6. Dashboard form static checks:")
    import ast
    cm_path = Path("dashboard/sections/config_manager.py")
    src = cm_path.read_text()
    # Verify the file parses
    try:
        tree = ast.parse(src)
        check("config_manager.py parses cleanly", True)
    except SyntaxError as exc:
        check("config_manager.py parses cleanly", False, str(exc))
        failures.append("dashboard-syntax")
        tree = None

    if tree is not None:
        if not check("Form contains 'Backtest fee/side' input",
                     "Backtest fee/side" in src):
            failures.append("dashboard-input")
        if not check("set_runtime_config call passes backtest_cost_per_side",
                     "backtest_cost_per_side=" in src):
            failures.append("dashboard-save")
        if not check("Reads cur_backtest_cost from cfg",
                     "cur_backtest_cost" in src and 'cfg.get("backtest_cost_per_side"' in src):
            failures.append("dashboard-read")

    # ── 7. End-to-end backtest with DB-backed cost ────────────────────────────
    print("\n7. End-to-end backtest reads DB cost:")
    try:
        from datetime import datetime, timezone, timedelta
        from bot.backtest.cache import fetch_and_cache
        from bot.backtest.engine import BacktestEngine
        from bot.backtest.scenario_runner import compute_annual_return
        # Reset DB cost to 0.001 for this test
        db.set_runtime_config(backtest_cost_per_side="0.001")

        end = datetime.now(tz=timezone.utc)
        start = end - timedelta(days=1095 + 30)
        df_4h = fetch_and_cache("BTCUSDT", "4h", start, end)
        df_1d = fetch_and_cache("BTCUSDT", "1d", start, end)

        cfg_e2e = BacktestConfig(
            initial_capital=10_000, risk_per_trade=0.04, timeframe="4h", long_only=True
        )
        if not check("BacktestConfig in run picks up 0.001 from DB",
                     abs(cfg_e2e.cost_per_side_pct - 0.001) < 1e-9):
            failures.append("e2e-cfg")

        engine = BacktestEngine(cfg_e2e)
        bt = engine.run(df=df_4h, df_4h=df_1d, symbol="BTCUSDT")
        s = engine.summary(bt)
        ann = compute_annual_return(bt.initial_capital, bt.final_capital, 1095)
        # Expect approximately +53% (matches our reference run)
        ann_pct = ann * 100
        if not check(f"Backtest produces +52% to +54% annual (got {ann_pct:+.1f}%)",
                     52.0 <= ann_pct <= 54.0):
            failures.append("e2e-result")
        print(f"      → Annual={ann_pct:+.1f}%, PF={s['profit_factor']:.2f}, DD=-{s['max_drawdown_pct']:.1f}%")
    except Exception as exc:
        check("End-to-end backtest", False, str(exc))
        failures.append("e2e-exception")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    if failures:
        print(f"  RESULT: FAIL — {len(failures)} check(s) failed: {failures}")
        print(f"{'=' * 80}\n")
        return 1
    print(f"  RESULT: PASS — all checks succeeded")
    print(f"{'=' * 80}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
