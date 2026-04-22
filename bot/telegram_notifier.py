"""Telegram notification client — reads config from DB, sends via Bot API."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import requests

from bot.metrics import sharpe_ratio, max_drawdown, profit_factor, max_consecutive_losses

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

    # ── Setup ─────────────────────────────────────────────────────────────────

    def register_commands(self) -> None:
        """Register bot commands with Telegram so they appear in the chat UI menu."""
        token, _, _ = self._cfg()
        if not token:
            return
        commands = [
            {"command": "status", "description": "Balance actual y posición abierta"},
            {"command": "report", "description": "Resumen histórico completo"},
            {"command": "pause",  "description": "Pausar el bot (no nuevas entradas)"},
            {"command": "resume", "description": "Reanudar el bot"},
        ]
        try:
            resp = requests.post(
                _API.format(token=token, method="setMyCommands"),
                json={"commands": commands},
                timeout=5,
            )
            resp.raise_for_status()
            logger.info("Telegram commands registered")
        except Exception as exc:
            logger.warning("setMyCommands failed: %s", exc)

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

    def optimizer_applied(self, old_params: dict, new_params: dict, mode: str) -> None:
        """Notify when the auto-optimizer applies new EMA parameters."""
        self._post(
            f"⚙️ <b>AUTO-OPTIMIZER APPLIED</b>  [{self._mode_tag(mode)}]\n\n"
            f"SL mult: <code>{old_params['ema_stop_mult']:.2f}</code> → "
            f"<b>{new_params['ema_stop_mult']:.2f}</b>\n"
            f"TP mult: <code>{old_params['ema_tp_mult']:.2f}</code> → "
            f"<b>{new_params['ema_tp_mult']:.2f}</b>\n\n"
            f"PF:     <b>{new_params['profit_factor']:.2f}</b>  "
            f"Sharpe: <b>{new_params['sharpe_ratio']:.2f}</b>  "
            f"WR: <b>{new_params['win_rate']:.1f}%</b>\n"
            f"Trades: {new_params['total_trades']}  "
            f"MaxDD:  {new_params['max_drawdown']:.1f}%"
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

    def status(self, balance: float, open_trade: dict | None, mode: str, *, paused: bool = False) -> None:
        if open_trade:
            pos = (
                f"{open_trade['side']} @ <code>${open_trade['entry_price']:,.2f}</code>\n"
                f"SL <code>${open_trade['stop_loss']:,.2f}</code>  "
                f"TP <code>${open_trade['take_profit']:,.2f}</code>"
            )
        else:
            pos = "No open position"
        bot_state = "⏸ Paused" if paused else "▶️ Running"
        self._post(
            f"📊 <b>STATUS</b>  [{self._mode_tag(mode)}]\n"
            f"Balance: <code>${balance:,.2f}</code>\n"
            f"Bot:     <code>{bot_state}</code>\n"
            f"{pos}"
        )

    def report(
        self,
        closed_trades: list[dict],
        equity_curve: list[dict],
        perf_by_strategy: list[dict],
        balance: float,
        mode: str,
        initial_capital: float,
    ) -> None:
        if not closed_trades:
            self._post(
                f"📈 <b>REPORT</b>  [{self._mode_tag(mode)}]\n"
                f"Balance: <code>${balance:,.2f}</code>\n"
                f"No closed trades yet."
            )
            return

        total     = len(closed_trades)
        wins      = sum(1 for t in closed_trades if t.get("pnl", 0) > 0)
        win_rate  = wins / total * 100
        total_pnl = sum(t.get("pnl", 0) for t in closed_trades)
        pnl_pct   = (total_pnl / initial_capital * 100) if initial_capital > 0 else 0.0
        sign      = "+" if total_pnl >= 0 else ""

        pf     = profit_factor(closed_trades)
        sr     = sharpe_ratio(equity_curve)
        md     = max_drawdown(equity_curve) * 100
        streak = max_consecutive_losses(closed_trades)

        best = max(perf_by_strategy, key=lambda x: x["win_rate"]) if perf_by_strategy else None
        best_line = (
            f"\nBest strategy: <code>{best['strategy']}</code>  "
            f"({best['win_rate']:.1f}% WR, {best['total_trades']} trades)"
            if best else ""
        )
        pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"

        self._post(
            f"📈 <b>REPORT</b>  [{self._mode_tag(mode)}]\n\n"
            f"Balance:         <code>${balance:,.2f}  ({sign}{pnl_pct:.2f}%)</code>\n"
            f"Trades:          <code>{total} closed  |  Win rate: {win_rate:.1f}%</code>\n"
            f"PnL total:       <code>{sign}${total_pnl:.2f}</code>\n"
            f"Profit factor:   <code>{pf_str}</code>\n"
            f"Sharpe ratio:    <code>{sr:.2f}</code>\n"
            f"Max drawdown:    <code>{md:.2f}%</code>\n"
            f"Max loss streak: <code>{streak}</code>"
            f"{best_line}"
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
