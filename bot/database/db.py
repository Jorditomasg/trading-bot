import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

logger = logging.getLogger(__name__)

DB_PATH = "trading_bot.db"

DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL,
    strategy    TEXT    NOT NULL,
    regime      TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    exit_price  REAL,
    quantity    REAL    NOT NULL,
    pnl         REAL,
    pnl_pct     REAL,
    entry_time  TEXT    NOT NULL,
    exit_time   TEXT,
    exit_reason TEXT,
    stop_loss   REAL    NOT NULL,
    take_profit REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS equity (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    balance   REAL    NOT NULL,
    drawdown  REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    symbol    TEXT    NOT NULL,
    strategy  TEXT    NOT NULL,
    regime    TEXT    NOT NULL,
    action    TEXT    NOT NULL,
    strength  REAL    NOT NULL
);
"""


class Database:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)
        logger.debug("Database schema initialized at %s", self.path)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_trade(
        self,
        symbol: str,
        side: str,
        strategy: str,
        regime: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: float,
        entry_time: Optional[datetime] = None,
    ) -> int:
        ts = (entry_time or datetime.utcnow()).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO trades
                   (symbol, side, strategy, regime, entry_price, quantity,
                    stop_loss, take_profit, entry_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, side, strategy, regime, entry_price, quantity,
                 stop_loss, take_profit, ts),
            )
            trade_id = cursor.lastrowid
        logger.info("Trade inserted id=%s side=%s entry=%.2f", trade_id, side, entry_price)
        return trade_id

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        exit_time: Optional[datetime] = None,
    ) -> None:
        ts = (exit_time or datetime.utcnow()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entry_price, quantity, side FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Trade id={trade_id} not found")

            entry_price: float = row["entry_price"]
            quantity: float = row["quantity"]
            side: str = row["side"]

            if side == "BUY":
                pnl = (exit_price - entry_price) * quantity
            else:
                pnl = (entry_price - exit_price) * quantity

            pnl_pct = pnl / (entry_price * quantity)

            conn.execute(
                """UPDATE trades
                   SET exit_price = ?, exit_time = ?, exit_reason = ?,
                       pnl = ?, pnl_pct = ?
                   WHERE id = ?""",
                (exit_price, ts, exit_reason, pnl, pnl_pct, trade_id),
            )
        logger.info(
            "Trade closed id=%s exit=%.2f pnl=%.4f (%.2f%%)",
            trade_id, exit_price, pnl, pnl_pct * 100,
        )

    def insert_equity_snapshot(self, balance: float, drawdown: float = 0.0) -> None:
        ts = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity (timestamp, balance, drawdown) VALUES (?, ?, ?)",
                (ts, balance, drawdown),
            )
        logger.debug("Equity snapshot balance=%.2f drawdown=%.4f", balance, drawdown)

    def insert_signal(
        self,
        symbol: str,
        strategy: str,
        regime: str,
        action: str,
        strength: float,
        timestamp: Optional[datetime] = None,
    ) -> None:
        ts = (timestamp or datetime.utcnow()).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO signals (timestamp, symbol, strategy, regime, action, strength)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts, symbol, strategy, regime, action, strength),
            )
        logger.debug("Signal recorded action=%s strength=%.2f", action, strength)

    def get_all_trades(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY entry_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_equity_curve(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT timestamp, balance, drawdown FROM equity ORDER BY timestamp"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trade(self) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_performance_by_strategy(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT
                       strategy,
                       COUNT(*)                                        AS total_trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS wins,
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)      AS losses,
                       ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) * 100, 2) AS win_rate,
                       ROUND(SUM(pnl), 4)                              AS total_pnl,
                       ROUND(AVG(pnl), 4)                              AS avg_pnl
                   FROM trades
                   WHERE exit_price IS NOT NULL
                   GROUP BY strategy"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
