import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert_signal(db, symbol: str, action: str = "BUY") -> None:
    db.insert_signal(
        symbol=symbol, strategy="EMA_CROSSOVER", regime="TRENDING",
        action=action, strength=0.8,
    )


class TestGetRecentSignalsSymbolFilter:
    def test_no_filter_returns_all_symbols(self, db):
        _insert_signal(db, "BTCUSDT")
        _insert_signal(db, "ETHUSDT")
        assert len(db.get_recent_signals(20)) == 2

    def test_symbol_filter_returns_only_matching(self, db):
        _insert_signal(db, "BTCUSDT")
        _insert_signal(db, "ETHUSDT")
        result = db.get_recent_signals(20, symbol="BTCUSDT")
        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"

    def test_symbol_filter_empty_when_no_match(self, db):
        _insert_signal(db, "BTCUSDT")
        result = db.get_recent_signals(20, symbol="SOLUSDT")
        assert result == []

    def test_no_filter_respects_limit(self, db):
        for _ in range(5):
            _insert_signal(db, "BTCUSDT")
        assert len(db.get_recent_signals(3)) == 3

    def test_symbol_filter_respects_limit(self, db):
        for _ in range(5):
            _insert_signal(db, "BTCUSDT")
        result = db.get_recent_signals(2, symbol="BTCUSDT")
        assert len(result) == 2
