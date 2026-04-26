"""Tests for trail optimizer DB methods."""
import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert(db, activation=1.0, trail=1.5, pf=1.2, status="pending"):
    return db.insert_trail_run(
        symbol="BTCUSDT",
        timeframe="4h",
        period_days=180,
        trail_activation_mult=activation,
        trail_atr_mult=trail,
        ema_stop_mult=1.5,
        ema_tp_mult=3.5,
        profit_factor=pf,
        sharpe_ratio=0.8,
        win_rate=55.0,
        max_drawdown=10.0,
        total_trades=20,
        total_pnl=500.0,
        status=status,
    )


class TestInsertTrailRun:
    def test_returns_id(self, db):
        run_id = _insert(db)
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_second_insert_increments_id(self, db):
        id1 = _insert(db)
        id2 = _insert(db)
        assert id2 == id1 + 1


class TestGetTrailRuns:
    def test_empty_returns_empty_list(self, db):
        assert db.get_trail_runs() == []

    def test_returns_inserted_run(self, db):
        _insert(db, activation=2.0, trail=1.0)
        runs = db.get_trail_runs()
        assert len(runs) == 1
        assert runs[0]["trail_activation_mult"] == 2.0
        assert runs[0]["trail_atr_mult"] == 1.0

    def test_limit_respected(self, db):
        for _ in range(5):
            _insert(db)
        runs = db.get_trail_runs(limit=3)
        assert len(runs) == 3

    def test_ordered_by_timestamp_desc(self, db):
        _insert(db, pf=1.1)
        _insert(db, pf=1.5)
        runs = db.get_trail_runs()
        assert runs[0]["profit_factor"] == 1.5


class TestGetBestPendingTrailRun:
    def test_returns_none_when_empty(self, db):
        assert db.get_best_pending_trail_run() is None

    def test_returns_highest_pf_pending(self, db):
        _insert(db, pf=1.1, status="pending")
        _insert(db, pf=1.8, status="pending")
        _insert(db, pf=2.0, status="approved")
        best = db.get_best_pending_trail_run()
        assert best["profit_factor"] == 1.8

    def test_ignores_non_pending(self, db):
        _insert(db, pf=2.0, status="approved")
        _insert(db, pf=2.0, status="rejected")
        assert db.get_best_pending_trail_run() is None


class TestSetTrailRunStatus:
    def test_updates_status(self, db):
        run_id = _insert(db, status="pending")
        db.set_trail_run_status(run_id, "approved")
        runs = db.get_trail_runs()
        assert runs[0]["status"] == "approved"

    def test_reject(self, db):
        run_id = _insert(db, status="pending")
        db.set_trail_run_status(run_id, "rejected")
        assert db.get_best_pending_trail_run() is None
