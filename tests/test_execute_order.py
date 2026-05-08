"""Unit tests for the post-exchange DB-write retry helpers in main.py.

Covers `_retry_db_write` (exception scope + backoff) and
`_alert_orphan_position` (logging + Telegram alert delivery).
"""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import MagicMock

import pytest

import main as main_module


# ── _retry_db_write ──────────────────────────────────────────────────────────


def test_retry_db_write_returns_value_on_success():
    """No exception → fn called once, value returned untouched."""
    fn = MagicMock(return_value="ok")
    result = main_module._retry_db_write("op", fn, 1, key="value")
    assert result == "ok"
    assert fn.call_count == 1
    fn.assert_called_with(1, key="value")


def test_retry_db_write_retries_on_sqlite_operational_error(monkeypatch):
    """OperationalError is retryable (locked DB) — succeeds on attempt 2."""
    monkeypatch.setattr(main_module.time, "sleep", lambda _: None)
    calls: list[int] = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise sqlite3.OperationalError("database is locked")
        return "recovered"

    result = main_module._retry_db_write("flaky", flaky)
    assert result == "recovered"
    assert len(calls) == 2


def test_retry_db_write_propagates_after_exhaustion(monkeypatch):
    """All attempts fail with retryable error → final exception is raised."""
    monkeypatch.setattr(main_module.time, "sleep", lambda _: None)
    fn = MagicMock(side_effect=sqlite3.OperationalError("disk I/O error"))

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        main_module._retry_db_write("op", fn)

    assert fn.call_count == main_module._DB_RETRY_ATTEMPTS


def test_retry_db_write_does_not_retry_programming_bugs(monkeypatch):
    """TypeError / KeyError / AttributeError are NOT retryable — propagate
    immediately so callers see the bug instead of paying ~3.5s for nothing."""
    monkeypatch.setattr(main_module.time, "sleep", lambda _: None)

    for exc_class in (TypeError, KeyError, AttributeError, ValueError):
        fn = MagicMock(side_effect=exc_class("bug"))
        with pytest.raises(exc_class):
            main_module._retry_db_write("op", fn)
        assert fn.call_count == 1, f"{exc_class.__name__} should not retry"


def test_retry_db_write_backoff_doubles(monkeypatch):
    """Backoff sequence: 0.5, 1.0, 2.0 — each failed attempt doubles the wait."""
    waits: list[float] = []
    monkeypatch.setattr(main_module.time, "sleep", waits.append)
    fn = MagicMock(side_effect=sqlite3.OperationalError("locked"))

    with pytest.raises(sqlite3.OperationalError):
        main_module._retry_db_write("op", fn)

    assert waits == [0.5, 1.0, 2.0]


# ── _alert_orphan_position ────────────────────────────────────────────────────


def test_alert_orphan_logs_critical_and_sends_telegram(caplog):
    """Logs CRITICAL with orderId + trade detail, calls notifier.alert exactly once."""
    notifier = MagicMock()
    order = {
        "side":     "BUY",
        "quantity": 0.001,
        "trade_id": 42,
    }
    exchange_result = {"orderId": 12345, "status": "FILLED"}
    exc = sqlite3.OperationalError("disk full")

    with caplog.at_level(logging.CRITICAL, logger=main_module.logger.name):
        main_module._alert_orphan_position(
            notifier=notifier,
            op="OPEN",
            symbol="BTCUSDT",
            order=order,
            exchange_result=exchange_result,
            exc=exc,
        )

    # Critical log carries orderId + symbol so reconciliation has what it needs
    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(crit) == 1
    assert "BTCUSDT" in crit[0].getMessage()
    assert "12345"   in crit[0].getMessage()
    assert "ORPHANED OPEN" in crit[0].getMessage()

    # Alert is sent once via the public notifier API
    notifier.alert.assert_called_once()
    msg = notifier.alert.call_args[0][0]
    assert "ORPHANED OPEN" in msg
    assert "BTCUSDT" in msg
    assert "12345"   in msg
    assert "BUY"     in msg


def test_alert_orphan_handles_missing_notifier(caplog):
    """notifier=None must NOT raise — only logs CRITICAL."""
    with caplog.at_level(logging.CRITICAL, logger=main_module.logger.name):
        main_module._alert_orphan_position(
            notifier=None,
            op="CLOSE",
            symbol="ETHUSDT",
            order={"side": "SELL", "quantity": 0.5},
            exchange_result={"orderId": 99},
            exc=sqlite3.OperationalError("locked"),
        )
    crit = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(crit) == 1
    assert "ORPHANED CLOSE" in crit[0].getMessage()


def test_alert_orphan_with_non_dict_exchange_result(caplog):
    """Defensive: exchange_result not a dict (e.g. None on weird code path) →
    orderId reported as 'N/A', no AttributeError raised."""
    notifier = MagicMock()
    with caplog.at_level(logging.CRITICAL, logger=main_module.logger.name):
        main_module._alert_orphan_position(
            notifier=notifier,
            op="OPEN",
            symbol="BTCUSDT",
            order={"side": "BUY", "quantity": 0.001},
            exchange_result=None,  # type: ignore[arg-type]
            exc=sqlite3.OperationalError("locked"),
        )
    notifier.alert.assert_called_once()
    msg = notifier.alert.call_args[0][0]
    assert "N/A" in msg
