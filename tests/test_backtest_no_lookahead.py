"""Gotcha #49 — the backtest must only see higher-timeframe bars that had CLOSED.

Until 2026-10-12 `_get_4h_window` / `_get_weekly_window` filtered by
`open_time <= current_time`. For a 4h bar opening at 08:00 that admitted the
daily bar opened at 00:00 *with its final close* (16h in the future), and the
current week's bar with Sunday's close (up to 7 days ahead). Live fetches
`closed_only=True` klines, so every backtest that validated the strategy was
reading the future and live was not.
"""
import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig, BacktestEngine


def _bars(start: str, n: int, freq: str, close0: float = 100.0) -> pd.DataFrame:
    t = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    c = [close0 + i for i in range(n)]
    return pd.DataFrame({"open_time": t, "open": c, "high": c, "low": c, "close": c, "volume": [1.0] * n})


@pytest.fixture
def engine() -> BacktestEngine:
    return BacktestEngine(BacktestConfig(timeframe="4h", min_4h_bars=1, momentum_filter_enabled=True))


def test_daily_window_excludes_the_day_still_forming(engine):
    daily = _bars("2026-01-01", 10, "1D")
    # 4h bar opening 2026-01-05 08:00 closes 12:00 — the 01-05 daily bar is still forming.
    win = engine._get_4h_window(daily, pd.Timestamp("2026-01-05 08:00", tz="UTC"))
    assert win["close"].iloc[-1] == daily.loc[daily.open_time == pd.Timestamp("2026-01-04", tz="UTC"), "close"].item()


def test_daily_window_includes_the_day_closed_by_this_bar(engine):
    daily = _bars("2026-01-01", 10, "1D")
    # The 20:00 4h bar closes at 00:00 of 01-06, which is exactly when the 01-05 daily bar closes.
    win = engine._get_4h_window(daily, pd.Timestamp("2026-01-05 20:00", tz="UTC"))
    assert win["close"].iloc[-1] == daily.loc[daily.open_time == pd.Timestamp("2026-01-05", tz="UTC"), "close"].item()


def test_weekly_window_excludes_the_week_still_forming(engine):
    weekly = _bars("2025-06-02", 40, "7D")   # Mondays
    now = pd.Timestamp("2026-01-07 08:00", tz="UTC")   # Wednesday: week opened 01-05 still forming
    win = engine._get_weekly_window(weekly, now)
    assert win["open_time"].iloc[-1] + pd.Timedelta("7D") <= now + pd.Timedelta("4h")


def test_momentum_uses_the_current_price_not_the_last_weekly_close(engine):
    weekly = _bars("2025-06-02", 30, "7D", close0=100.0)
    win = engine._get_weekly_window(weekly, pd.Timestamp("2026-01-07 08:00", tz="UTC"))
    sma = win["close"].to_numpy()[-20:].mean()
    assert engine._get_momentum_state(win, current_price=sma * 0.5) == "BEARISH"
    assert engine._get_momentum_state(win, current_price=sma * 1.5) == "BULLISH"
    assert engine._get_momentum_state(win, current_price=sma) == "NEUTRAL"
