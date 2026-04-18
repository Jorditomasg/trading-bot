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
    for i in range(n_wins):
        trades.append({"pnl": 100.0, "exit_price": 50000.0, "strategy": "EMA_CROSSOVER"})
    for i in range(n_losses):
        trades.append({"pnl": -50.0, "exit_price": 49000.0, "strategy": "EMA_CROSSOVER"})
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
