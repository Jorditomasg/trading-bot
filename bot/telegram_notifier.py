"""Telegram notification client — reads config from DB, sends via Bot API."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from bot.database.db import Database

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    """Fire-and-forget Telegram notifier. Silently no-ops when disabled or unconfigured."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cfg(self) -> tuple[str, str, bool]:
        cfg = self._db.get_telegram_config()
        return cfg["token"], cfg["chat_id"], cfg["enabled"]

    def _post(self, text: str) -> None:
        token, chat_id, enabled = self._cfg()
        if not enabled or not token or not chat_id:
            return
        try:
            resp = requests.post(
                _API.format(token=token, method="sendMessage"),
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=5,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    @staticmethod
    def _mode_tag(mode: str) -> str:
        return "🔴 MAINNET" if mode == "MAINNET" else "🧪 DEMO"

    # ── Trade events ──────────────────────────────────────────────────────────

    def trade_opened(self, trade: dict, mode: str) -> None:
        side  = trade.get("side", "?")
        emoji = "🟢" if side == "BUY" else "🔴"
        self._post(
            f"{emoji} <b>TRADE OPENED</b>  [{self._mode_tag(mode)}]\n"
            f"Side:     <code>{side}</code>\n"
            f"Entry:    <code>${trade.get('entry_price', 0):,.2f}</code>\n"
            f"Qty:      <code>{trade.get('quantity', 0):.5f}</code>\n"
            f"SL:       <code>${trade.get('stop_loss', 0):,.2f}</code>\n"
            f"TP:       <code>${trade.get('take_profit', 0):,.2f}</code>\n"
            f"Strategy: <code>{trade.get('strategy', '?')}</code>  "
            f"Regime: <code>{trade.get('regime', '?')}</code>"
        )

    def trade_closed(self, trade: dict, pnl: float, exit_reason: str, mode: str) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        sign  = "+" if pnl >= 0 else ""
        self._post(
            f"{emoji} <b>TRADE CLOSED</b>  [{self._mode_tag(mode)}]\n"
            f"Reason:   <code>{exit_reason}</code>\n"
            f"Exit:     <code>${trade.get('exit_price', 0):,.2f}</code>\n"
            f"PnL:      <code>{sign}${pnl:.4f}</code>\n"
            f"Strategy: <code>{trade.get('strategy', '?')}</code>"
        )

    # ── Bot events ────────────────────────────────────────────────────────────

    def circuit_breaker(self, drawdown: float, mode: str) -> None:
        self._post(
            f"⚠️ <b>CIRCUIT BREAKER</b>  [{self._mode_tag(mode)}]\n"
            f"Drawdown: <code>{drawdown * 100:.2f}%</code>\n"
            f"Trading paused — cooldown active."
        )

    def bot_started(self, dry_run: bool, mode: str) -> None:
        suffix = "  <i>(dry-run)</i>" if dry_run else ""
        self._post(f"🤖 <b>BOT STARTED</b>  [{self._mode_tag(mode)}]{suffix}")

    def bot_stopped(self) -> None:
        self._post("🛑 <b>BOT STOPPED</b>")

    # ── Command responses ─────────────────────────────────────────────────────

    def paused(self) -> None:
        self._post("⏸ <b>BOT PAUSED</b>  (via Telegram)")

    def resumed(self) -> None:
        self._post("▶️ <b>BOT RESUMED</b>  (via Telegram)")

    def status(self, balance: float, open_trade: dict | None, mode: str) -> None:
        if open_trade:
            pos = (
                f"{open_trade['side']} @ <code>${open_trade['entry_price']:,.2f}</code>\n"
                f"SL <code>${open_trade['stop_loss']:,.2f}</code>  "
                f"TP <code>${open_trade['take_profit']:,.2f}</code>"
            )
        else:
            pos = "No open position"
        self._post(
            f"📊 <b>STATUS</b>  [{self._mode_tag(mode)}]\n"
            f"Balance: <code>${balance:,.2f}</code>\n"
            f"{pos}"
        )

    # ── Static test helper (used by dashboard) ────────────────────────────────

    @staticmethod
    def test_send(token: str, chat_id: str) -> tuple[bool, str]:
        """Test connectivity with given credentials without DB. Returns (ok, message)."""
        try:
            resp = requests.post(
                _API.format(token=token.strip(), method="sendMessage"),
                json={
                    "chat_id": chat_id.strip(),
                    "text": "🤖 Trading Bot — test notification ✅",
                },
                timeout=5,
            )
            resp.raise_for_status()
            return True, "Message sent."
        except requests.HTTPError as exc:
            return False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        except Exception as exc:
            return False, str(exc)
