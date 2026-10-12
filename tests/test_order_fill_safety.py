"""Entry/exit order placement must never put more size on the exchange than
the DB records.

Three paths used to do exactly that:
  1. LIMIT_MAKER partially filled → cancelled → MARKET for the FULL quantity.
  2. LIMIT_MAKER filled between the last poll and the cancel → cancel fails
     (only a warning) → MARKET for the full quantity anyway → 2× position.
  3. create_order times out AFTER Binance accepted it → @_retry resends → 2×.
"""
from unittest.mock import MagicMock

import pytest
import requests
from binance.exceptions import BinanceAPIException

import bot.exchange.binance_client as bc
from bot.exchange.binance_client import BinanceClient


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)


def _client(inner) -> BinanceClient:
    c = object.__new__(BinanceClient)
    c._client = inner
    c._testnet = True
    return c


def _api_error(code: int, msg: str = "err") -> BinanceAPIException:
    resp = MagicMock(status_code=400, text=f'{{"code":{code},"msg":"{msg}"}}')
    resp.json.return_value = {"code": code, "msg": msg}
    return BinanceAPIException(resp, 400, resp.text)


def test_partial_fill_then_market_buys_only_the_remainder(monkeypatch):
    inner = MagicMock()
    inner.create_order.side_effect = [
        {"orderId": 1, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"},
        {"orderId": 2, "status": "FILLED", "executedQty": "0.6", "cummulativeQuoteQty": "60.6"},
    ]
    partial = {"orderId": 1, "status": "PARTIALLY_FILLED",
               "executedQty": "0.4", "cummulativeQuoteQty": "40.0"}
    inner.get_order.return_value = partial
    inner.get_symbol_info.return_value = {
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00010000"}],
    }

    result = _client(inner).place_entry_order(
        "ETHUSDT", "BUY", 1.0, entry_price=100.0, wait_seconds=0,
    )

    market_call = inner.create_order.call_args_list[1]
    assert market_call.kwargs["type"] == "MARKET"
    assert market_call.kwargs["quantity"] == pytest.approx(0.6)
    # Combined fill so the caller's avg-price maths covers both legs
    assert float(result["executedQty"]) == pytest.approx(1.0)
    assert float(result["cummulativeQuoteQty"]) == pytest.approx(100.6)


def test_fill_racing_the_cancel_does_not_send_a_market_order():
    inner = MagicMock()
    inner.create_order.return_value = {"orderId": 1, "status": "NEW"}
    filled = {"orderId": 1, "status": "FILLED",
              "executedQty": "1.0", "cummulativeQuoteQty": "99.99"}
    # Last poll still NEW (wait_seconds=0 → no poll), cancel fails because it
    # just filled, the re-read shows FILLED.
    inner.cancel_order.side_effect = _api_error(-2011, "Unknown order sent.")
    inner.get_order.return_value = filled

    result = _client(inner).place_entry_order(
        "ETHUSDT", "BUY", 1.0, entry_price=100.0, wait_seconds=0,
    )

    assert inner.create_order.call_count == 1  # no MARKET fallback
    assert result["status"] == "FILLED"


def test_place_order_timeout_after_accept_is_not_resent():
    inner = MagicMock()
    inner.create_order.side_effect = requests.exceptions.ReadTimeout("read timed out")
    accepted = {"orderId": 7, "status": "FILLED", "executedQty": "0.5"}
    inner.get_order.return_value = accepted

    result = _client(inner).place_order("BTCUSDT", "SELL", 0.5)

    assert inner.create_order.call_count == 1
    assert result == accepted
    sent_cid = inner.create_order.call_args.kwargs["newClientOrderId"]
    assert inner.get_order.call_args.kwargs["origClientOrderId"] == sent_cid


def test_place_order_timeout_before_accept_is_retried_with_same_id():
    inner = MagicMock()
    inner.create_order.side_effect = [
        requests.exceptions.ReadTimeout("read timed out"),
        {"orderId": 8, "status": "FILLED"},
    ]
    inner.get_order.side_effect = _api_error(-2013, "Order does not exist.")

    result = _client(inner).place_order("BTCUSDT", "SELL", 0.5)

    assert result["orderId"] == 8
    cids = {c.kwargs["newClientOrderId"] for c in inner.create_order.call_args_list}
    assert len(cids) == 1  # same id → Binance itself rejects a duplicate


def test_place_order_api_rejection_is_not_retried_and_keeps_cause():
    inner = MagicMock()
    inner.create_order.side_effect = _api_error(-2010, "insufficient balance")

    with pytest.raises(Exception) as ei:
        _client(inner).place_order("BTCUSDT", "SELL", 0.5)

    assert inner.create_order.call_count == 1
    cause = ei.value.__cause__ or ei.value
    assert getattr(cause, "code", None) == -2010


def test_client_keeps_its_whole_public_api():
    """Regression (2026-10-02 deploy): module-level helpers inserted mid-class
    silently detached every method below them. Unit tests built the client
    with object.__new__ and never noticed; main() crashed on
    start_price_stream at startup."""
    for name in (
        "get_klines", "get_balance", "place_order", "place_entry_order",
        "get_price_precision", "get_quantity_precision", "get_open_orders",
        "cancel_order", "get_ticker_price", "start_price_stream",
        "stop_price_stream", "is_testnet",
    ):
        assert hasattr(BinanceClient, name), name


def test_partial_fill_remainder_uses_lot_size_precision():
    inner = MagicMock()
    inner.create_order.side_effect = [
        {"orderId": 1, "status": "NEW"},
        {"orderId": 2, "status": "FILLED", "executedQty": "0.0123", "cummulativeQuoteQty": "1.23"},
    ]
    inner.get_order.return_value = {"orderId": 1, "status": "CANCELED",
                                    "executedQty": "0.0100", "cummulativeQuoteQty": "1.0"}
    inner.get_symbol_info.return_value = {
        "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00010000"}],
    }

    _client(inner).place_entry_order(
        "BTCUSDT", "BUY", 0.02236, entry_price=100.0, wait_seconds=0,
    )

    # 0.02236 - 0.0100 = 0.01236 → rounded DOWN to 4 decimals = 0.0123
    assert inner.create_order.call_args_list[1].kwargs["quantity"] == pytest.approx(0.0123)
