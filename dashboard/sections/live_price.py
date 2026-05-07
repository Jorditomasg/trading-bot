"""Live price section — split into two fragments at different cadences.

`_live_metric_fragment` (5s) renders the live tick + unrealized P&L on the
left column. Cheap to refresh: a metric widget, not a Plotly figure.

`_live_chart_fragment` (3 min) renders the candlestick chart on the right.
Refreshing the chart faster causes Plotly re-renders that flicker and
trash interaction state — and the chart is honestly historical, so there
is no value in pretending it is live. The live price lives in the left
column; the chart shows fully-closed candles from Binance.

Window selection comes from the unified `dashboard.range` selector. The
chart preloads `preload_bars` and clips the initial view via `xaxis.range`,
so pan-back within the buffer is free. `uirevision` keyed on
(symbol, range) preserves zoom/pan across fragment refreshes.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RED, WHITE, GREEN, ChartConfig, RefreshRates, CacheTTL
from dashboard.range import current_spec, klines_params_for_range, visible_bars
from dashboard.themes import NothingOS
from dashboard.utils import fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG


@st.cache_data(ttl=CacheTTL.KLINES)
def get_klines_cached(symbol: str, timeframe: str, limit: int) -> list:
    try:
        client = BinanceClient()
        df = client.get_klines(symbol, timeframe, limit)
        return df.to_dict("records")
    except Exception:
        return []


@st.cache_data(ttl=CacheTTL.LIVE_PRICE)
def get_rest_price(symbol: str) -> float | None:
    try:
        client = BinanceClient()
        return client.get_ticker_price(symbol)
    except Exception:
        return None


def _match_signal_to_bar(sig_ts_str: str, timestamps: pd.Series) -> int | None:
    if len(timestamps) == 0:
        return None
    sig_ts = pd.to_datetime(sig_ts_str)
    if getattr(sig_ts, "tzinfo", None) is not None:
        sig_ts = sig_ts.tz_localize(None)
    diffs = (timestamps - sig_ts).abs()
    idx = int(diffs.values.argmin())
    interval_secs = (
        (timestamps.iloc[1] - timestamps.iloc[0]).total_seconds()
        if len(timestamps) >= 2 else 3600
    )
    if diffs.iloc[idx].total_seconds() > interval_secs * 0.5:
        return None
    return idx


def _add_position_levels(
    fig: go.Figure,
    trades: list[dict],
    chart_end: pd.Timestamp,
) -> None:
    """Overlay Entry / SL / TP segments — only across bars where each level was active."""
    for trade in trades:
        entry    = trade["entry_price"]
        sl       = trade["stop_loss"]
        tp       = trade["take_profit"]
        entry_ts = pd.to_datetime(trade["entry_time"])
        if getattr(entry_ts, "tzinfo", None) is not None:
            entry_ts = entry_ts.tz_localize(None)
        if entry_ts > chart_end:
            continue

        for y, color, dash, label in (
            (entry, WHITE, "dash", f"ENTRY  ${fmt(entry, ',.0f')}"),
            (tp,    GREEN, "dot",  f"TP  ${fmt(tp, ',.0f')}"),
            (sl,    RED,   "dot",  f"SL  ${fmt(sl, ',.0f')}"),
        ):
            fig.add_shape(
                type="line",
                x0=entry_ts, x1=chart_end,
                y0=y,        y1=y,
                line=dict(color=color, dash=dash, width=ChartConfig.LINE_WIDTH),
            )
            fig.add_annotation(
                x=entry_ts, y=y,
                text=label,
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color=color, size=10),
                bgcolor="rgba(10,10,10,0.6)",
            )


@st.fragment(run_every=RefreshRates.LIVE_PRICE)
def _live_metric_fragment(db: Database, symbol: str) -> None:
    tick        = db.get_live_tick(symbol)
    open_trades = db.get_open_trades(symbol=symbol)

    if tick is None:
        price = get_rest_price(symbol)
        if price is not None:
            tick = {"price": price}

    if tick is None:
        st.caption("live feed connecting...")
        return

    live_price = tick["price"]
    st.metric(f"{symbol} LIVE", f"${fmt(live_price)}")
    if open_trades:
        total_upnl = 0.0
        for trade in open_trades:
            entry = trade["entry_price"]
            qty   = trade["quantity"]
            side  = trade["side"]
            sign  = 1 if side == "BUY" else -1
            total_upnl += sign * (live_price - entry) * qty

        upnl_pct = total_upnl / live_price * 100
        st.metric(
            "Unrealized P&L",
            f"${fmt(total_upnl, '+.4f')}",
            delta=f"{fmt(upnl_pct, '+.2f')}%",
        )
        if len(open_trades) > 1:
            st.caption(f"{len(open_trades)} open positions")


@st.fragment(run_every=RefreshRates.LIVE_CHART)
def _live_chart_fragment(db: Database, symbol: str) -> None:
    spec        = current_spec()
    open_trades = db.get_open_trades(symbol=symbol)

    tf, n_bars = klines_params_for_range(spec)
    records    = get_klines_cached(symbol, tf, n_bars)
    df_k       = pd.DataFrame(records)
    if df_k.empty:
        st.caption("chart data unavailable")
        return

    if "open_time" in df_k.columns:
        timestamps = pd.to_datetime(df_k["open_time"]).reset_index(drop=True)
    else:
        freq_map   = {"1m": "min", "5m": "5min", "15m": "15min", "30m": "30min",
                      "1h": "h", "2h": "2h", "4h": "4h", "1d": "D", "1w": "W"}
        freq       = freq_map.get(tf, "h")
        end        = pd.Timestamp.now().floor(freq)
        timestamps = pd.Series(pd.date_range(end=end, periods=len(df_k), freq=freq))

    fig = go.Figure(data=go.Candlestick(
        x=timestamps,
        open=df_k["open"],
        high=df_k["high"],
        low=df_k["low"],
        close=df_k["close"],
        increasing_line_color=WHITE,
        decreasing_line_color=RED,
    ))

    signals    = db.get_recent_signals(50, symbol=symbol)
    buy_sigs   = [s for s in signals if s["action"] == "BUY"]
    sell_sigs  = [s for s in signals if s["action"] == "SELL"]

    bx, by = [], []
    for s in buy_sigs:
        idx = _match_signal_to_bar(s["timestamp"], timestamps)
        if idx is not None:
            bx.append(timestamps.iloc[idx])
            by.append(float(df_k["low"].iloc[idx]) * 0.999)
    if bx:
        fig.add_trace(go.Scatter(
            x=bx, y=by, mode="markers",
            marker=dict(symbol="triangle-up", size=ChartConfig.MARKER_SIZE, color=GREEN, opacity=ChartConfig.MARKER_OPACITY),
            showlegend=False, hoverinfo="skip",
        ))

    sx, sy = [], []
    for s in sell_sigs:
        idx = _match_signal_to_bar(s["timestamp"], timestamps)
        if idx is not None:
            sx.append(timestamps.iloc[idx])
            sy.append(float(df_k["high"].iloc[idx]) * 1.001)
    if sx:
        fig.add_trace(go.Scatter(
            x=sx, y=sy, mode="markers",
            marker=dict(symbol="triangle-down", size=ChartConfig.MARKER_SIZE, color=RED, opacity=ChartConfig.MARKER_OPACITY),
            showlegend=False, hoverinfo="skip",
        ))

    if open_trades:
        _add_position_levels(fig, open_trades, chart_end=timestamps.iloc[-1])

    n_vis   = min(visible_bars(spec), len(timestamps))
    x_start = timestamps.iloc[-n_vis]
    x_end   = timestamps.iloc[-1]

    fig.update_layout(
        **PLOTLY_LAYOUT,
        height=ChartConfig.HEIGHT_LIVE,
        showlegend=False,
        uirevision=f"live_{symbol}_{spec.key}",
        xaxis=dict(
            **PLOTLY_LAYOUT["xaxis"],
            range=[x_start, x_end],
            rangeslider=dict(visible=False),
        ),
    )
    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)


def live_price_section(db: Database, symbol: str) -> None:
    col_live, col_chart = st.columns([1, 3])
    with col_live:
        _live_metric_fragment(db, symbol)
    with col_chart:
        _live_chart_fragment(db, symbol)
