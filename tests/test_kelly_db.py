# tests/test_kelly_db.py
import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "test.db")
    d = Database(path)
    return d


def _insert_closed_trade(db: Database, strategy: str, side: str, pnl: float, pnl_pct: float):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, side, strategy, regime, entry_price, exit_price,
                quantity, pnl, pnl_pct, entry_time, exit_time, exit_reason,
                stop_loss, take_profit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("BTCUSDT", side, strategy, "TRENDING", 50000.0, 51000.0,
             0.001, pnl, pnl_pct, "2025-01-01T00:00:00", "2025-01-02T00:00:00",
             "TAKE_PROFIT", 49000.0, 52000.0),
        )


class TestGetKellyStats:
    def test_returns_none_below_min_trades(self, db):
        for i in range(10):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        result = db.get_kelly_stats("EMA_CROSSOVER", min_trades=15)
        assert result is None

    def test_returns_stats_at_min_trades(self, db):
        for i in range(10):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        for i in range(5):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", -50.0, -0.01)
        result = db.get_kelly_stats("EMA_CROSSOVER", min_trades=15)
        assert result is not None

    def test_returns_none_for_unknown_strategy(self, db):
        result = db.get_kelly_stats("NONEXISTENT", min_trades=1)
        assert result is None

    def test_correct_win_rate(self, db):
        for _ in range(10):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        for _ in range(5):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", -50.0, -0.01)
        result = db.get_kelly_stats("EMA_CROSSOVER", min_trades=15)
        assert result is not None
        assert abs(result["win_rate"] - 10/15) < 1e-6

    def test_correct_avg_win_pct(self, db):
        for _ in range(10):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        for _ in range(5):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", -50.0, -0.01)
        result = db.get_kelly_stats("EMA_CROSSOVER", min_trades=15)
        assert abs(result["avg_win_pct"] - 0.02) < 1e-6

    def test_correct_avg_loss_pct(self, db):
        for _ in range(10):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        for _ in range(5):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", -50.0, -0.01)
        result = db.get_kelly_stats("EMA_CROSSOVER", min_trades=15)
        # avg_loss_pct is ABS value of pnl_pct on losing trades
        assert abs(result["avg_loss_pct"] - 0.01) < 1e-6

    def test_strategy_isolation(self, db):
        for _ in range(20):
            _insert_closed_trade(db, "EMA_CROSSOVER", "BUY", 100.0, 0.02)
        result = db.get_kelly_stats("MEAN_REVERSION", min_trades=1)
        assert result is None

    def test_returns_none_when_all_losses_no_avg_win(self, db):
        for _ in range(20):
            _insert_closed_trade(db, "BREAKOUT", "BUY", -50.0, -0.01)
        result = db.get_kelly_stats("BREAKOUT", min_trades=15)
        assert result is None
