"""Verify that `_seed_optimized_defaults` writes the audit-validated B-pick.

The May 2026 walk-forward audit + optimizer bake-off (10×3mo windows) selected
SL=1.5, TP=5.0 as the best config, beating C1 baseline (TP=4.5) in 10/10
windows. We use that as the canonical seed.

Also verifies the audit-driven kill switches default to disabled.
"""
from __future__ import annotations

from bot.database.db import Database
from main import _seed_optimized_defaults


def test_seed_writes_b_pick_ema_tp_mult(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["ema_tp_mult"] == "5.0"


def test_seed_preserves_c1_stop_mult(tmp_path) -> None:
    """B-pick uses same SL as C1 (1.5×ATR), only TP differs."""
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["ema_stop_mult"] == "1.5"


def test_seed_writes_validated_risk(tmp_path) -> None:
    """1.5% risk (Quarter-Kelly) — audit's GO config."""
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["risk_per_trade"] == "0.015"


def test_seed_disables_auto_optimizer_by_default(tmp_path) -> None:
    """Audit proved current optimizer methodology is broken — default OFF."""
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["auto_optimizer_enabled"] == "false"


def test_seed_disables_auto_entry_quality_by_default(tmp_path) -> None:
    """Sibling optimizer also disabled until audited."""
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["auto_entry_quality_enabled"] == "false"


def test_seed_does_not_overwrite_user_customizations(tmp_path) -> None:
    """Existing keys must NOT be touched by re-seeding (idempotency)."""
    db = Database(str(tmp_path / "test.db"))
    db.set_runtime_config(
        ema_tp_mult="4.0",                 # user override
        auto_optimizer_enabled="true",     # user explicitly re-enabled
    )
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["ema_tp_mult"] == "4.0"
    assert cfg["auto_optimizer_enabled"] == "true"
