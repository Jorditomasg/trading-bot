import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "trading_bot.db")

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
    take_profit REAL    NOT NULL,
    atr         REAL,
    trailing_sl REAL
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

CREATE TABLE IF NOT EXISTS live_ticks (
    symbol    TEXT PRIMARY KEY,
    price     REAL,
    open      REAL,
    high      REAL,
    low       REAL,
    volume    REAL,
    timestamp TEXT
);

CREATE TABLE IF NOT EXISTS adaptive_params (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    strategy    TEXT    NOT NULL,
    param_name  TEXT    NOT NULL,
    old_value   REAL    NOT NULL,
    new_value   REAL    NOT NULL,
    reason      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)
        self._migrate_schema()
        logger.debug("Database schema initialized at %s", self.path)

    def _migrate_schema(self) -> None:
        """Add columns introduced after initial schema creation (safe, idempotent)."""
        with self._conn() as conn:
            trades_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(trades)").fetchall()
            }
            for col, definition in [
                ("atr",         "REAL"),
                ("trailing_sl", "REAL"),
                ("timeframe",   "TEXT DEFAULT '1h'"),
            ]:
                if col not in trades_cols:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
                    logger.info("Migration: added column trades.%s", col)

            signals_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(signals)").fetchall()
            }
            for col, definition in [
                ("bias", "TEXT"),
            ]:
                if col not in signals_cols:
                    conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
                    logger.info("Migration: added column signals.%s", col)

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
        atr: Optional[float] = None,
        timeframe: str = "1h",
    ) -> int:
        ts = (entry_time or datetime.now()).isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO trades
                   (symbol, side, strategy, regime, entry_price, quantity,
                    stop_loss, take_profit, entry_time, atr, timeframe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, side, strategy, regime, entry_price, quantity,
                 stop_loss, take_profit, ts, atr, timeframe),
            )
            trade_id = cursor.lastrowid
        logger.info(
            "Trade inserted id=%s side=%s entry=%.2f timeframe=%s",
            trade_id, side, entry_price, timeframe,
        )
        return trade_id

    def close_trade(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        exit_time: Optional[datetime] = None,
    ) -> None:
        ts = (exit_time or datetime.now()).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT entry_price, quantity, side FROM trades WHERE id = ? AND exit_price IS NULL",
                (trade_id,),
            ).fetchone()
            if row is None:
                logger.warning("close_trade: trade id=%d already closed or not found — no-op", trade_id)
                return

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
                   WHERE id = ? AND exit_price IS NULL""",
                (exit_price, ts, exit_reason, pnl, pnl_pct, trade_id),
            )
        logger.info(
            "Trade closed id=%s exit=%.2f pnl=%.4f (%.2f%%)",
            trade_id, exit_price, pnl, pnl_pct * 100,
        )

    def update_trailing_sl(self, trade_id: int, trailing_sl: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE trades SET trailing_sl = ? WHERE id = ?",
                (trailing_sl, trade_id),
            )
        logger.debug("Trailing SL updated trade_id=%s sl=%.2f", trade_id, trailing_sl)

    def insert_equity_snapshot(self, balance: float, drawdown: float = 0.0) -> None:
        ts = datetime.now().isoformat()
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
        bias: Optional[str] = None,
    ) -> None:
        ts = (timestamp or datetime.now()).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO signals (timestamp, symbol, strategy, regime, action, strength, bias)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, strategy, regime, action, strength, bias),
            )
        logger.debug("Signal recorded action=%s strength=%.2f bias=%s", action, strength, bias)

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

    def get_open_trades(self) -> list[dict]:
        """Return all open trades, ordered by entry_time descending."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trade(self) -> Optional[dict]:
        """Shim for backward compatibility — returns the most recent open trade or None."""
        trades = self.get_open_trades()
        return trades[0] if trades else None

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

    def get_performance_by_regime(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT
                       regime,
                       COUNT(*)                                        AS total_trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS wins,
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)      AS losses,
                       ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) * 100, 2) AS win_rate,
                       ROUND(SUM(pnl), 4)                              AS total_pnl,
                       ROUND(AVG(pnl), 4)                              AS avg_pnl
                   FROM trades
                   WHERE exit_price IS NOT NULL
                   GROUP BY regime"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_live_tick(
        self,
        symbol: str,
        price: float,
        open_: float,
        high: float,
        low: float,
        volume: float,
        timestamp: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO live_ticks
                   (symbol, price, open, high, low, volume, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (symbol, price, open_, high, low, volume, timestamp),
            )
        logger.debug("Live tick upserted symbol=%s price=%.2f", symbol, price)

    def get_live_tick(self, symbol: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM live_ticks WHERE symbol = ?", (symbol,)
            ).fetchone()
        return dict(row) if row else None

    def insert_adaptive_param(
        self,
        strategy: str,
        param_name: str,
        old_value: float,
        new_value: float,
        reason: str,
    ) -> None:
        ts = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO adaptive_params
                   (timestamp, strategy, param_name, old_value, new_value, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ts, strategy, param_name, old_value, new_value, reason),
            )
        logger.debug("Adaptive param: %s.%s %.4f→%.4f (%s)", strategy, param_name, old_value, new_value, reason)

    def get_adaptive_params(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM adaptive_params ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Bot config (key-value store) ──────────────────────────────────────────────

    def get_config(self, key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM bot_config WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
                (key, value),
            )
        logger.debug("Config set: %s", key)

    def get_active_mode(self) -> str:
        return self.get_config("active_mode") or "TESTNET"

    def set_active_mode(self, mode: str) -> None:
        assert mode in ("TESTNET", "MAINNET"), f"Invalid mode: {mode}"
        self.set_config("active_mode", mode)
        logger.info("Active mode set to %s", mode)

    def save_mainnet_credentials(self, api_key_enc: str, api_secret_enc: str) -> None:
        self.set_config("mainnet_api_key", api_key_enc)
        self.set_config("mainnet_api_secret", api_secret_enc)
        logger.info("Mainnet credentials saved (encrypted)")

    def get_mainnet_credentials(self) -> tuple[str, str] | None:
        key    = self.get_config("mainnet_api_key")
        secret = self.get_config("mainnet_api_secret")
        if key and secret:
            return key, secret
        return None

    def has_mainnet_credentials(self) -> bool:
        return self.get_mainnet_credentials() is not None

    # ── Telegram config ───────────────────────────────────────────────────────

    def save_telegram_config(self, token: str, chat_id: str, enabled: bool) -> None:
        self.set_config("telegram_token",   token)
        self.set_config("telegram_chat_id", chat_id)
        self.set_config("telegram_enabled", "1" if enabled else "0")
        logger.info("Telegram config saved (enabled=%s)", enabled)

    def get_telegram_config(self) -> dict:
        return {
            "token":   self.get_config("telegram_token")   or "",
            "chat_id": self.get_config("telegram_chat_id") or "",
            "enabled": self.get_config("telegram_enabled") == "1",
        }

    def has_telegram_config(self) -> bool:
        cfg = self.get_telegram_config()
        return bool(cfg["token"] and cfg["chat_id"])

    # ── Bot pause state ───────────────────────────────────────────────────────

    def get_peak_capital(self) -> float | None:
        val = self.get_config("peak_capital")
        return float(val) if val else None

    def set_peak_capital(self, value: float) -> None:
        self.set_config("peak_capital", f"{value:.4f}")

    def get_bot_paused(self) -> bool:
        return self.get_config("bot_paused") == "1"

    def set_bot_paused(self, paused: bool) -> None:
        self.set_config("bot_paused", "1" if paused else "0")
        logger.info("Bot paused=%s", paused)

    # ── Single trade lookup ───────────────────────────────────────────────────

    def get_trade(self, trade_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        return dict(row) if row else None

    # ── Range queries for export ──────────────────────────────────────────────

    def _get_range(
        self,
        table: str,
        ts_col: str,
        from_dt: str | None,
        to_dt: str | None,
        order: str = "ASC",
    ) -> list[dict]:
        """Generic range query over any table with an ISO timestamp column."""
        conditions: list[str] = []
        params: list[str] = []
        if from_dt:
            conditions.append(f"{ts_col} >= ?")
            params.append(from_dt)
        if to_dt:
            conditions.append(f"{ts_col} <= ?")
            params.append(to_dt)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM {table}{where} ORDER BY {ts_col} {order}"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_trades_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("trades", "entry_time", from_dt, to_dt)

    def get_equity_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("equity", "timestamp", from_dt, to_dt)

    def get_signals_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("signals", "timestamp", from_dt, to_dt)

    def get_adaptive_params_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("adaptive_params", "timestamp", from_dt, to_dt)
