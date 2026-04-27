"""Tests for entry quality optimizer DB methods and grid search."""
import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert(db, vol_mult=1.5, bar_dir=True, momentum=True, min_atr=0.003, pf=1.2, status="pending"):
    return db.insert_entry_quality_run(
        symbol="BTCUSDT",
        timeframe="1h",
        period_days=270,
        vol_mult=vol_mult,
        bar_direction=bar_dir,
        ema_momentum=momentum,
        min_atr_pct=min_atr,
        ema_stop_mult=1.5,
        ema_tp_mult=3.5,
        profit_factor=pf,
        sharpe_ratio=0.8,
        win_rate=52.0,
        max_drawdown=12.0,
        total_trades=18,
        total_pnl=400.0,
        status=status,
    )


class TestInsertEntryQualityRun:
    def test_returns_positive_int_id(self, db):
        run_id = _insert(db)
        assert isinstance(run_id, int) and run_id > 0

    def test_second_insert_increments_id(self, db):
        id1 = _insert(db)
        id2 = _insert(db)
        assert id2 == id1 + 1

    def test_vol_mult_zero_preserved(self, db):
        _insert(db, vol_mult=0.0)
        rows = db.get_entry_quality_runs()
        assert rows[0]["vol_mult"] == 0.0

    def test_bool_bar_direction_roundtrip(self, db):
        _insert(db, bar_dir=True)
        rows = db.get_entry_quality_runs()
        assert bool(rows[0]["bar_direction"]) is True

    def test_bool_false_roundtrip(self, db):
        _insert(db, bar_dir=False)
        rows = db.get_entry_quality_runs()
        assert bool(rows[0]["bar_direction"]) is False


class TestGetEntryQualityRuns:
    def test_empty_returns_empty_list(self, db):
        assert db.get_entry_quality_runs() == []

    def test_returns_inserted_run(self, db):
        _insert(db, vol_mult=2.0, min_atr=0.005)
        rows = db.get_entry_quality_runs()
        assert len(rows) == 1
        assert rows[0]["vol_mult"] == 2.0
        assert rows[0]["min_atr_pct"] == 0.005

    def test_limit_respected(self, db):
        for _ in range(5):
            _insert(db)
        assert len(db.get_entry_quality_runs(limit=3)) == 3

    def test_ordered_desc_by_timestamp(self, db):
        _insert(db, pf=1.1)
        _insert(db, pf=1.8)
        rows = db.get_entry_quality_runs()
        assert rows[0]["profit_factor"] == 1.8


class TestGetBestPendingEntryQualityRun:
    def test_returns_none_when_empty(self, db):
        assert db.get_best_pending_entry_quality_run() is None

    def test_returns_highest_pf_among_pending(self, db):
        _insert(db, pf=1.1, status="pending")
        _insert(db, pf=1.9, status="pending")
        _insert(db, pf=2.5, status="approved")
        best = db.get_best_pending_entry_quality_run()
        assert best["profit_factor"] == 1.9

    def test_ignores_approved_and_rejected(self, db):
        _insert(db, pf=2.0, status="approved")
        _insert(db, pf=2.0, status="rejected")
        assert db.get_best_pending_entry_quality_run() is None


class TestSetEntryQualityRunStatus:
    def test_updates_to_approved(self, db):
        run_id = _insert(db, status="pending")
        db.set_entry_quality_run_status(run_id, "approved")
        rows = db.get_entry_quality_runs()
        assert rows[0]["status"] == "approved"

    def test_reject_removes_from_pending(self, db):
        run_id = _insert(db, status="pending")
        db.set_entry_quality_run_status(run_id, "rejected")
        assert db.get_best_pending_entry_quality_run() is None


class TestIsViable:
    def _summary(self, **overrides):
        base = {
            "total_trades": 20,
            "max_drawdown_pct": 10.0,
            "sharpe_ratio": 0.8,
            "profit_factor": 1.2,
            "win_rate_pct": 55.0,
            "total_pnl": 500.0,
        }
        base.update(overrides)
        return base

    def test_all_pass_is_viable(self):
        from bot.optimizer.entry_quality_optimizer import _is_viable
        assert _is_viable(self._summary()) is True

    def test_trades_below_threshold_not_viable(self):
        from bot.optimizer.entry_quality_optimizer import _is_viable
        assert _is_viable(self._summary(total_trades=9)) is False

    def test_drawdown_above_threshold_not_viable(self):
        from bot.optimizer.entry_quality_optimizer import _is_viable
        assert _is_viable(self._summary(max_drawdown_pct=21.0)) is False

    def test_sharpe_below_threshold_not_viable(self):
        from bot.optimizer.entry_quality_optimizer import _is_viable
        assert _is_viable(self._summary(sharpe_ratio=0.39)) is False

    def test_pf_below_threshold_not_viable(self):
        from bot.optimizer.entry_quality_optimizer import _is_viable
        assert _is_viable(self._summary(profit_factor=1.04)) is False


class TestGridGeneration:
    def test_grid_produces_48_combos(self):
        from itertools import product
        from bot.optimizer.entry_quality_optimizer import (
            VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID
        )
        combos = list(product(VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID))
        assert len(combos) == 48

    def test_zero_vol_mult_in_grid(self):
        from bot.optimizer.entry_quality_optimizer import VOL_GRID
        assert 0.0 in VOL_GRID

    def test_false_in_bar_dir_grid(self):
        from bot.optimizer.entry_quality_optimizer import BAR_DIR_GRID
        assert False in BAR_DIR_GRID


class TestBacktestConfigOverrides:
    def test_new_fields_exist_with_none_defaults(self):
        from bot.backtest.engine import BacktestConfig
        cfg = BacktestConfig(timeframe="1h")
        assert cfg.ema_volume_mult is None
        assert cfg.ema_require_bar_dir is None
        assert cfg.ema_require_momentum is None
        assert cfg.ema_min_atr_pct is None

    def test_fields_accept_values(self):
        from bot.backtest.engine import BacktestConfig
        cfg = BacktestConfig(
            timeframe="1h",
            ema_volume_mult=1.5,
            ema_require_bar_dir=True,
            ema_require_momentum=False,
            ema_min_atr_pct=0.003,
        )
        assert cfg.ema_volume_mult == 1.5
        assert cfg.ema_require_bar_dir is True
        assert cfg.ema_require_momentum is False
        assert cfg.ema_min_atr_pct == 0.003

    def test_zero_vol_mult_is_not_none(self):
        from bot.backtest.engine import BacktestConfig
        cfg = BacktestConfig(timeframe="1h", ema_volume_mult=0.0)
        assert cfg.ema_volume_mult is not None
        assert cfg.ema_volume_mult == 0.0


class TestApplyEmaConfigEntryFilters:
    def _make_orch(self, tmp_path):
        from bot.database.db import Database
        from bot.orchestrator import StrategyOrchestrator
        db = Database(str(tmp_path / "test.db"))
        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        return db, orch

    def test_vol_mult_applied(self, tmp_path):
        import main
        db, orch = self._make_orch(tmp_path)
        db.set_runtime_config(ema_vol_mult="1.5")
        main._apply_ema_config(db, orch)
        from bot.constants import StrategyName
        assert orch.get_strategy(StrategyName.EMA_CROSSOVER).config.volume_multiplier == 1.5

    def test_bar_dir_true_applied(self, tmp_path):
        import main
        db, orch = self._make_orch(tmp_path)
        db.set_runtime_config(ema_bar_dir="true")
        main._apply_ema_config(db, orch)
        from bot.constants import StrategyName
        assert orch.get_strategy(StrategyName.EMA_CROSSOVER).config.require_bar_direction is True

    def test_bar_dir_false_applied(self, tmp_path):
        import main
        db, orch = self._make_orch(tmp_path)
        db.set_runtime_config(ema_bar_dir="false")
        main._apply_ema_config(db, orch)
        from bot.constants import StrategyName
        assert orch.get_strategy(StrategyName.EMA_CROSSOVER).config.require_bar_direction is False

    def test_missing_keys_leave_preset_intact(self, tmp_path):
        import main
        from bot.constants import StrategyName
        db, orch = self._make_orch(tmp_path)
        preset_vol = orch.get_strategy(StrategyName.EMA_CROSSOVER).config.volume_multiplier
        main._apply_ema_config(db, orch)  # no entry quality keys in config
        assert orch.get_strategy(StrategyName.EMA_CROSSOVER).config.volume_multiplier == preset_vol

    def test_zero_vol_mult_applied_not_skipped(self, tmp_path):
        import main
        db, orch = self._make_orch(tmp_path)
        db.set_runtime_config(ema_vol_mult="0.0")
        main._apply_ema_config(db, orch)
        from bot.constants import StrategyName
        assert orch.get_strategy(StrategyName.EMA_CROSSOVER).config.volume_multiplier == 0.0
