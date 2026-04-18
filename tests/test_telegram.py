"""Tests for TelegramNotifier and TelegramCommandHandler."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from bot.telegram_notifier import TelegramNotifier
from bot.telegram_commands import TelegramCommandHandler


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _notifier(token="tok", chat_id="123", enabled=True) -> TelegramNotifier:
    """Return a TelegramNotifier backed by a mock DB."""
    db = MagicMock()
    db.get_telegram_config.return_value = {
        "token": token, "chat_id": chat_id, "enabled": enabled,
    }
    return TelegramNotifier(db)


def _closed_trades(n_wins: int = 3, n_losses: int = 1) -> list[dict]:
    trades = []
    for _ in range(n_wins):
        trades.append({"pnl": 100.0, "exit_price": 50000.0, "strategy": "EMA_CROSSOVER", "side": "BUY"})
    for _ in range(n_losses):
        trades.append({"pnl": -50.0, "exit_price": 49000.0, "strategy": "EMA_CROSSOVER", "side": "SELL"})
    return trades


def _equity_curve(start: float = 10000.0, end: float = 10350.0) -> list[dict]:
    return [
        {"timestamp": "2026-01-01T00:00:00", "balance": start, "drawdown": 0.0},
        {"timestamp": "2026-01-02T00:00:00", "balance": end,   "drawdown": 0.02},
    ]


def _perf_by_strategy() -> list[dict]:
    return [
        {
            "strategy": "EMA_CROSSOVER",
            "total_trades": 4,
            "wins": 3,
            "losses": 1,
            "win_rate": 75.0,
            "total_pnl": 250.0,
            "avg_pnl": 62.5,
        }
    ]


# ── status() ──────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_running_no_position(self):
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.status(10432.50, None, "TESTNET", paused=False)
        text = mock_post.call_args[0][0]
        assert "10,432.50" in text
        assert "Running" in text
        assert "No open position" in text

    def test_status_paused(self):
        n = _notifier()
        with patch.object(n, "_post") as mock_post:
            n.status(10000.0, None, "TESTNET", paused=True)
        text = mock_post.call_args[0][0]
        assert "Paused" in text

    def test_status_with_open_position(self):
        n = _notifier()
        trade = {"side": "BUY", "entry_price": 50000.0, "stop_loss": 49000.0, "take_profit": 52000.0}
        with patch.object(n, "_post") as mock_post:
            n.status(10000.0, trade, "TESTNET", paused=False)
        text = mock_post.call_args[0][0]
        assert "50,000.00" in text
        assert "49,000.00" in text
        assert "52,000.00" in text


# ── /status integration ───────────────────────────────────────────────────────

class TestStatusIntegration:
    def test_status_command_forwards_paused_state(self):
        db = MagicMock()
        db.get_telegram_config.return_value = {"token": "tok", "chat_id": "123", "enabled": True}
        db.get_equity_curve.return_value = [{"balance": 10000.0}]
        db.get_open_trade.return_value = None
        db.get_active_mode.return_value = "TESTNET"
        db.get_bot_paused.return_value = True
        notifier = MagicMock()
        handler = TelegramCommandHandler(db, notifier)
        update = {"update_id": 1, "message": {"chat": {"id": "123"}, "text": "/status"}}
        handler._handle(update, "123")
        notifier.status.assert_called_once_with(10000.0, None, "TESTNET", paused=True)


# ── register_commands() ───────────────────────────────────────────────────────

class TestRegisterCommands:
    def test_calls_setMyCommands_with_all_four_commands(self):
        n = _notifier()
        with patch("requests.post") as mock_post:
            mock_post.return_value.raise_for_status = MagicMock()
            n.register_commands()
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        commands = {c["command"] for c in payload["commands"]}
        assert commands == {"status", "report", "pause", "resume"}

    def test_no_call_when_token_missing(self):
        n = _notifier(token="")
        with patch("requests.post") as mock_post:
            n.register_commands()
        mock_post.assert_not_called()

    def test_silently_ignores_http_error(self):
        n = _notifier()
        with patch("requests.post", side_effect=Exception("network error")):
            n.register_commands()  # must not raise
