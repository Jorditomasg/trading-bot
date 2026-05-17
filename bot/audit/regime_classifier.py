"""Walk-forward window regime classification.

Classifies each walk-forward window by the prevailing BTC market regime based
on the 30-day return ending at window.test_start, using cached 4h klines.

Design notes (D8):
- Source of price data: existing 4h kline cache — NOT a live fetch (audit reproducibility).
- 30 days × (24h / 4h) = 180 bars.
- Fail-flat: insufficient bars before test_start → FLAT (never raises).
- Thresholds are exposed as kwargs for sensitivity analysis.

Usage:
    from bot.audit.regime_classifier import RegimeLabel, classify_window

    label = classify_window(window, df_btc_4h)
"""
from __future__ import annotations

import logging
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)

_BARS_PER_30_DAYS = 180   # 30 days × 24h ÷ 4h = 180 4h bars


class RegimeLabel(str, Enum):
    """Market regime labels based on BTC 30-day return at the start of a test window.

    Inherits from (str, Enum) per project convention (gotcha #5): comparisons
    with plain strings work natively (e.g. label == "BULL" → True).
    """
    BULL = "BULL"   # 30-day return >= +threshold_pct
    BEAR = "BEAR"   # 30-day return <= -threshold_pct
    FLAT = "FLAT"   # return within (-threshold_pct, +threshold_pct), or insufficient data


def classify_window(
    window: object,           # bot.audit.walk_forward.Window (duck-typed)
    df_btc_4h: pd.DataFrame,  # cached BTCUSDT 4h klines; must have 'open_time' + 'close'
    *,
    lookback_bars: int   = _BARS_PER_30_DAYS,
    threshold_pct: float = 0.05,
) -> RegimeLabel:
    """Classify a walk-forward window by BTC's 30-day return ending at window.test_start.

    Parameters
    ----------
    window:
        Walk-forward Window object (from bot.audit.walk_forward). Only ``test_start``
        is used.
    df_btc_4h:
        Full 4h OHLCV DataFrame for BTCUSDT. Must contain ``open_time`` (UTC-aware
        datetime or compatible) and ``close`` columns.
    lookback_bars:
        Number of 4h bars to look back from test_start. Default 180 = 30 days.
    threshold_pct:
        Minimum absolute return to call BULL/BEAR. Default 0.05 (±5%).
        Returns >= +threshold_pct → BULL; <= -threshold_pct → BEAR; else FLAT.

    Returns
    -------
    RegimeLabel
        BULL, BEAR, or FLAT. Returns FLAT on any data-quality issue (fail-flat).
    """
    test_start = window.test_start

    if df_btc_4h is None or df_btc_4h.empty:
        logger.debug("classify_window: empty df → FLAT")
        return RegimeLabel.FLAT

    # Filter bars strictly before test_start (the window's out-of-sample period begins
    # at test_start; we only look at the 30-day window that ENDS there).
    mask = df_btc_4h["open_time"] < test_start
    df_pre = df_btc_4h.loc[mask]

    if len(df_pre) < lookback_bars:
        logger.debug(
            "classify_window: only %d bars before test_start %s (need %d) → FLAT",
            len(df_pre), test_start, lookback_bars,
        )
        return RegimeLabel.FLAT

    # The 30-day window = the last `lookback_bars` bars before test_start.
    df_window = df_pre.iloc[-lookback_bars:]

    price_start = float(df_window["close"].iloc[0])
    price_end   = float(df_window["close"].iloc[-1])

    if price_start == 0.0:
        logger.warning("classify_window: price_start is 0, cannot compute return → FLAT")
        return RegimeLabel.FLAT

    ret = (price_end - price_start) / price_start

    logger.debug(
        "classify_window: test_start=%s, price_start=%.2f, price_end=%.2f, "
        "return=%.4f, threshold=±%.4f",
        test_start, price_start, price_end, ret, threshold_pct,
    )

    if ret >= threshold_pct:
        return RegimeLabel.BULL
    if ret <= -threshold_pct:
        return RegimeLabel.BEAR
    return RegimeLabel.FLAT
