import pandas as pd


def adx_last(df: pd.DataFrame, period: int) -> float:
    """Return the current ADX value (last bar) using Wilder smoothing.

    Extracted from RegimeDetector._adx() so both the regime detector and
    EMACrossoverStrategy can share the same math without coupling.
    Bit-identical to RegimeDetector._adx() — see test_adx_last_matches_detector_adx.
    """
    high = df["high"]
    low  = df["low"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = df["close"].shift(1)

    plus_dm  = (high - prev_high).clip(lower=0)
    minus_dm = (prev_low - low).clip(lower=0)
    plus_dm  = plus_dm.where(plus_dm  > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm,  0.0)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    smoothed_tr    = wilder_smooth(tr,       period)
    smoothed_plus  = wilder_smooth(plus_dm,  period)
    smoothed_minus = wilder_smooth(minus_dm, period)

    plus_di  = 100 * smoothed_plus  / smoothed_tr.replace(0, float("nan"))
    minus_di = 100 * smoothed_minus / smoothed_tr.replace(0, float("nan"))

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, float("nan"))
    ).fillna(0)

    adx_series = wilder_smooth(dx, period)
    return float(adx_series.iloc[-1])


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    # Replace zero loss with a near-zero epsilon so all-up streaks yield RSI≈100
    # instead of NaN, which would silently suppress signals via False comparisons.
    rs = gain / loss.replace(0, 1e-10)
    return (100 - (100 / (1 + rs))).clip(0, 100)


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False).mean()


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate an OHLCV frame to a coarser timeframe.

    `df` needs an ``open_time`` datetime column plus open/high/low/close/volume.
    Used to derive a higher-timeframe series (e.g. the daily bias gate) from the
    primary klines when the exchange caps direct higher-tf fetches: Binance
    testnet returns ~20 daily bars, below the 22 the bias EMA21 needs (gotcha
    #38). 4h candles are UTC-aligned (00,04,…,20) so resampling to ``"1D"`` is an
    exact reconstruction of real daily candles.

    The trailing (in-progress) bucket is kept, mirroring a live higher-tf kline
    whose last candle is still forming.
    """
    out = (
        df.set_index("open_time")
        .resample(rule)
        .agg({"open": "first", "high": "max", "low": "min",
              "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )
    return out
