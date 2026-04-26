import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert_open_trade(db: Database, symbol: str, side: str = "BUY"):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, side, strategy, regime, entry_price, quantity,
                entry_time, stop_loss, take_profit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, "EMA_CROSSOVER", "TRENDING",
             50000.0, 0.001, "2025-01-01T00:00:00", 49000.0, 52000.0),
        )


class TestGetOpenTradesSymbolFilter:
    def test_no_filter_returns_all(self, db):
        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")
        assert len(db.get_open_trades()) == 2

    def test_filter_returns_only_matching_symbol(self, db):
        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")
        result = db.get_open_trades(symbol="BTCUSDT")
        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"

    def test_filter_returns_empty_when_no_match(self, db):
        _insert_open_trade(db, "BTCUSDT")
        assert db.get_open_trades(symbol="SOLUSDT") == []

    def test_closed_trades_excluded(self, db):
        _insert_open_trade(db, "BTCUSDT")
        with db._conn() as conn:
            conn.execute("UPDATE trades SET exit_price = 51000.0 WHERE symbol = 'BTCUSDT'")
        assert db.get_open_trades(symbol="BTCUSDT") == []


class TestGetSetSymbols:
    def test_get_symbols_returns_list_from_db(self, db):
        db.set_symbols(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert db.get_symbols() == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_get_symbols_empty_when_not_set(self, db):
        assert db.get_symbols() == []

    def test_set_symbols_overwrites_previous(self, db):
        db.set_symbols(["BTCUSDT", "ETHUSDT"])
        db.set_symbols(["SOLUSDT"])
        assert db.get_symbols() == ["SOLUSDT"]

    def test_get_symbols_strips_whitespace(self, db):
        # write dirty directly to verify get_symbols() strips — set_symbols() always writes clean
        db.set_runtime_config(symbols=" BTCUSDT , ETHUSDT ")
        assert db.get_symbols() == ["BTCUSDT", "ETHUSDT"]


class TestOrchestratorSymbolIsolation:
    def test_orchestrator_only_sees_own_symbol_trades(self, db):
        from unittest.mock import patch
        from bot.orchestrator import StrategyOrchestrator
        import pandas as pd

        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")

        orch = StrategyOrchestrator(db=db, symbol="ETHUSDT")

        with patch.object(db, "get_open_trades", wraps=db.get_open_trades) as mock_got:
            n = 100
            df = pd.DataFrame({
                "open": [50000.0] * n, "high": [50100.0] * n,
                "low": [49900.0] * n, "close": [50050.0] * n,
                "volume": [100.0] * n,
            })
            try:
                orch.step(df, 10000.0, None)
            except Exception:
                pass
            mock_got.assert_called_with(symbol="ETHUSDT")


class TestRunCycleUsesOrchestratorSymbol:
    def test_klines_fetched_for_orchestrator_symbol(self, db):
        from unittest.mock import MagicMock, patch
        from bot.orchestrator import StrategyOrchestrator
        import main as main_module

        orch = StrategyOrchestrator(db=db, symbol="ETHUSDT")

        mock_client = MagicMock()
        # First call to get_klines returns None to trigger early return
        mock_client.get_klines.side_effect = [None, None]
        mock_client.get_balance.return_value = 10000.0

        with patch.object(main_module, "_build_client", return_value=mock_client):
            try:
                main_module.run_cycle(orch, db, dry_run=True)
            except Exception:
                # We're only checking the calls made, not the full execution
                pass

        # Verify first two calls (primary timeframe and daily/1d) used ETHUSDT
        calls = [c.args[0] for c in mock_client.get_klines.call_args_list]
        for sym in calls:
            assert sym == "ETHUSDT", f"Expected ETHUSDT but got {sym}"


class TestPositionManagerPerTradeTick:
    def test_each_trade_uses_its_own_symbol_tick(self, db):
        from unittest.mock import patch
        import main as main_module
        import datetime as _dt

        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")

        db.upsert_live_tick("BTCUSDT", 50000.0, 50000.0, 50100.0, 49900.0, 100.0,
                            _dt.datetime.utcnow().isoformat())
        db.upsert_live_tick("ETHUSDT", 3000.0, 3000.0, 3010.0, 2990.0, 500.0,
                            _dt.datetime.utcnow().isoformat())

        calls = []
        original_get_tick = db.get_live_tick

        def tracking_get_tick(symbol):
            calls.append(symbol)
            return original_get_tick(symbol)

        with patch.object(db, "get_live_tick", side_effect=tracking_get_tick):
            with patch.object(main_module, "_manage_single_position"):
                main_module.position_manager(db, dry_run=True)

        assert "BTCUSDT" in calls
        assert "ETHUSDT" in calls
