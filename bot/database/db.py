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
    trailing_sl REAL,
    peak_price  REAL
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

CREATE TABLE IF NOT EXISTS optimizer_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    timeframe     TEXT    NOT NULL,
    period_days   INTEGER NOT NULL,
    ema_stop_mult REAL    NOT NULL,
    ema_tp_mult   REAL    NOT NULL,
    profit_factor REAL    NOT NULL,
    sharpe_ratio  REAL    NOT NULL,
    win_rate      REAL    NOT NULL,
    max_drawdown  REAL    NOT NULL,
    total_trades  INTEGER NOT NULL,
    total_pnl     REAL    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS entry_quality_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    timeframe      TEXT    NOT NULL,
    period_days    INTEGER NOT NULL,
    vol_mult       REAL    NOT NULL,
    bar_direction  INTEGER NOT NULL,
    ema_momentum   INTEGER NOT NULL,
    min_atr_pct    REAL    NOT NULL,
    max_distance_atr REAL,
    ema_stop_mult  REAL    NOT NULL,
    ema_tp_mult    REAL    NOT NULL,
    profit_factor  REAL    NOT NULL,
    sharpe_ratio   REAL    NOT NULL,
    win_rate       REAL    NOT NULL,
    max_drawdown   REAL    NOT NULL,
    total_trades   INTEGER NOT NULL,
    total_pnl      REAL    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_exit_price  ON trades(exit_price);
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
                ("peak_price",  "REAL"),
            ]:
                if col not in trades_cols:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
                    logger.info("Migration: added column trades.%s", col)

            signals_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(signals)").fetchall()
            }
            for col, definition in [
                ("bias",     "TEXT"),
                ("momentum", "TEXT"),
            ]:
                if col not in signals_cols:
                    conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {definition}")
                    logger.info("Migration: added column signals.%s", col)

            # entry_quality_runs — max_distance_atr column
            eq_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(entry_quality_runs)").fetchall()
            }
            if "max_distance_atr" not in eq_cols:
                conn.execute("ALTER TABLE entry_quality_runs ADD COLUMN max_distance_atr REAL")
                logger.info("Migration: added column entry_quality_runs.max_distance_atr")

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
        momentum: Optional[str] = None,
    ) -> None:
        ts = (timestamp or datetime.now()).isoformat()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO signals
                   (timestamp, symbol, strategy, regime, action, strength, bias, momentum)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, strategy, regime, action, strength, bias, momentum),
            )
        logger.debug(
            "Signal recorded action=%s strength=%.2f bias=%s momentum=%s",
            action, strength, bias, momentum,
        )

    def get_all_trades(self, symbol: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if symbol is None:
                rows = conn.execute(
                    "SELECT * FROM trades ORDER BY entry_time DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE symbol = ? ORDER BY entry_time DESC",
                    (symbol,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_equity_curve(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT timestamp, balance, drawdown FROM equity ORDER BY timestamp"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_symbol_equity_curve(self, symbol: str, initial_capital: float) -> list[dict]:
        """Derived equity curve for a single symbol.

        The exchange balance is shared across symbols, so per-symbol equity is
        computed from the cumulative realised PnL of that symbol's closed trades
        starting at `initial_capital`. Open trades are not included.
        """
        from bot.metrics import derive_equity_curve
        trades = self.get_all_trades(symbol=symbol)
        # get_all_trades returns DESC by entry_time; derive_equity_curve sorts by exit_time itself.
        return derive_equity_curve(trades, initial_capital)

    def get_open_trades(self, symbol: str | None = None) -> list[dict]:
        """Return open trades, optionally filtered by symbol, ordered by entry_time descending."""
        with self._conn() as conn:
            if symbol is None:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trades WHERE exit_price IS NULL AND symbol = ?"
                    " ORDER BY entry_time DESC",
                    (symbol,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_open_trade(self) -> Optional[dict]:
        """Shim for backward compatibility — returns the most recent open trade or None."""
        trades = self.get_open_trades()
        return trades[0] if trades else None

    def get_performance_by_strategy(self, symbol: str | None = None) -> list[dict]:
        base_sql = """SELECT
                       strategy,
                       COUNT(*)                                        AS total_trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS wins,
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)      AS losses,
                       ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) * 100, 2) AS win_rate,
                       ROUND(SUM(pnl), 4)                              AS total_pnl,
                       ROUND(AVG(pnl), 4)                              AS avg_pnl
                   FROM trades
                   WHERE exit_price IS NOT NULL"""
        with self._conn() as conn:
            if symbol is None:
                rows = conn.execute(base_sql + " GROUP BY strategy").fetchall()
            else:
                rows = conn.execute(
                    base_sql + " AND symbol = ? GROUP BY strategy", (symbol,)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_kelly_stats(self, strategy_name: str, min_trades: int = 15) -> dict | None:
        """Compute Kelly Criterion statistics for a strategy.

        Returns dict with keys:
          - total_trades: count of closed trades
          - win_rate: fraction of profitable trades (0.0-1.0)
          - avg_win_pct: average PnL% on winning trades (decimal, e.g. 0.02 = 2%)
          - avg_loss_pct: average ABS(PnL%) on losing trades (always positive)

        Returns None if:
          - fewer than min_trades closed trades exist
          - no winning trades (avg_win_pct is NULL, meaning all losses)
        """
        with self._conn() as conn:
            row = conn.execute(
                """SELECT
                       COUNT(*)                                                  AS total_trades,
                       AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END)            AS win_rate,
                       AVG(CASE WHEN pnl > 0 THEN pnl_pct ELSE NULL END)        AS avg_win_pct,
                       AVG(CASE WHEN pnl <= 0 THEN ABS(pnl_pct) ELSE NULL END)  AS avg_loss_pct
                   FROM trades
                   WHERE exit_price IS NOT NULL
                     AND strategy = ?""",
                (strategy_name,),
            ).fetchone()

        if row is None or row["total_trades"] < min_trades:
            return None
        if row["avg_win_pct"] is None or row["avg_loss_pct"] is None:
            return None

        return {
            "total_trades": int(row["total_trades"]),
            "win_rate":     float(row["win_rate"]),
            "avg_win_pct":  float(row["avg_win_pct"]),
            "avg_loss_pct": float(row["avg_loss_pct"]),
        }

    def get_performance_by_regime(self, symbol: str | None = None) -> list[dict]:
        base_sql = """SELECT
                       regime,
                       COUNT(*)                                        AS total_trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS wins,
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END)      AS losses,
                       ROUND(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0 END) * 100, 2) AS win_rate,
                       ROUND(SUM(pnl), 4)                              AS total_pnl,
                       ROUND(AVG(pnl), 4)                              AS avg_pnl
                   FROM trades
                   WHERE exit_price IS NOT NULL"""
        with self._conn() as conn:
            if symbol is None:
                rows = conn.execute(base_sql + " GROUP BY regime").fetchall()
            else:
                rows = conn.execute(
                    base_sql + " AND symbol = ? GROUP BY regime", (symbol,)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_signals(self, limit: int = 20, symbol: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if symbol is None:
                rows = conn.execute(
                    "SELECT * FROM signals ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM signals WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
                    (symbol, limit),
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

    def get_account_baseline(self) -> float | None:
        """Return the persisted account baseline (USDT trading-start balance) or None if unset.

        None signals 'needs lazy seed'. Float parsing matches get_peak_capital().
        """
        val = self.get_config("account_baseline")
        return float(val) if val else None

    def set_account_baseline(self, value: float) -> None:
        """Persist the account baseline. Idempotent; overwrites any prior value.

        Used by the lazy seeder on first Phase 2 run and by future manual overrides.
        """
        self.set_config("account_baseline", f"{value:.4f}")

    def get_closed_pnl_sum(self, symbol: str | None = None) -> float:
        """Sum of ``pnl`` for closed trades (exit_price IS NOT NULL).

        symbol=None  → cross-symbol total (used by trading_equity computation).
        symbol=<str> → single-symbol total (for per-symbol analytics).

        Returns float; never None.
        """
        with self._conn() as conn:
            if symbol is None:
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE exit_price IS NOT NULL"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades"
                    " WHERE exit_price IS NOT NULL AND symbol = ?",
                    (symbol,),
                ).fetchone()
        return float(row["total"])

    def reset_peak_capital(
        self,
        value: float | None = None,
        clear_breaker: bool = True,
    ) -> tuple[float, float]:
        """Reset HWM and optionally clear all per-symbol breaker-cooldown rows.

        Returns ``(old_peak, new_peak)`` so the Telegram handler can surface a diff.

        Behaviour:
          - value=None      → new_peak = account_baseline + closed_pnl_sum
                              Falls back to 0.0 if baseline is unset.
          - value=<float>   → new_peak = value (explicit override).
          - clear_breaker=True → DELETE every bot_config row matching
                                 'breaker_triggered_at_%'.

        All writes occur in ONE ``_conn()`` transaction for consistency.
        See gotcha #31 for the semantic shift of peak_capital (May 2026).
        """
        old_peak: float = self.get_peak_capital() or 0.0

        if value is None:
            baseline = self.get_account_baseline() or 0.0
            closed_pnl = self.get_closed_pnl_sum()
            new_peak: float = baseline + closed_pnl
        else:
            new_peak = float(value)

        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
                ("peak_capital", f"{new_peak:.4f}"),
            )
            if clear_breaker:
                conn.execute(
                    "DELETE FROM bot_config WHERE key LIKE 'breaker_triggered_at_%'"
                )

        logger.info(
            "peak_capital reset: old=%.4f new=%.4f (clear_breaker=%s)",
            old_peak, new_peak, clear_breaker,
        )
        return old_peak, new_peak

    def get_bot_paused(self) -> bool:
        return self.get_config("bot_paused") == "1"

    def set_bot_paused(self, paused: bool) -> None:
        self.set_config("bot_paused", "1" if paused else "0")
        logger.info("Bot paused=%s", paused)

    # ── Single trade lookup ───────────────────────────────────────────────────

    # ── Runtime config (hot-reloadable bot parameters) ───────────────────────

    def get_runtime_config(self) -> dict:
        """Return all runtime-configurable bot parameters from the KV store.

        Reads every key stored with the ``rt_`` prefix and strips the prefix
        so callers receive bare names (e.g. ``"ema_stop_mult"``, not ``"rt_ema_stop_mult"``).
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT key, value FROM bot_config WHERE key LIKE 'rt_%'"
            ).fetchall()
        return {row["key"][3:]: row["value"] for row in rows}

    def set_runtime_config(self, **kwargs) -> None:
        """Persist runtime config params to the KV store (rt_ prefix)."""
        for key, value in kwargs.items():
            self.set_config(f"rt_{key}", str(value))
        logger.info("Runtime config updated: %s", list(kwargs.keys()))

    def get_symbols(self) -> list[str]:
        """Return the configured symbol list from bot_config. Empty list if not set."""
        cfg = self.get_runtime_config()
        raw = cfg.get("symbols", "")
        if not raw:
            return []
        return [s.strip() for s in raw.split(",") if s.strip()]

    def get_backtest_cost_per_side(self) -> float:
        """Return per-side fee+slippage assumed in backtests. Default 0.001 (0.10%)."""
        cfg = self.get_runtime_config()
        try:
            return float(cfg.get("backtest_cost_per_side", "0.001"))
        except (TypeError, ValueError):
            return 0.001

    def set_symbols(self, symbols: list[str]) -> None:
        """Persist the symbol list to bot_config."""
        self.set_runtime_config(symbols=",".join(symbols))

    def insert_optimizer_run(
        self,
        symbol: str,
        timeframe: str,
        period_days: int,
        ema_stop_mult: float,
        ema_tp_mult: float,
        profit_factor: float,
        sharpe_ratio: float,
        win_rate: float,
        max_drawdown: float,
        total_trades: int,
        total_pnl: float,
        status: str = "pending",
    ) -> int:
        ts = datetime.now().isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO optimizer_runs
                   (timestamp, symbol, timeframe, period_days, ema_stop_mult, ema_tp_mult,
                    profit_factor, sharpe_ratio, win_rate, max_drawdown, total_trades, total_pnl, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, timeframe, period_days, ema_stop_mult, ema_tp_mult,
                 profit_factor, sharpe_ratio, win_rate, max_drawdown, total_trades, total_pnl, status),
            )
            return cursor.lastrowid

    def get_optimizer_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM optimizer_runs ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_best_pending_optimizer_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM optimizer_runs WHERE status = 'pending'
                   ORDER BY profit_factor DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def set_optimizer_run_status(self, run_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE optimizer_runs SET status = ? WHERE id = ?", (status, run_id)
            )
        logger.info("Optimizer run id=%d status=%s", run_id, status)

    # ── Entry Quality Optimizer ───────────────────────────────────────────────

    def insert_entry_quality_run(
        self,
        symbol: str,
        timeframe: str,
        period_days: int,
        vol_mult: float,
        bar_direction: bool,
        ema_momentum: bool,
        min_atr_pct: float,
        ema_stop_mult: float,
        ema_tp_mult: float,
        profit_factor: float,
        sharpe_ratio: float,
        win_rate: float,
        max_drawdown: float,
        total_trades: int,
        total_pnl: float,
        status: str = "pending",
        max_distance_atr: float | None = None,
    ) -> int:
        ts = datetime.now().isoformat()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO entry_quality_runs
                   (timestamp, symbol, timeframe, period_days,
                    vol_mult, bar_direction, ema_momentum, min_atr_pct,
                    max_distance_atr,
                    ema_stop_mult, ema_tp_mult,
                    profit_factor, sharpe_ratio, win_rate, max_drawdown,
                    total_trades, total_pnl, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, timeframe, period_days,
                 vol_mult, int(bar_direction), int(ema_momentum), min_atr_pct,
                 max_distance_atr,
                 ema_stop_mult, ema_tp_mult,
                 profit_factor, sharpe_ratio, win_rate, max_drawdown,
                 total_trades, total_pnl, status),
            )
            return cursor.lastrowid

    def get_entry_quality_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM entry_quality_runs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("max_distance_atr") is None:
                d["max_distance_atr"] = 0.5
            result.append(d)
        return result

    def get_best_pending_entry_quality_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM entry_quality_runs
                   WHERE status = 'pending'
                   ORDER BY profit_factor DESC LIMIT 1""",
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("max_distance_atr") is None:
            d["max_distance_atr"] = 0.5
        return d

    def set_entry_quality_run_status(self, run_id: int, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE entry_quality_runs SET status = ? WHERE id = ?",
                (status, run_id),
            )
        logger.info("Entry quality run id=%d status=%s", run_id, status)

    def consume_restart_request(self) -> bool:
        """Returns True (and clears the flag) if a dashboard restart was requested."""
        if self.get_config("restart_requested") == "1":
            self.set_config("restart_requested", "0")
            logger.info("Restart request consumed")
            return True
        return False

    def get_trade(self, trade_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        return dict(row) if row else None

    # ── Range queries for export ──────────────────────────────────────────────

    _ALLOWED_RANGE_TABLES = frozenset(
        {"trades", "equity", "signals", "adaptive_params"}
    )
    _ALLOWED_RANGE_COLS = frozenset(
        {"entry_time", "exit_time", "timestamp"}
    )
    _ALLOWED_ORDERS = frozenset({"ASC", "DESC"})

    # Tables that have a symbol column and accept symbol filtering
    _SYMBOL_AWARE_TABLES = frozenset({"trades", "signals"})

    def _get_range(
        self,
        table: str,
        ts_col: str,
        from_dt: str | None,
        to_dt: str | None,
        order: str = "ASC",
        symbol: str | None = None,
    ) -> list[dict]:
        """Generic range query over any table with an ISO timestamp column.

        When `symbol` is provided AND the table has a symbol column, results are
        filtered to that symbol. For tables without a symbol column the parameter
        is silently ignored (the caller may pass it uniformly across datasets).
        """
        if table not in self._ALLOWED_RANGE_TABLES:
            raise ValueError(f"_get_range: table '{table}' is not allowed")
        if ts_col not in self._ALLOWED_RANGE_COLS:
            raise ValueError(f"_get_range: column '{ts_col}' is not allowed")
        order = order.upper()
        if order not in self._ALLOWED_ORDERS:
            raise ValueError(f"_get_range: order '{order}' is not allowed")

        conditions: list[str] = []
        params: list[str] = []
        if from_dt:
            conditions.append(f"{ts_col} >= ?")
            params.append(from_dt)
        if to_dt:
            conditions.append(f"{ts_col} <= ?")
            params.append(to_dt)
        if symbol and table in self._SYMBOL_AWARE_TABLES:
            conditions.append("symbol = ?")
            params.append(symbol)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM {table}{where} ORDER BY {ts_col} {order}"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_trades_range(
        self,
        from_dt: str | None = None,
        to_dt: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        return self._get_range("trades", "entry_time", from_dt, to_dt, symbol=symbol)

    def get_equity_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("equity", "timestamp", from_dt, to_dt)

    def get_signals_range(
        self,
        from_dt: str | None = None,
        to_dt: str | None = None,
        symbol: str | None = None,
    ) -> list[dict]:
        return self._get_range("signals", "timestamp", from_dt, to_dt, symbol=symbol)

    def get_adaptive_params_range(
        self, from_dt: str | None = None, to_dt: str | None = None
    ) -> list[dict]:
        return self._get_range("adaptive_params", "timestamp", from_dt, to_dt)
