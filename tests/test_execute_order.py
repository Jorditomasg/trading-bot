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


# ── per-symbol price precision ───────────────────────────────────────────────


def test_init_price_precision_per_symbol(monkeypatch):
    """Each active symbol gets its own PRICE_FILTER precision cached."""
    fake_client = MagicMock()
    fake_client.get_price_precision.side_effect = (
        lambda s: {"BTCUSDT": 2, "DOGEUSDT": 5}[s]
    )
    monkeypatch.setattr(main_module, "_build_client", lambda db: fake_client)
    monkeypatch.setattr(main_module, "_price_precision", {})

    main_module._init_price_precision(MagicMock(), ["BTCUSDT", "DOGEUSDT"])

    assert main_module._price_precision == {"BTCUSDT": 2, "DOGEUSDT": 5}


def test_init_price_precision_partial_failure_keeps_other_symbols(monkeypatch):
    """A fetch failure on one symbol does not block the rest; the failed one
    falls back to _DEFAULT_PRICE_PRECISION at order time."""

    def fetch(sym):
        if sym == "SOLUSDT":
            raise RuntimeError("exchangeInfo down")
        return 2

    fake_client = MagicMock()
    fake_client.get_price_precision.side_effect = fetch
    monkeypatch.setattr(main_module, "_build_client", lambda db: fake_client)
    monkeypatch.setattr(main_module, "_price_precision", {})

    main_module._init_price_precision(MagicMock(), ["BTCUSDT", "SOLUSDT"])

    assert main_module._price_precision == {"BTCUSDT": 2}
    assert (
        main_module._price_precision.get("SOLUSDT", main_module._DEFAULT_PRICE_PRECISION)
        == main_module._DEFAULT_PRICE_PRECISION
    )


def test_execute_order_open_uses_symbol_precision(monkeypatch):
    """_execute_order passes the per-symbol precision to place_entry_order."""
    monkeypatch.setattr(main_module, "_price_precision", {"ETHUSDT": 4})

    client = MagicMock()
    client.is_testnet = True
    client.place_entry_order.return_value = {"orderId": 1, "status": "FILLED"}
    db = MagicMock()
    db.get_active_mode.return_value = "TESTNET"
    db.insert_trade.return_value = 7

    main_module._execute_order(client, db, {
        "action":      "OPEN",
        "symbol":      "ETHUSDT",
        "side":        "BUY",
        "quantity":    0.5,
        "entry_price": 1800.0,
        "stop_loss":   1750.0,
        "take_profit": 1950.0,
        "strategy":    "EMA_CROSSOVER",
        "regime":      "TRENDING",
        "atr":         25.0,
        "timeframe":   "4h",
    }, notifier=None)

    assert client.place_entry_order.call_args.kwargs["price_precision"] == 4


def test_execute_order_open_unknown_symbol_uses_default(monkeypatch):
    """Symbols missing from the cache fall back to _DEFAULT_PRICE_PRECISION."""
    monkeypatch.setattr(main_module, "_price_precision", {})

    client = MagicMock()
    client.is_testnet = True
    client.place_entry_order.return_value = {"orderId": 1, "status": "FILLED"}
    db = MagicMock()
    db.get_active_mode.return_value = "TESTNET"
    db.insert_trade.return_value = 8

    main_module._execute_order(client, db, {
        "action":      "OPEN",
        "symbol":      "BTCUSDT",
        "side":        "BUY",
        "quantity":    0.01,
        "entry_price": 65000.0,
        "stop_loss":   63000.0,
        "take_profit": 70000.0,
        "strategy":    "EMA_CROSSOVER",
        "regime":      "TRENDING",
        "atr":         900.0,
        "timeframe":   "4h",
    }, notifier=None)

    assert (
        client.place_entry_order.call_args.kwargs["price_precision"]
        == main_module._DEFAULT_PRICE_PRECISION
    )


# ── CLOSE rejected for insufficient balance (testnet reset) ─────────────────
# Incident 2026-09-18 → 09-23: Binance's spot testnet was reset, wiping the
# 7.051 SOL of trade 26. Its TP hit on 09-18 and every 60s since the SELL was
# rejected with -2010 — 4,597 failed closes, and the SOL slot stayed "in
# position" so it could never re-enter.


def _insufficient_balance_error() -> RuntimeError:
    from binance.exceptions import BinanceAPIException

    response = MagicMock()
    response.text = '{"code":-2010,"msg":"Account has insufficient balance for requested action."}'
    api_exc = BinanceAPIException(response, 400, response.text)
    err = RuntimeError("All 3 attempts failed for place_order")
    err.__cause__ = api_exc
    return err


def _close_order(trade_id: int = 26) -> dict:
    return {
        "action":      "CLOSE",
        "symbol":      "SOLUSDT",
        "side":        "SELL",
        "quantity":    7.051,
        "trade_id":    trade_id,
        "exit_price":  111.20,
        "exit_reason": "TAKE_PROFIT",
    }


def _rejecting_client(testnet: bool, free_base: float) -> MagicMock:
    client = MagicMock()
    client.is_testnet = testnet
    client.place_order.side_effect = _insufficient_balance_error()
    client.get_balance.return_value = free_base
    return client


@pytest.fixture(autouse=True)
def _reset_close_alerts(monkeypatch):
    monkeypatch.setattr(main_module, "_unfillable_close_alerted", set())


def test_testnet_close_whose_base_asset_was_wiped_is_recorded_at_signal_price():
    client = _rejecting_client(testnet=True, free_base=4.0)
    db = MagicMock()
    db.get_active_mode.return_value = "TESTNET"
    notifier = MagicMock()

    main_module._execute_order(client, db, _close_order(), notifier=notifier)

    client.get_balance.assert_called_with("SOL")
    db.close_trade.assert_called_once_with(
        trade_id=26, exit_price=111.20, exit_reason="TAKE_PROFIT",
    )
    notifier.alert.assert_called_once()
    notifier.trade_closed.assert_called_once()


def test_mainnet_close_rejected_for_balance_is_never_faked_and_alerts_once():
    client = _rejecting_client(testnet=False, free_base=4.0)
    db = MagicMock()
    db.get_active_mode.return_value = "MAINNET"
    notifier = MagicMock()

    main_module._execute_order(client, db, _close_order(), notifier=notifier)
    main_module._execute_order(client, db, _close_order(), notifier=notifier)

    db.close_trade.assert_not_called()
    notifier.alert.assert_called_once()


def test_testnet_close_with_enough_base_balance_is_not_reconciled():
    # Balance is there, so -2010 means something else — do not paper over it.
    client = _rejecting_client(testnet=True, free_base=10.0)
    db = MagicMock()
    db.get_active_mode.return_value = "TESTNET"

    main_module._execute_order(client, db, _close_order(), notifier=None)

    db.close_trade.assert_not_called()


def test_close_failing_for_other_reasons_is_not_reconciled():
    client = MagicMock()
    client.is_testnet = True
    client.place_order.side_effect = RuntimeError("All 3 attempts failed for place_order")
    db = MagicMock()
    db.get_active_mode.return_value = "TESTNET"

    main_module._execute_order(client, db, _close_order(), notifier=None)

    client.get_balance.assert_not_called()
    db.close_trade.assert_not_called()
