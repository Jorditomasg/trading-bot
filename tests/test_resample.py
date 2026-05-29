"""Tests for resample_ohlcv — the daily-bias reconstruction used to dodge the
Binance-testnet 1d kline cap (gotcha #38)."""

from __future__ import annotations

import pandas as pd

from bot.bias.filter import Bias, BiasFilter, BiasFilterConfig
from bot.indicators.utils import resample_ohlcv
from tests.conftest import make_ohlcv, uptrend


def test_4h_to_daily_aggregation():
    # 12 4h bars starting at UTC midnight = exactly 2 full days (6 bars each).
    closes = [100, 101, 102, 103, 104, 105,   # day 1
              106, 107, 108, 109, 110, 111]   # day 2
    df = make_ohlcv(closes, freq="4h", start="2024-01-01")

    daily = resample_ohlcv(df, "1D")

    assert len(daily) == 2
    day1 = daily.iloc[0]
    # open = first bar's open, close = last bar's close, high/low = extrema, volume = sum
    assert day1["open"]  == df.iloc[0]["open"]
    assert day1["close"] == df.iloc[5]["close"]
    assert day1["high"]  == df.iloc[0:6]["high"].max()
    assert day1["low"]   == df.iloc[0:6]["low"].min()
    assert day1["volume"] == df.iloc[0:6]["volume"].sum()


def test_partial_trailing_bucket_kept():
    # 8 4h bars = 1 full day + 2 bars into the next → trailing partial day kept.
    df = make_ohlcv(list(range(100, 108)), freq="4h", start="2024-01-01")
    daily = resample_ohlcv(df, "1D")
    assert len(daily) == 2  # in-progress second day is retained, like a live kline


def test_200_4h_bars_clears_bias_threshold():
    # The production case: 200 primary 4h bars must yield >= 22 daily bars so the
    # bias EMA21 has enough history when testnet caps the direct 1d fetch.
    df = make_ohlcv([40_000.0 + i for i in range(200)], freq="4h", start="2024-01-01")
    daily = resample_ohlcv(df, "1D")
    assert len(daily) >= 22
    assert list(daily.columns) == ["open_time", "open", "high", "low", "close", "volume"]
    assert pd.api.types.is_datetime64_any_dtype(daily["open_time"])


def test_resampled_daily_yields_real_bias_not_data_failure():
    # The point of the fix: with only ~20 direct 1d bars the BiasFilter returns a
    # data-failure NEUTRAL (gate disabled). Feeding it the daily series resampled
    # from 200 4h bars must instead produce a real directional bias.
    bias = BiasFilter(BiasFilterConfig())

    short_direct = uptrend(n=20, freq="1d")          # what testnet hands back
    assert bias.get_bias(short_direct) is Bias.NEUTRAL
    assert bias._last_bias_was_data_failure is True

    primary_4h = uptrend(n=200, freq="4h", start="2024-01-01")
    derived = resample_ohlcv(primary_4h, "1D")
    result = bias.get_bias(derived)
    assert result is Bias.BULLISH
    assert bias._last_bias_was_data_failure is False
