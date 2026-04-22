import pandas as pd


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
