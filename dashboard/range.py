"""Unified window selector for the MONITOR tab.

All three charts (equity, drawdown, live price) read the same window from
`st.session_state[SESSION_KEY]`. Each option maps to:

- `hours`: wall-clock window for the visible portion (None = ALL)
- `tf`: kline timeframe used by the live chart
- `visible_bars`: initial bars in view on the live chart
- `preload_bars`: bars actually fetched (≥ visible) — extra is the
  pan-back buffer so the user can drag left without refetching

Equity / drawdown don't filter the local curve — the full series is
loaded and `xaxis.range` clips the initial view, so pan-back is free.

Direct widget binding (`key=SESSION_KEY`) makes selection apply on a
single click instead of two — the old pattern (separate widget key +
manual copy) wrote to session_state AFTER fragments had already
rendered, requiring a second interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import streamlit as st

from bot.database.db import Database


@dataclass(frozen=True)
class RangeSpec:
    key: str
    hours: float | None        # None = ALL (no time filter / xaxis auto-fit)
    tf: str                    # kline interval for live chart
    visible_bars: int          # bars in initial view
    preload_bars: int          # bars actually fetched (Binance hard cap = 1000)


# Coverage column = preload_bars × tf (how far back pan-back can go)
_RANGES: tuple[RangeSpec, ...] = (
    RangeSpec("1H",   1,    "1m",   60, 600),    # 10h
    RangeSpec("4H",   4,    "5m",   48, 600),    # 50h
    RangeSpec("12H",  12,   "15m",  48, 600),    # 6d
    RangeSpec("24H",  24,   "1h",   24, 240),    # 10d
    RangeSpec("3D",   72,   "1h",   72, 720),    # 30d
    RangeSpec("7D",   168,  "1h",  168, 1000),   # 41d
    RangeSpec("30D",  720,  "4h",  180, 1000),   # 166d
    RangeSpec("90D",  2160, "4h",  540, 1000),   # 166d
    RangeSpec("1Y",   8760, "1d",  365, 1000),   # 2.7y
    RangeSpec("ALL",  None, "1d", 1000, 1000),   # 2.7y
)

_BY_KEY: dict[str, RangeSpec] = {r.key: r for r in _RANGES}
DEFAULT_RANGE = "ALL"
SESSION_KEY   = "monitor_range"


# ── Available options ────────────────────────────────────────────────────────


def available_options(db: Database) -> list[str]:
    """Return range keys that fit the current equity curve's age. ALL always present."""
    curve = db.get_equity_curve()
    if len(curve) < 2:
        return ["ALL"]
    oldest_ts = pd.to_datetime(curve[0]["timestamp"])
    if oldest_ts.tzinfo is not None:
        oldest_ts = oldest_ts.tz_localize(None)
    age_h = (datetime.now() - oldest_ts).total_seconds() / 3600

    out: list[str] = []
    for r in _RANGES:
        if r.hours is None:
            out.append(r.key)
        elif age_h >= r.hours:
            out.append(r.key)
    if "ALL" not in out:
        out.append("ALL")
    return out


# ── Selector widget ──────────────────────────────────────────────────────────


def render_selector(db: Database) -> str:
    """Render the radio with `key=SESSION_KEY` so selecting an option
    updates state on the same rerun (no two-click bug)."""
    options = available_options(db)
    # Clamp BEFORE the widget renders — Streamlit raises if the bound
    # state value isn't in `options`.
    if st.session_state.get(SESSION_KEY) not in options:
        st.session_state[SESSION_KEY] = DEFAULT_RANGE
    return st.radio(
        "Range",
        options=options,
        key=SESSION_KEY,
        horizontal=True,
        label_visibility="collapsed",
    )


def current_range() -> str:
    return st.session_state.get(SESSION_KEY, DEFAULT_RANGE)


def current_spec() -> RangeSpec:
    return _BY_KEY.get(current_range(), _BY_KEY[DEFAULT_RANGE])


# ── Helpers consumed by chart sections ───────────────────────────────────────


def window_xaxis_range(spec: RangeSpec | None = None) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(start, end) for `fig.update_xaxes(range=[...])`. None → auto-fit (ALL)."""
    spec = spec or current_spec()
    if spec.hours is None:
        return None
    end = pd.Timestamp.now()
    start = end - pd.Timedelta(hours=spec.hours)
    return start, end


def klines_params_for_range(spec: RangeSpec | None = None) -> tuple[str, int]:
    """(timeframe, preload_bars) for the live chart fetch."""
    spec = spec or current_spec()
    return spec.tf, spec.preload_bars


def visible_bars(spec: RangeSpec | None = None) -> int:
    spec = spec or current_spec()
    return spec.visible_bars
