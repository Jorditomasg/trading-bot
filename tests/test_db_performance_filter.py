import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert_closed(db, symbol, strategy, regime, pnl):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, side, strategy, regime, entry_price, quantity,
                entry_time, exit_price, exit_time, stop_loss, take_profit, pnl, pnl_pct)
               VALUES (?, 'BUY', ?, ?, 100.0, 1.0,
                       '2025-01-01T00:00:00', 110.0, '2025-01-01T01:00:00',
                       95.0, 115.0, ?, ?)""",
            (symbol, strategy, regime, pnl, pnl / 100.0),
        )


class TestGetAllTradesSymbolFilter:
    def test_no_filter_returns_all(self, db):
        _insert_closed(db, "BTCUSDT", "EMA_CROSSOVER", "TRENDING", 5.0)
        _insert_closed(db, "ETHUSDT", "EMA_CROSSOVER", "TRENDING", 3.0)
        assert len(db.get_all_trades()) == 2

    def test_filter_returns_only_matching_symbol(self, db):
        _insert_closed(db, "BTCUSDT", "EMA_CROSSOVER", "TRENDING", 5.0)
        _insert_closed(db, "ETHUSDT", "EMA_CROSSOVER", "TRENDING", 3.0)
        result = db.get_all_trades(symbol="BTCUSDT")
        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"


class TestGetPerformanceByStrategySymbolFilter:
    def test_no_filter_aggregates_across_symbols(self, db):
        _insert_closed(db, "BTCUSDT", "EMA_CROSSOVER", "TRENDING", 5.0)
        _insert_closed(db, "ETHUSDT", "EMA_CROSSOVER", "TRENDING", 3.0)
        rows = db.get_performance_by_strategy()
        assert len(rows) == 1
        assert rows[0]["total_trades"] == 2

    def test_filter_restricts_to_symbol(self, db):
        _insert_closed(db, "BTCUSDT", "EMA_CROSSOVER", "TRENDING", 5.0)
        _insert_closed(db, "ETHUSDT", "EMA_CROSSOVER", "TRENDING", 3.0)
        rows = db.get_performance_by_strategy(symbol="BTCUSDT")
        assert len(rows) == 1
        assert rows[0]["total_trades"] == 1
        assert rows[0]["total_pnl"] == 5.0


class TestGetPerformanceByRegimeSymbolFilter:
    def test_filter_restricts_to_symbol(self, db):
        _insert_closed(db, "BTCUSDT", "EMA_CROSSOVER", "TRENDING", 5.0)
        _insert_closed(db, "ETHUSDT", "EMA_CROSSOVER", "RANGING", -2.0)
        rows = db.get_performance_by_regime(symbol="BTCUSDT")
        assert len(rows) == 1
        assert rows[0]["regime"] == "TRENDING"
