"""Telegram notification client — reads config from DB, sends via Bot API."""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import requests

from bot.metrics import sharpe_ratio, max_drawdown, profit_factor, max_consecutive_losses

if TYPE_CHECKING:
    from bot.database.db import Database

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

# ── Decimal formatting (respects DECIMAL_SEPARATOR env var) ───────────────────
_COMMA_DECIMAL = os.getenv("DECIMAL_SEPARATOR", "dot").lower() == "comma"


def _fmt(value: float, spec: str = ",.2f") -> str:
    """Format a number respecting DECIMAL_SEPARATOR. dot→1,234.56  comma→1.234,56"""
    s = format(value, spec)
    if not _COMMA_DECIMAL:
        return s
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


class TelegramNotifier:
    """Fire-and-forget Telegram notifier. Silently no-ops when disabled or unconfigured."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Internal ──────────────────────────────────────────────────────────────

    def _cfg(self) -> tuple[str, str, bool]:
        cfg = self._db.get_telegram_config()
        return cfg["token"], cfg["chat_id"], cfg["enabled"]

    def _post(self, text: str, reply_markup: dict | None = None) -> None:
        token, chat_id, enabled = self._cfg()
        if not enabled or not token or not chat_id:
            return
        try:
            payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            resp = requests.post(
                _API.format(token=token, method="sendMessage"),
                json=payload,
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
            {"command": "status", "description": "Balance y posiciones abiertas (todos los símbolos)"},
            {"command": "report", "description": "Resumen histórico — opcional: /report SYMBOL"},
            {"command": "help",   "description": "Mostrar todos los comandos disponibles"},
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

    # ── Generic alert ─────────────────────────────────────────────────────────

    def alert(self, message: str) -> None:
        """Public alert channel for operational warnings (orphan positions, DB
        failures, anything the human needs to see).

        The message is prepended with an `⚠️ ALERT` header and sent via the
        same `_post` machinery, so HTML formatting is supported and the call
        silently no-ops when Telegram is unconfigured. Errors during the HTTP
        send are swallowed by `_post` — callers do not need their own try/except.
        """
        self._post(f"⚠️ <b>ALERT</b>\n{message}")

    # ── Trade events ──────────────────────────────────────────────────────────

    def trade_opened(self, trade: dict, mode: str) -> None:
        side   = trade.get("side", "?")
        symbol = trade.get("symbol", "?")
        emoji  = "🟢" if side == "BUY" else "🔴"
        self._post(
            f"{emoji} <b>TRADE OPENED</b>  <code>{symbol}</code>  [{self._mode_tag(mode)}]\n"
            f"Side:     <code>{side}</code>\n"
            f"Entry:    <code>${_fmt(trade.get('entry_price', 0))}</code>\n"
            f"Qty:      <code>{trade.get('quantity', 0):.5f}</code>\n"
            f"SL:       <code>${_fmt(trade.get('stop_loss', 0))}</code>\n"
            f"TP:       <code>${_fmt(trade.get('take_profit', 0))}</code>\n"
            f"Strategy: <code>{trade.get('strategy', '?')}</code>  "
            f"Regime: <code>{trade.get('regime', '?')}</code>"
        )

    def trade_closed(self, trade: dict, pnl: float, exit_reason: str, mode: str) -> None:
        symbol = trade.get("symbol", "?")
        emoji  = "✅" if pnl >= 0 else "❌"
        sign   = "+" if pnl >= 0 else ""
        self._post(
            f"{emoji} <b>TRADE CLOSED</b>  <code>{symbol}</code>  [{self._mode_tag(mode)}]\n"
            f"Reason:   <code>{exit_reason}</code>\n"
            f"Exit:     <code>${_fmt(trade.get('exit_price', 0))}</code>\n"
            f"PnL:      <code>{sign}${_fmt(pnl, '.4f')}</code>\n"
            f"Strategy: <code>{trade.get('strategy', '?')}</code>"
        )

    # ── Bot events ────────────────────────────────────────────────────────────

    def circuit_breaker(self, drawdown: float, mode: str) -> None:
        self._post(
            f"⚠️ <b>CIRCUIT BREAKER</b>  [{self._mode_tag(mode)}]\n"
            f"Drawdown: <code>{drawdown * 100:.2f}%</code>\n"
            f"Trading paused — cooldown active."
        )

    def hwm_reset(self, old_peak: float, new_peak: float, mode: str) -> None:
        """Confirmation message sent in response to /reset_hwm.

        Format includes old and new HWM values in USDT, the mode tag
        (🧪 DEMO / 🔴 MAINNET), and a note that circuit-breaker timers are cleared.
        See gotcha #31 for the peak_capital semantic shift (May 2026).
        """
        self._post(
            f"🔄 <b>HWM Reset</b>  [{self._mode_tag(mode)}]\n"
            f"Old peak: <code>{_fmt(old_peak)} USDT</code>\n"
            f"New peak: <code>{_fmt(new_peak)} USDT</code>\n"
            f"Circuit-breaker timers cleared. Trading will resume on next cycle."
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

    def help(self) -> None:
        """Send a help message with inline buttons for every command.

        The chat-UI menu (`setMyCommands`) only advertises `/status`, `/report`,
        `/help` to keep it clean. `/pause` and `/resume` live behind this help
        message as inline buttons — pressing one sends the corresponding command
        via Telegram's callback_query mechanism, which the command handler
        treats as if the user had typed it.
        """
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📊 Status", "callback_data": "/status"},
                    {"text": "📈 Report", "callback_data": "/report"},
                ],
                [
                    {"text": "⏸ Pause",  "callback_data": "/pause"},
                    {"text": "▶️ Resume", "callback_data": "/resume"},
                ],
                [
                    # Destructive command — placed behind /help so it's discoverable
                    # but not in setMyCommands top-3. See design Decision 5.
                    {"text": "🔄 Reset HWM", "callback_data": "/reset_hwm"},
                ],
            ]
        }
        self._post(
            "🤖 <b>COMANDOS DISPONIBLES</b>\n\n"
            "<b>/status</b> — Balance y posiciones abiertas\n"
            "<b>/report</b> — Resumen histórico (opcional: <code>/report SYMBOL</code>)\n"
            "<b>/pause</b> — Pausar el bot (no nuevas entradas)\n"
            "<b>/resume</b> — Reanudar el bot\n"
            "<b>/reset_hwm [valor]</b> — Resetear HWM al equity actual (o valor explícito)\n"
            "<b>/help</b> — Mostrar este menú",
            reply_markup=keyboard,
        )

    def status(
        self,
        balance: float,
        open_positions: list[dict],
        mode: str,
        *,
        paused: bool = False,
    ) -> None:
        """Send a /status reply.

        open_positions: list of dicts with keys: symbol, side, entry_price,
        stop_loss, take_profit, and optionally current_price + unrealized_pnl.
        """
        bot_state = "⏸ Paused" if paused else "▶️ Running"

        if not open_positions:
            positions_block = "No open positions"
        else:
            sections: list[str] = []
            for pos in open_positions:
                sym   = pos.get("symbol", "?")
                price = pos.get("current_price")
                pnl   = pos.get("unrealized_pnl")

                price_line = (
                    f"  Price: <code>${_fmt(price)}</code>\n" if price is not None else ""
                )
                if pnl is not None:
                    sign = "+" if pnl >= 0 else "-"
                    pnl_line = f"  PnL:   <code>{sign}${_fmt(abs(pnl), '.2f')}</code>\n"
                else:
                    pnl_line = ""

                sections.append(
                    f"<b>{sym}</b>  {pos['side']} @ <code>${_fmt(pos['entry_price'])}</code>\n"
                    f"  SL <code>${_fmt(pos['stop_loss'])}</code>  "
                    f"TP <code>${_fmt(pos['take_profit'])}</code>\n"
                    f"{price_line}{pnl_line}"
                )
            positions_block = "\n".join(sections).rstrip()

        self._post(
            f"📊 <b>STATUS</b>  [{self._mode_tag(mode)}]\n"
            f"Balance: <code>${_fmt(balance)}</code>\n"
            f"Bot:     <code>{bot_state}</code>\n\n"
            f"{positions_block}"
        )

    def report(
        self,
        closed_trades: list[dict],
        equity_curve: list[dict],
        perf_by_strategy: list[dict],
        balance: float,
        mode: str,
        initial_capital: float,
        *,
        symbol: str | None = None,
        symbols_breakdown: list[dict] | None = None,
    ) -> None:
        header = f"📈 <b>REPORT</b>"
        if symbol:
            header += f"  <code>{symbol}</code>"
        header += f"  [{self._mode_tag(mode)}]"

        if not closed_trades:
            self._post(
                f"{header}\n"
                f"Balance: <code>${_fmt(balance)}</code>\n"
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

        breakdown_block = ""
        if symbols_breakdown:
            lines = []
            for row in symbols_breakdown:
                row_sign = "+" if row["total_pnl"] >= 0 else ""
                lines.append(
                    f"  <code>{row['symbol']:<10}</code> "
                    f"{row['total']:>3}t  WR {row['win_rate']:.0f}%  "
                    f"<code>{row_sign}${_fmt(row['total_pnl'])}</code>"
                )
            breakdown_block = "\n\n<b>By symbol:</b>\n" + "\n".join(lines)

        self._post(
            f"{header}\n\n"
            f"Balance:         <code>${_fmt(balance)}  ({sign}{_fmt(pnl_pct, '.2f')}%)</code>\n"
            f"Trades:          <code>{total} closed  |  Win rate: {_fmt(win_rate, '.1f')}%</code>\n"
            f"PnL total:       <code>{sign}${_fmt(total_pnl)}</code>\n"
            f"Profit factor:   <code>{pf_str}</code>\n"
            f"Sharpe ratio:    <code>{sr:.2f}</code>\n"
            f"Max drawdown:    <code>{md:.2f}%</code>\n"
            f"Max loss streak: <code>{streak}</code>"
            f"{best_line}"
            f"{breakdown_block}"
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
