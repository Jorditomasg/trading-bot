"""Unified range selector for the MONITOR tab.

All charts (equity, drawdown, live price) read the same range from
`st.session_state["monitor_range"]`. The range is bounded by the equity
curve's start — showing "30D" when the bot has 12h of history makes no
sense, so longer options are hidden until enough data exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from bot.database.db import Database

# ── Range definitions ────────────────────────────────────────────────────────
# (range_key, hours, kline_timeframe, n_bars_for_live_chart)
# n_bars × kline_tf should approximately cover the range so the live chart
# shows roughly the same window as equity/drawdown.
_RANGES: list[tuple[str, float, str, int]] = [
    ("1H",   1,    "1m",  60),
    ("24H",  24,   "1h",  24),
    ("7D",   168,  "1h",  168),
    ("30D",  720,  "4h",  180),
    ("ALL",  None, "1d",  365),  # type: ignore[list-item]
]

_RANGE_HOURS:        dict[str, float | None] = {k: h  for k, h, _, _ in _RANGES}
_RANGE_KLINES:       dict[str, tuple[str, int]] = {k: (tf, n) for k, _, tf, n in _RANGES}
DEFAULT_RANGE = "ALL"
SESSION_KEY   = "monitor_range"


# ── Available options based on bot age ───────────────────────────────────────


def available_options(db: Database) -> list[str]:
    """Return range keys that fit the current equity curve's age. ALL always present."""
    curve = db.get_equity_curve()
    if len(curve) < 2:
        return ["ALL"]
    oldest_ts = pd.to_datetime(curve[0]["timestamp"])
    # Strip tz to compare against datetime.now() (local naive)
    if oldest_ts.tzinfo is not None:
        oldest_ts = oldest_ts.tz_localize(None)
    age_h = (datetime.now() - oldest_ts).total_seconds() / 3600

    out: list[str] = []
    for key, hours, _, _ in _RANGES:
        if hours is None:
            out.append(key)  # ALL always last
        elif age_h >= hours:
            out.append(key)
    if "ALL" not in out:
        out.append("ALL")
    return out


# ── Selector widget ──────────────────────────────────────────────────────────


def render_selector(db: Database) -> str:
    """Render the range pill selector and return the chosen range key.

    Persisted in `st.session_state[SESSION_KEY]` so all fragments see the
    same value across reruns.
    """
    options = available_options(db)

    # Initialise / clamp session value to a valid option
    current = st.session_state.get(SESSION_KEY, DEFAULT_RANGE)
    if current not in options:
        current = DEFAULT_RANGE
        st.session_state[SESSION_KEY] = current

    selected = st.radio(
        label="Range",
        options=options,
        index=options.index(current),
        horizontal=True,
        label_visibility="collapsed",
        key=f"_range_radio_{len(options)}",  # rerender when option set changes
    )
    if selected != st.session_state.get(SESSION_KEY):
        st.session_state[SESSION_KEY] = selected
    return selected


def current_range() -> str:
    """Return the current range key from session_state, defaulting to ALL."""
    return st.session_state.get(SESSION_KEY, DEFAULT_RANGE)


# ── Helpers consumed by chart sections ───────────────────────────────────────


def filter_curve_by_range(curve: list[dict], range_key: str) -> list[dict]:
    """Return only the equity_curve rows within the selected range."""
    hours = _RANGE_HOURS.get(range_key)
    if hours is None:
        return curve
    cutoff = datetime.now() - timedelta(hours=hours)
    out: list[dict] = []
    for row in curve:
        ts = pd.to_datetime(row["timestamp"])
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        if ts >= cutoff:
            out.append(row)
    return out


def klines_params_for_range(range_key: str) -> tuple[str, int]:
    """Map range key to (kline_timeframe, n_bars) for the live chart fetch."""
    return _RANGE_KLINES.get(range_key, _RANGE_KLINES["ALL"])
