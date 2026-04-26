import pytest
import pandas as pd
from unittest.mock import patch, MagicMock

from bot.momentum.filter import MomentumFilter, MomentumState


@pytest.fixture
def db(tmp_path):
    from bot.database.db import Database
    return Database(str(tmp_path / "test.db"))


def _make_df(n: int = 100, close: float = 50050.0) -> pd.DataFrame:
    return pd.DataFrame({
        "open":   [50000.0] * n,
        "high":   [50100.0] * n,
        "low":    [49900.0] * n,
        "close":  [close]   * n,
        "volume": [100.0]   * n,
    })


def _buy_signal():
    from bot.strategy.base import Signal
    return Signal(action="BUY", strength=0.9, stop_loss=49000.0, take_profit=52000.0, atr=500.0)


class TestMomentumGate:
    def test_bearish_blocks_new_entry(self, db):
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()

        with patch.object(orch, "_select_strategy") as mock_sel:
            mock_strat = MagicMock()
            mock_strat.name = "EMA_CROSSOVER"
            mock_strat.generate_signal.return_value = _buy_signal()
            mock_sel.return_value = mock_strat

            with patch.object(MomentumFilter, "get_state", return_value=MomentumState.BEARISH):
                result = orch.step(df, 10000.0, None, None)

        assert result == []

    def test_momentum_state_stored_on_orchestrator(self, db):
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()

        with patch.object(MomentumFilter, "get_state", return_value=MomentumState.NEUTRAL):
            try:
                orch.step(df, 10000.0, None, None)
            except Exception:
                pass

        assert orch._last_momentum_state == MomentumState.NEUTRAL

    def test_neutral_scales_capital_by_half(self, db):
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()
        capitals = []
        original = orch.risk_manager.compute_position_size

        def capture(capital, entry, stop_loss, risk_fraction=None):
            capitals.append(capital)
            return original(capital=capital, entry=entry, stop_loss=stop_loss, risk_fraction=risk_fraction)

        with patch.object(orch, "_select_strategy") as mock_sel:
            mock_strat = MagicMock()
            mock_strat.name = "EMA_CROSSOVER"
            mock_strat.generate_signal.return_value = _buy_signal()
            mock_sel.return_value = mock_strat

            with patch.object(orch.risk_manager, "compute_position_size", side_effect=capture):
                with patch.object(MomentumFilter, "get_state", return_value=MomentumState.NEUTRAL):
                    orch.step(df, 10000.0, None, None)

        assert len(capitals) == 1
        assert capitals[0] == pytest.approx(5000.0)

    def test_bullish_uses_full_capital(self, db):
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()
        capitals = []
        original = orch.risk_manager.compute_position_size

        def capture(capital, entry, stop_loss, risk_fraction=None):
            capitals.append(capital)
            return original(capital=capital, entry=entry, stop_loss=stop_loss, risk_fraction=risk_fraction)

        with patch.object(orch, "_select_strategy") as mock_sel:
            mock_strat = MagicMock()
            mock_strat.name = "EMA_CROSSOVER"
            mock_strat.generate_signal.return_value = _buy_signal()
            mock_sel.return_value = mock_strat

            with patch.object(orch.risk_manager, "compute_position_size", side_effect=capture):
                with patch.object(MomentumFilter, "get_state", return_value=MomentumState.BULLISH):
                    orch.step(df, 10000.0, None, None)

        assert len(capitals) == 1
        assert capitals[0] == pytest.approx(10000.0)

    def test_step_default_df_weekly_is_none(self, db):
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()
        try:
            orch.step(df, 10000.0, None)   # no df_weekly → fail-open → BULLISH
        except Exception:
            pass
        assert orch._last_momentum_state == MomentumState.BULLISH

    def test_momentum_value_stored_in_db(self, db):
        """Verify momentum is written to signals table as plain string, not 'MomentumState.BEARISH'."""
        from bot.orchestrator import StrategyOrchestrator

        orch = StrategyOrchestrator(db=db, symbol="BTCUSDT")
        df = _make_df()

        with patch.object(orch, "_select_strategy") as mock_sel:
            mock_strat = MagicMock()
            mock_strat.name = "EMA_CROSSOVER"
            mock_strat.generate_signal.return_value = _buy_signal()
            mock_sel.return_value = mock_strat

            with patch.object(MomentumFilter, "get_state", return_value=MomentumState.NEUTRAL):
                orch.step(df, 10000.0, None, None)

        sigs = db.get_recent_signals(1)
        assert sigs, "No signal was inserted"
        assert sigs[0]["momentum"] == "NEUTRAL", (
            f"Expected 'NEUTRAL' but got {sigs[0]['momentum']!r} — "
            "check that momentum_state.value is used, not str(momentum_state)"
        )


class TestRunCycleWeeklyFetch:
    def test_run_cycle_fetches_weekly_klines(self, db):
        from unittest.mock import MagicMock, patch
        from bot.orchestrator import StrategyOrchestrator
        import pandas as pd
        import main as main_module

        orch = StrategyOrchestrator(db=db, symbol="ETHUSDT")

        n = 100
        valid_df = pd.DataFrame({
            "open": [50000.0]*n, "high": [50100.0]*n,
            "low": [49900.0]*n, "close": [50050.0]*n,
            "volume": [100.0]*n,
        })
        weekly_df = pd.DataFrame({"close": [50000.0]*60})

        mock_client = MagicMock()
        # Calls in order: primary klines → daily bias klines → weekly klines
        mock_client.get_klines.side_effect = [valid_df, valid_df, weekly_df]
        mock_client.get_balance.return_value = 10000.0

        with patch.object(main_module, "_build_client", return_value=mock_client):
            try:
                main_module.run_cycle(orch, db, dry_run=True)
            except Exception:
                pass

        intervals_called = [c.args[1] for c in mock_client.get_klines.call_args_list]
        symbols_called   = [c.args[0] for c in mock_client.get_klines.call_args_list]
        assert "1w" in intervals_called, f"Weekly klines not fetched. Got: {intervals_called}"
        for sym in symbols_called:
            assert sym == "ETHUSDT", f"Wrong symbol for weekly fetch: {sym}"
