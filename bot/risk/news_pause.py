"""Endogenous news-pause filter.

Detects abnormal volatility and/or volume spikes that typically accompany
external news (regulation, hacks, macro shocks, scheduled releases). Returns
True when the most recent bar should trigger a pause of new entries.

The filter is "endogenous" — it requires no external feeds. It reads the
market's own response to news (price velocity + volume) instead of trying
to predict news. Reactive but universal: catches scheduled events AND
surprises, in any market.

Two spike checks combinable via mode:
- ATR spike   : current ATR / rolling-mean ATR > atr_mult
- Volume spike: current volume / rolling-mean volume > vol_mult
- mode "OR"   : trigger if EITHER fires (loose, more pauses)
- mode "AND"  : trigger only when BOTH fire (strict, fewer pauses)

Once triggered, the caller is expected to block new entries for
`bars_after` bars (open positions are NOT force-closed).
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.indicators.utils import atr as compute_atr


@dataclass
class NewsPauseConfig:
    enabled:    bool  = False
    atr_mult:   float = 3.0   # current ATR / rolling mean threshold
    vol_mult:   float = 5.0   # current volume / rolling mean threshold
    window:     int   = 50    # rolling lookback for the means
    atr_period: int   = 14    # ATR window
    mode:       str   = "OR"  # "OR" | "AND"
    bars_after: int   = 6     # block new entries for N bars after a trigger


def is_pause_triggered(window: pd.DataFrame, config: NewsPauseConfig) -> bool:
    """Return True if the most recent bar in `window` triggers a news pause.

    `window` is an OHLCV DataFrame ending at the current bar. Indicators are
    computed on the close. The most recent bar's spike values are compared
    against the rolling mean over the previous `config.window` bars (excluding
    the current bar itself, so the test is one-sided and free of look-ahead).
    """
    if not config.enabled:
        return False

    needed = config.window + config.atr_period + 1
    if len(window) < needed:
        return False

    # ATR spike on the latest bar
    atr_series  = compute_atr(window, period=config.atr_period)
    current_atr = float(atr_series.iloc[-1])
    mean_atr    = float(atr_series.iloc[-config.window - 1 : -1].mean())
    atr_spike   = mean_atr > 0 and (current_atr / mean_atr) > config.atr_mult

    # Volume spike on the latest bar
    vol         = window["volume"]
    current_vol = float(vol.iloc[-1])
    mean_vol    = float(vol.iloc[-config.window - 1 : -1].mean())
    vol_spike   = mean_vol > 0 and (current_vol / mean_vol) > config.vol_mult

    if config.mode == "AND":
        return atr_spike and vol_spike
    return atr_spike or vol_spike
