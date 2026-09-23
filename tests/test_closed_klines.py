"""Live↔backtest fidelity: signal fetches must only see CLOSED candles.

The bot runs hourly (`schedule.every().hour.at(":00")`) but trades a 4h
timeframe. Binance's `get_klines` always appends the currently-forming candle,
so without filtering every cycle evaluated EMA/ATR/bias on a partial bar —
signals repainted and ATR was understated, producing stops far tighter than the
backtest ever simulated. `BacktestEngine` iterates historical bars that are all
closed by construction, so live and backtest were structurally different.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.exchange.binance_client import BinanceClient


HOUR_MS = 3_600_000


def _kline(open_time_ms: int, interval_ms: int, close: float) -> list:
    """One raw Binance kline row. close_time is the last ms of the interval."""
    return [
        open_time_ms,
        str(close), str(close), str(close), str(close), "10.0",
        open_time_ms + interval_ms - 1,
        "0.0", 5, "0.0", "0.0", "0",
    ]


@pytest.fixture()
def client(monkeypatch):
    """BinanceClient with the network constructor bypassed."""
    monkeypatch.setattr(BinanceClient, "__init__", lambda self: None)
    c = BinanceClient()
    c._market = MagicMock()
    c._testnet = True
    return c


def _fixed_now(monkeypatch, now_ms: int) -> None:
    monkeypatch.setattr(
        "bot.exchange.binance_client.time.time", lambda: now_ms / 1000.0
    )


def test_closed_only_drops_the_forming_candle(client, monkeypatch):
    interval = 4 * HOUR_MS
    # Bars open at 12:00, 16:00, 20:00. "Now" is 21:19 → the 20:00 bar is live.
    bars = [
        _kline(12 * HOUR_MS, interval, 1923.25),
        _kline(16 * HOUR_MS, interval, 1918.90),
        _kline(20 * HOUR_MS, interval, 1923.54),  # still forming
    ]
    client._market.get_klines.return_value = bars
    _fixed_now(monkeypatch, 21 * HOUR_MS + 19 * 60_000)

    df = client.get_klines("ETHUSDT", "4h", limit=3, closed_only=True)

    assert len(df) == 2
    assert df["close"].tolist() == [1923.25, 1918.90]


def test_closed_only_keeps_a_fully_closed_last_candle(client, monkeypatch):
    interval = 4 * HOUR_MS
    bars = [
        _kline(12 * HOUR_MS, interval, 1923.25),
        _kline(16 * HOUR_MS, interval, 1918.90),
    ]
    client._market.get_klines.return_value = bars
    # 20:30 — the 16:00 bar closed at 19:59:59.999, nothing to drop.
    _fixed_now(monkeypatch, 20 * HOUR_MS + 30 * 60_000)

    df = client.get_klines("ETHUSDT", "4h", limit=2, closed_only=True)

    assert len(df) == 2
    assert df["close"].tolist() == [1923.25, 1918.90]


def test_closed_only_requests_one_extra_bar_to_preserve_limit(client, monkeypatch):
    """Dropping the live bar must not starve warmup windows (EMA200, Hurst 100)."""
    interval = 4 * HOUR_MS
    bars = [_kline(i * interval, interval, 100.0 + i) for i in range(201)]
    client._market.get_klines.return_value = bars
    _fixed_now(monkeypatch, 200 * interval + HOUR_MS)  # last bar forming

    df = client.get_klines("BTCUSDT", "4h", limit=200, closed_only=True)

    assert client._market.get_klines.call_args.kwargs["limit"] == 201
    assert len(df) == 200


def test_fetch_limit_is_clamped_to_the_binance_maximum(client, monkeypatch):
    """limit+1 must not push the request past 1000, which Binance rejects."""
    interval = 4 * HOUR_MS
    bars = [_kline(i * interval, interval, 100.0) for i in range(1000)]
    client._market.get_klines.return_value = bars
    _fixed_now(monkeypatch, 1000 * interval)

    client.get_klines("BTCUSDT", "4h", limit=1000, closed_only=True)

    assert client._market.get_klines.call_args.kwargs["limit"] == 1000


def test_default_keeps_the_live_candle(client, monkeypatch):
    """Exit checks price off a 1m bar — they need the in-flight price."""
    minute = 60_000
    bars = [
        _kline(100 * minute, minute, 64_700.0),
        _kline(101 * minute, minute, 64_734.0),  # forming
    ]
    client._market.get_klines.return_value = bars
    _fixed_now(monkeypatch, 101 * minute + 30_000)

    df = client.get_klines("BTCUSDT", "1m", limit=2)

    assert len(df) == 2
    assert df["close"].iloc[-1] == 64_734.0
    assert client._market.get_klines.call_args.kwargs["limit"] == 2


def test_closed_only_never_empties_a_single_bar_response(client, monkeypatch):
    """Degenerate feed: return what we have rather than an empty frame."""
    interval = 4 * HOUR_MS
    client._market.get_klines.return_value = [_kline(20 * HOUR_MS, interval, 1923.54)]
    _fixed_now(monkeypatch, 21 * HOUR_MS)

    df = client.get_klines("ETHUSDT", "4h", limit=1, closed_only=True)

    assert len(df) == 1
