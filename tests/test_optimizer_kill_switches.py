"""Tests for the optimizer kill switches.

The May 2026 walk-forward audit proved that the auto-optimizer's methodology
(single 180d window, sort by PF) overfits to recent noise. Until sub-project D
delivers a new methodology, both auto-optimizers default to DISABLED via the
`auto_optimizer_enabled` and `auto_entry_quality_enabled` runtime keys.

See: docs/audits/A_walk_forward_2026-05-14.md (audit verdict NO-GO for current
methodology) and the bake-off in scripts/audit/optimizer_bakeoff.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.database.db import Database
from bot.optimizer.auto_entry_quality_optimizer import should_run as eq_should_run
from bot.optimizer.auto_optimizer import should_run as wf_should_run


# ── Walk-forward optimizer kill switch ────────────────────────────────────────

def test_wf_should_run_false_when_flag_explicitly_false(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.set_runtime_config(auto_optimizer_enabled="false")
    assert wf_should_run(db) is False


def test_wf_should_run_true_when_flag_true_and_no_history(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.set_runtime_config(auto_optimizer_enabled="true")
    assert wf_should_run(db) is True


def test_wf_should_run_false_when_flag_false_even_if_interval_elapsed(tmp_path) -> None:
    """Flag overrides the time-based interval check."""
    db = Database(str(tmp_path / "test.db"))
    old = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    db.set_runtime_config(
        auto_optimizer_enabled="false",
        last_auto_optimizer_run=old,
    )
    assert wf_should_run(db) is False


def test_wf_should_run_defaults_to_disabled_when_key_missing(tmp_path) -> None:
    """Until sub-project D ships a fixed methodology, default is OFF.

    This is the audit-driven safety: an empty DB (post-nuke) must NOT auto-run
    the proven-broken optimizer.
    """
    db = Database(str(tmp_path / "test.db"))
    # No config set at all
    assert wf_should_run(db) is False


# ── Entry-quality optimizer kill switch ───────────────────────────────────────

def test_eq_should_run_false_when_flag_explicitly_false(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.set_runtime_config(auto_entry_quality_enabled="false")
    assert eq_should_run(db) is False


def test_eq_should_run_true_when_flag_true_and_no_history(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    db.set_runtime_config(auto_entry_quality_enabled="true")
    assert eq_should_run(db) is True


def test_eq_should_run_defaults_to_disabled_when_key_missing(tmp_path) -> None:
    db = Database(str(tmp_path / "test.db"))
    assert eq_should_run(db) is False
