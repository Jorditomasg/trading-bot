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


class TestEntryQualityRunMaxDistanceAtr:
    def test_insert_and_read_max_distance_atr(self, db):
        db.insert_entry_quality_run(
            symbol="BTCUSDT", timeframe="1h", period_days=90,
            vol_mult=1.0, bar_direction=True, ema_momentum=True,
            min_atr_pct=0.003, ema_stop_mult=1.5, ema_tp_mult=3.5,
            profit_factor=1.2, sharpe_ratio=0.5, win_rate=55.0,
            max_drawdown=10.0, total_trades=25, total_pnl=500.0,
            status="pending",
            max_distance_atr=0.3,
        )
        runs = db.get_entry_quality_runs(limit=1)
        assert runs[0]["max_distance_atr"] == pytest.approx(0.3)

    def test_null_max_distance_atr_returns_default(self, db):
        # Simulate a pre-migration row by inserting directly via SQL with NULL
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO entry_quality_runs "
                "(timestamp, symbol, timeframe, period_days, vol_mult, bar_direction, ema_momentum, "
                "min_atr_pct, ema_stop_mult, ema_tp_mult, profit_factor, sharpe_ratio, "
                "win_rate, max_drawdown, total_trades, total_pnl, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2024-01-01T00:00:00", "BTCUSDT", "1h", 90, 1.0, 1, 1, 0.003, 1.5, 3.5, 1.2, 0.5, 55.0, 10.0, 25, 500.0, "pending")
            )
        runs = db.get_entry_quality_runs(limit=1)
        assert runs[0]["max_distance_atr"] == pytest.approx(0.5)

    def test_best_pending_includes_max_distance_atr(self, db):
        db.insert_entry_quality_run(
            symbol="BTCUSDT", timeframe="1h", period_days=90,
            vol_mult=1.0, bar_direction=True, ema_momentum=True,
            min_atr_pct=0.003, ema_stop_mult=1.5, ema_tp_mult=3.5,
            profit_factor=1.5, sharpe_ratio=0.6, win_rate=58.0,
            max_drawdown=9.0, total_trades=30, total_pnl=700.0,
            status="pending",
            max_distance_atr=0.8,
        )
        best = db.get_best_pending_entry_quality_run()
        assert best["max_distance_atr"] == pytest.approx(0.8)

    def test_best_pending_null_max_distance_atr_returns_default(self, db):
        # Insert via raw SQL to simulate pre-migration row
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO entry_quality_runs "
                "(timestamp, symbol, timeframe, period_days, vol_mult, bar_direction, ema_momentum, "
                "min_atr_pct, ema_stop_mult, ema_tp_mult, profit_factor, sharpe_ratio, "
                "win_rate, max_drawdown, total_trades, total_pnl, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2024-01-01T00:00:00", "BTCUSDT", "1h", 90, 1.0, 1, 1, 0.003, 1.5, 3.5, 1.9, 0.5, 55.0, 10.0, 25, 500.0, "pending")
            )
        best = db.get_best_pending_entry_quality_run()
        assert best["max_distance_atr"] == pytest.approx(0.5)


class TestGridGeneration:
    def test_grid_produces_48_combos(self):
        from itertools import product
        from bot.optimizer.entry_quality_optimizer import (
            VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID
        )
        combos = list(product(VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID))
        assert len(combos) == 48


class TestGridGenerationWithDistAtr:
    def test_combo_count_is_240(self):
        from itertools import product
        from bot.optimizer.entry_quality_optimizer import (
            VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID, DIST_ATR_GRID
        )
        combos = list(product(VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID, DIST_ATR_GRID))
        assert len(combos) == 240

    def test_result_rows_contain_max_distance_atr(self):
        """Grid search result rows must include max_distance_atr key."""
        from unittest.mock import patch, MagicMock
        import pandas as pd
        from bot.optimizer.entry_quality_optimizer import run_entry_quality_grid_search

        mock_db = MagicMock()
        mock_db.get_runtime_config.return_value = {"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"}

        df = pd.DataFrame({
            "open":   [100.0] * 300,
            "high":   [102.0] * 300,
            "low":    [98.0]  * 300,
            "close":  [101.0] * 300,
            "volume": [1000.0] * 300,
        })

        with patch("bot.optimizer.entry_quality_optimizer.fetch_and_cache", return_value=df):
            with patch("bot.optimizer.entry_quality_optimizer.BacktestEngine") as MockEngine:
                mock_engine = MagicMock()
                MockEngine.return_value = mock_engine
                mock_engine.run.return_value = {}
                mock_engine.summary.return_value = {
                    "profit_factor": 0.5, "sharpe_ratio": 0.1,
                    "win_rate_pct": 40.0, "max_drawdown_pct": 25.0,
                    "total_trades": 5, "total_pnl": -100.0,
                }
                results = run_entry_quality_grid_search(mock_db, "BTCUSDT", "1h", lookback_days=90)

        assert len(results) == 240
        assert "max_distance_atr" in results[0]

    def test_backtest_config_receives_dist_atr(self):
        """Each BacktestConfig call must include ema_max_distance_atr."""
        from unittest.mock import patch, MagicMock
        import pandas as pd
        from bot.optimizer.entry_quality_optimizer import run_entry_quality_grid_search, DIST_ATR_GRID
        from bot.backtest.engine import BacktestConfig

        mock_db = MagicMock()
        mock_db.get_runtime_config.return_value = {}

        df = pd.DataFrame({
            "open": [100.0]*300, "high": [102.0]*300,
            "low": [98.0]*300, "close": [101.0]*300, "volume": [1000.0]*300,
        })

        configs_created = []
        original_init = BacktestConfig.__init__
        def capture_init(self, **kwargs):
            configs_created.append(kwargs.get("ema_max_distance_atr"))
            original_init(self, **kwargs)

        with patch("bot.optimizer.entry_quality_optimizer.fetch_and_cache", return_value=df):
            with patch("bot.optimizer.entry_quality_optimizer.BacktestEngine") as MockEngine:
                mock_engine = MagicMock()
                MockEngine.return_value = mock_engine
                mock_engine.run.return_value = {}
                mock_engine.summary.return_value = {
                    "profit_factor": 0.5, "sharpe_ratio": 0.1,
                    "win_rate_pct": 40.0, "max_drawdown_pct": 25.0,
                    "total_trades": 5, "total_pnl": -100.0,
                }
                with patch.object(BacktestConfig, "__init__", capture_init):
                    run_entry_quality_grid_search(mock_db, "BTCUSDT", "1h", lookback_days=90)

        # Every config must have a non-None ema_max_distance_atr from DIST_ATR_GRID
        assert len(configs_created) == 240
        unique_vals = set(configs_created)
        assert unique_vals == set(DIST_ATR_GRID)

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


class TestAutoEntryQualityOptimizerMaxDist:
    def _make_db(self, best_max_dist: float = 0.3, old_max_dist: str | None = None):
        from unittest.mock import MagicMock
        db = MagicMock()
        runtime_cfg = {"ema_stop_mult": "1.5", "ema_tp_mult": "3.5"}
        if old_max_dist is not None:
            runtime_cfg["ema_max_dist_atr"] = old_max_dist
        db.get_runtime_config.return_value = runtime_cfg
        db.get_best_pending_entry_quality_run.return_value = None
        return db

    def _best_result(self, max_distance_atr: float = 0.3) -> dict:
        return {
            "vol_mult": 1.0, "bar_direction": True, "ema_momentum": True,
            "min_atr_pct": 0.003, "max_distance_atr": max_distance_atr,
            "profit_factor": 1.5, "sharpe_ratio": 0.8, "win_rate": 60.0,
            "max_drawdown": 10.0, "total_trades": 30, "viable": True,
        }

    def test_applies_max_distance_atr_to_bot_config(self):
        from unittest.mock import patch
        from bot.optimizer.auto_entry_quality_optimizer import run_and_apply

        db = self._make_db(old_max_dist="0.8")  # changing from 0.8 to 0.3
        best = self._best_result(max_distance_atr=0.3)

        with patch("bot.optimizer.auto_entry_quality_optimizer.run_entry_quality_grid_search",
                   return_value=[best]):
            run_and_apply(db, "BTCUSDT", "1h")

        call_kwargs = db.set_runtime_config.call_args[1]
        assert call_kwargs.get("ema_max_dist_atr") == "0.3"

    def test_skips_update_when_max_distance_atr_unchanged(self):
        from unittest.mock import patch, call
        from bot.optimizer.auto_entry_quality_optimizer import run_and_apply, LAST_RUN_KEY

        # Old and new are both 0.3 — all params match → change-detection fires, no param update
        db = self._make_db(old_max_dist="0.3")
        db.get_runtime_config.return_value = {
            "ema_vol_mult": "1.0", "ema_bar_dir": "true",
            "ema_momentum": "true", "ema_min_atr": "0.003",
            "ema_max_dist_atr": "0.3",
        }
        best = self._best_result(max_distance_atr=0.3)

        with patch("bot.optimizer.auto_entry_quality_optimizer.run_entry_quality_grid_search",
                   return_value=[best]):
            result = run_and_apply(db, "BTCUSDT", "1h")

        assert result is None
        # Only the timestamp call is allowed — no ema_ params written
        for c in db.set_runtime_config.call_args_list:
            assert "ema_max_dist_atr" not in c.kwargs, (
                f"set_runtime_config should not have been called with ema_max_dist_atr but got: {c}"
            )


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

    def test_applies_max_dist_atr_from_config(self, tmp_path):
        """_apply_ema_config reads ema_max_dist_atr and patches strategy config."""
        import main
        db, orch = self._make_orch(tmp_path)
        db.set_runtime_config(ema_max_dist_atr="0.3")
        main._apply_ema_config(db, orch)
        from bot.constants import StrategyName
        ema = orch.get_strategy(StrategyName.EMA_CROSSOVER)
        assert ema.config.max_distance_atr == pytest.approx(0.3)

    def test_missing_max_dist_atr_leaves_preset(self, tmp_path):
        """When ema_max_dist_atr absent in config, max_distance_atr keeps its preset value."""
        import main
        from bot.config_presets import get_strategy_configs
        from bot.constants import StrategyName
        db, orch = self._make_orch(tmp_path)
        preset_val = get_strategy_configs("1h")[StrategyName.EMA_CROSSOVER].max_distance_atr
        main._apply_ema_config(db, orch)
        ema = orch.get_strategy(StrategyName.EMA_CROSSOVER)
        assert ema.config.max_distance_atr == pytest.approx(preset_val)
