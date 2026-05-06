"""Circuit breaker state must survive process restarts.

The bot is rebooted with `init 6` periodically. Without persistence, a
breaker triggered before the restart is lost — the bot wakes up trading
again, silently bypassing the cooldown. These tests verify that
RiskManager persists `_breaker_triggered_at` to the DB and reloads it on
construction.
"""

from datetime import datetime, timedelta

import pytest

from bot.database.db import Database
from bot.risk.manager import RiskConfig, RiskManager


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_fresh_db_has_no_breaker_state(db):
    rm = RiskManager(RiskConfig(), symbol="BTCUSDT", db=db)
    assert rm._breaker_triggered_at is None


def test_breaker_state_persists_across_instances(db):
    cfg = RiskConfig(max_drawdown=0.10, cooldown_hours=4)

    rm1 = RiskManager(cfg, symbol="BTCUSDT", db=db)
    assert rm1.check_circuit_breaker(current_capital=850.0, peak_capital=1000.0) is True
    saved_ts = rm1._breaker_triggered_at
    assert saved_ts is not None

    # Simulate process restart — new RiskManager from same DB
    rm2 = RiskManager(cfg, symbol="BTCUSDT", db=db)
    assert rm2._breaker_triggered_at is not None
    assert abs((rm2._breaker_triggered_at - saved_ts).total_seconds()) < 1


def test_breaker_stays_active_after_restart_within_cooldown(db):
    """The whole point of this fix: init 6 mid-cooldown must NOT clear state."""
    cfg = RiskConfig(max_drawdown=0.10, cooldown_hours=4)

    rm1 = RiskManager(cfg, symbol="BTCUSDT", db=db)
    rm1.check_circuit_breaker(current_capital=850.0, peak_capital=1000.0)

    # New instance — drawdown still bad, cooldown not elapsed → STILL active
    rm2 = RiskManager(cfg, symbol="BTCUSDT", db=db)
    assert rm2.check_circuit_breaker(current_capital=850.0, peak_capital=1000.0) is True


def test_breaker_auto_resets_after_cooldown_via_persisted_timestamp(db):
    """If the bot was down longer than cooldown, restored state auto-resets."""
    cfg = RiskConfig(max_drawdown=0.10, cooldown_hours=4)

    # Manually persist a stale timestamp (5h ago — cooldown elapsed during downtime)
    old_ts = (datetime.now() - timedelta(hours=5)).isoformat()
    db.set_config("breaker_triggered_at_BTCUSDT", old_ts)

    rm = RiskManager(cfg, symbol="BTCUSDT", db=db)
    assert rm._breaker_triggered_at is not None

    # Cooldown elapsed → breaker resets even though drawdown still bad
    assert rm.check_circuit_breaker(current_capital=850.0, peak_capital=1000.0) is False
    assert rm._breaker_triggered_at is None
    assert db.get_config("breaker_triggered_at_BTCUSDT") in (None, "")


def test_breaker_state_cleared_on_drawdown_recovery(db):
    cfg = RiskConfig(max_drawdown=0.15, cooldown_hours=4)

    rm = RiskManager(cfg, symbol="BTCUSDT", db=db)
    rm.check_circuit_breaker(current_capital=800.0, peak_capital=1000.0)
    assert db.get_config("breaker_triggered_at_BTCUSDT") not in (None, "")

    # Drawdown recovers → breaker resets and DB key is cleared
    assert rm.check_circuit_breaker(current_capital=950.0, peak_capital=1000.0) is False
    assert rm._breaker_triggered_at is None
    assert db.get_config("breaker_triggered_at_BTCUSDT") in (None, "")


def test_no_persistence_without_db(db):
    """Without db reference, RiskManager works in-memory only (backward compat)."""
    rm = RiskManager(RiskConfig(), symbol="BTCUSDT")  # no db
    rm.check_circuit_breaker(800.0, 1000.0)
    assert rm._breaker_triggered_at is not None  # in memory
    # No DB keys created
    assert db.get_config("breaker_triggered_at_BTCUSDT") in (None, "")


def test_invalid_persisted_timestamp_is_ignored(db):
    db.set_config("breaker_triggered_at_BTCUSDT", "not-a-valid-iso-timestamp")
    rm = RiskManager(RiskConfig(), symbol="BTCUSDT", db=db)
    assert rm._breaker_triggered_at is None
