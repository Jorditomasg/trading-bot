"""Telegram command handler — long-polls for /pause, /resume, /status in a daemon thread."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

from bot.database.db import Database
from bot.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramCommandHandler:
    """Background thread that receives bot commands via Telegram long-polling.

    Supported commands:
      /pause   — set bot_paused=True in DB (run_cycle will skip execution)
      /resume  — set bot_paused=False
      /status  — reply with current balance, pause state, and open position
      /report  — reply with full historical performance summary
    """

    def __init__(
        self,
        db: Database,
        notifier: TelegramNotifier,
        price_fetcher: Callable[[str], float] | None = None,
    ) -> None:
        """price_fetcher: callable that takes a symbol and returns its current price."""
        self._db            = db
        self._notifier      = notifier
        self._price_fetcher = price_fetcher
        self._offset        = 0
        self._stop          = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._poll_loop, name="tg-commands", daemon=True
        )
        self._thread.start()
        logger.info("Telegram command handler started")

    def stop(self) -> None:
        self._stop.set()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cfg(self) -> tuple[str, str, bool]:
        cfg = self._db.get_telegram_config()
        return cfg["token"], cfg["chat_id"], cfg["enabled"]

    def _get_updates(self, token: str) -> list[dict]:
        try:
            resp = requests.get(
                _API.format(token=token, method="getUpdates"),
                params={
                    "offset":          self._offset,
                    "timeout":         30,
                    "allowed_updates": ["message"],
                },
                timeout=35,
            )
            return resp.json().get("result", [])
        except Exception as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            return []

    def _handle(self, update: dict, allowed_chat_id: str) -> None:
        msg     = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != allowed_chat_id:
            logger.debug("Ignoring message from unknown chat %s", chat_id)
            return

        raw     = msg.get("text", "").strip()
        command = raw.split()[0].lower().split("@")[0] if raw else ""

        if command == "/pause":
            self._db.set_bot_paused(True)
            self._notifier.paused()
            logger.info("Bot paused via Telegram command")

        elif command == "/resume":
            self._db.set_bot_paused(False)
            self._notifier.resumed()
            logger.info("Bot resumed via Telegram command")

        elif command == "/status":
            curve       = self._db.get_equity_curve()
            balance     = curve[-1]["balance"] if curve else 0.0
            open_trades = self._db.get_open_trades()
            paused      = self._db.get_bot_paused()
            mode        = self._db.get_active_mode()

            positions: list[dict] = []
            for trade in open_trades:
                sym   = trade["symbol"]
                price: float | None = None
                pnl:   float | None = None

                if self._price_fetcher:
                    try:
                        price = self._price_fetcher(sym)
                    except Exception as exc:
                        logger.warning("Could not fetch price for %s in /status: %s", sym, exc)

                if price is not None:
                    side  = trade.get("side", "BUY")
                    entry = float(trade.get("entry_price", 0.0))
                    qty   = float(trade.get("quantity", 0.0))
                    pnl = (price - entry) * qty if side == "BUY" else (entry - price) * qty

                positions.append({
                    "symbol":          sym,
                    "side":            trade["side"],
                    "entry_price":     trade["entry_price"],
                    "stop_loss":       trade["stop_loss"],
                    "take_profit":     trade["take_profit"],
                    "current_price":   price,
                    "unrealized_pnl":  pnl,
                })

            self._notifier.status(balance, positions, mode, paused=paused)

        elif command == "/report":
            from bot.config import settings

            # Parse optional symbol argument: /report BTCUSDT
            parts        = raw.split()
            symbol_arg   = parts[1].upper() if len(parts) > 1 else None
            known        = set(self._db.get_symbols() or [])
            if symbol_arg and known and symbol_arg not in known:
                # Unknown symbol — surface a hint, don't silently ignore
                self._notifier._post(
                    f"Symbol <code>{symbol_arg}</code> is not active. "
                    f"Try one of: <code>{', '.join(sorted(known))}</code>"
                )
                return

            trades  = self._db.get_all_trades(symbol=symbol_arg)
            closed  = [t for t in trades if t.get("exit_price") is not None]
            curve   = self._db.get_equity_curve()
            perf    = self._db.get_performance_by_strategy(symbol=symbol_arg)
            balance = curve[-1]["balance"] if curve else 0.0
            mode    = self._db.get_active_mode()

            # When showing the global report, attach a per-symbol breakdown
            breakdown: list[dict] | None = None
            if symbol_arg is None and known:
                breakdown = []
                for sym in sorted(known):
                    sym_trades = [t for t in closed if t.get("symbol") == sym]
                    if not sym_trades:
                        continue
                    wins      = sum(1 for t in sym_trades if t.get("pnl", 0) > 0)
                    total     = len(sym_trades)
                    total_pnl = sum(t.get("pnl", 0) for t in sym_trades)
                    breakdown.append({
                        "symbol":    sym,
                        "total":     total,
                        "wins":      wins,
                        "win_rate":  (wins / total * 100) if total else 0.0,
                        "total_pnl": total_pnl,
                    })

            self._notifier.report(
                closed, curve, perf, balance, mode, settings.initial_capital,
                symbol=symbol_arg,
                symbols_breakdown=breakdown,
            )
            logger.info("Report sent via Telegram command (symbol=%s)", symbol_arg or "ALL")

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            token, chat_id, enabled = self._cfg()

            if not enabled or not token or not chat_id:
                # Config not ready — sleep and retry
                self._stop.wait(timeout=30)
                continue

            updates = self._get_updates(token)
            for update in updates:
                self._offset = update["update_id"] + 1
                self._handle(update, chat_id)

            if not updates:
                time.sleep(1)
