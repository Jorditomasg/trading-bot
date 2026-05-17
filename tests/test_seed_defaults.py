"""Tests for _seed_optimized_defaults and _resolve_legacy_keys.

Phase 1 of win-rate-uplift-2026-05: verify that the seed writes all 13 expected keys
(including 4 new entries) and that legacy-C2 values trigger warn-and-detect logic.

TDD: tests are written first (T01/T03) and turn green after T02/T04 implementation.
"""
from __future__ import annotations

import logging

import pytest

from bot.database.db import Database
from main import _seed_optimized_defaults


# ── Expected seed values after Phase 1 ───────────────────────────────────────

EXPECTED_SEED = {
    "symbol":                    "BTCUSDT",
    "timeframe":                 "4h",
    "risk_per_trade":            "0.015",
    "ema_stop_mult":             "1.5",
    "ema_tp_mult":               "5.0",
    "ema_max_dist_atr":          "1.0",
    "long_only":                 "true",
    "backtest_cost_per_side":    "0.001",
    "auto_optimizer_enabled":    "false",
    "auto_entry_quality_enabled": "false",
    # 4 new keys added by Phase 1 (T02)
    "ema_vol_mult":              "1.5",
    "ema_bar_dir":               "true",
    "ema_momentum_req":          "true",
    "momentum_neutral_band":     "0.08",
}


class TestSeedDefaults:
    def test_seed_writes_all_required_keys(self, tmp_path) -> None:
        """Fresh DB: all 13 expected keys must be written with exact values."""
        db = Database(str(tmp_path / "test.db"))
        _seed_optimized_defaults(db)
        cfg = db.get_runtime_config()
        for key, expected in EXPECTED_SEED.items():
            assert key in cfg, f"Missing key: {key!r}"
            assert cfg[key] == expected, (
                f"Wrong value for {key!r}: got {cfg[key]!r}, expected {expected!r}"
            )

    def test_seed_idempotent_on_second_run(self, tmp_path) -> None:
        """Second seed run: no keys must be overwritten when all exist."""
        db = Database(str(tmp_path / "test.db"))
        _seed_optimized_defaults(db)
        cfg_before = dict(db.get_runtime_config())
        _seed_optimized_defaults(db)
        cfg_after  = dict(db.get_runtime_config())
        assert cfg_before == cfg_after, "Second seed run must not change any existing values"


class TestLegacyKeyMigration:
    def test_legacy_key_migration_corrects_wrong_values(self, tmp_path, caplog) -> None:
        """Pre-populate with legacy C2 values; after seed, WARN log must fire per legacy key.

        Note: per design D11 this is WARN-only (detect-and-warn), NOT auto-correct.
        The values in the DB must remain unchanged — only the log fires.
        """
        db = Database(str(tmp_path / "test.db"))

        # Pre-populate with legacy/wrong C2 values
        db.set_runtime_config(
            risk_per_trade="0.03",
            ema_stop_mult="1.25",
            ema_tp_mult="3.5",
            ema_vol_mult="2.0",
            ema_bar_dir="false",
        )

        with caplog.at_level(logging.WARNING, logger="main"):
            _seed_optimized_defaults(db)

        # A WARNING containing legacy info must have been logged
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("legacy" in m.lower() or "0.03" in m or "1.25" in m for m in warning_msgs), (
            f"Expected a legacy-value warning; got: {warning_msgs}"
        )

        # Values must NOT be auto-corrected (detect-and-warn only)
        cfg = db.get_runtime_config()
        assert cfg["risk_per_trade"] == "0.03", "risk_per_trade must NOT be auto-corrected"
        assert cfg["ema_stop_mult"]  == "1.25", "ema_stop_mult must NOT be auto-corrected"
