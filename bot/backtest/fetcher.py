"""Historical OHLCV fetcher from Binance public REST API.

Uses the unauthenticated /api/v3/klines endpoint — always mainnet, no API keys
required.  This is correct for backtesting regardless of whether the live bot
runs on testnet.
"""

import logging
import time
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_BINANCE_API = "https://api.binance.com/api/v3/klines"
_KLINES_LIMIT = 1000
_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "num_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
_OHLCV_COLS = ["open_time", "open", "high", "low", "close", "volume"]


def fetch_historical_klines(
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
) -> pd.DataFrame:
    """Fetch historical OHLCV from Binance public API with automatic pagination.

    Args:
        symbol:   Binance trading pair, e.g. "BTCUSDT".
        interval: Kline interval, e.g. "1h", "15m", "4h".
        start_dt: Inclusive start (UTC-aware or naive UTC datetime).
        end_dt:   Exclusive end.

    Returns:
        DataFrame with columns: open_time (UTC Timestamp), open, high, low,
        close, volume.  Sorted ascending by open_time.

    Raises:
        requests.HTTPError: on non-2xx HTTP response.
        ValueError: if Binance returns no data for the requested range.
    """
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp()   * 1000)

    all_rows: list[list] = []
    cursor = start_ms

    while cursor < end_ms:
        resp = requests.get(
            _BINANCE_API,
            params={
                "symbol":    symbol,
                "interval":  interval,
                "startTime": cursor,
                "endTime":   end_ms,
                "limit":     _KLINES_LIMIT,
            },
            timeout=30,
        )
        resp.raise_for_status()
        batch: list[list] = resp.json()

        if not batch:
            break

        all_rows.extend(batch)
        logger.debug(
            "Fetched page: %d bars (total so far: %d)", len(batch), len(all_rows)
        )

        last_open_time_ms: int = batch[-1][0]
        cursor = last_open_time_ms + 1

        if len(batch) < _KLINES_LIMIT:
            break  # last page — no more data in range

        time.sleep(0.1)  # stay well within Binance rate limits

    if not all_rows:
        raise ValueError(
            f"Binance returned no klines for {symbol} {interval} "
            f"between {start_dt.date()} and {end_dt.date()}"
        )

    df = pd.DataFrame(all_rows, columns=_KLINE_COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[_OHLCV_COLS].copy()
    df[["open", "high", "low", "close", "volume"]] = (
        df[["open", "high", "low", "close", "volume"]].astype(float)
    )
    df = df.sort_values("open_time").reset_index(drop=True)

    logger.info(
        "Fetched %d %s klines for %s: %s → %s",
        len(df), interval, symbol,
        df["open_time"].iloc[0].strftime("%Y-%m-%d %H:%M"),
        df["open_time"].iloc[-1].strftime("%Y-%m-%d %H:%M"),
    )
    return df
