"""Live price section — refreshes every 5s."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.config import settings
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RED, WHITE, GRAY, GREEN, ChartConfig, RefreshRates, CacheTTL
from dashboard.themes import NothingOS
from dashboard.utils import fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


@st.cache_data(ttl=CacheTTL.KLINES)
def get_klines_cached(symbol: str, timeframe: str, limit: int = 50) -> list:
    try:
        client = BinanceClient()
        df = client.get_klines(symbol, timeframe, limit)
        return df.to_dict("records")
    except Exception:
        return []


@st.cache_data(ttl=CacheTTL.LIVE_PRICE)
def get_rest_price(symbol: str) -> float | None:
    """REST ticker price — fallback when WebSocket has not written yet."""
    try:
        client = BinanceClient()
        return client.get_ticker_price(symbol)
    except Exception:
        return None


_TIMEFRAME_FREQ_MAP = {
    "1m": "min", "3m": "3min", "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h",
    "1d": "D", "3d": "3D", "1w": "W",
}


def _kline_timestamps(limit: int, timeframe: str) -> pd.DatetimeIndex:
    """Compute synthetic open-time timestamps for kline bars."""
    freq = _TIMEFRAME_FREQ_MAP.get(timeframe, "h")
    end = pd.Timestamp.now().floor(freq)
    return pd.date_range(end=end, periods=limit, freq=freq)


def _match_signal_to_bar(sig_ts_str: str, timestamps: pd.DatetimeIndex) -> int | None:
    """Return bar index for a signal timestamp, or None if outside the window."""
    sig_ts = pd.to_datetime(sig_ts_str)
    diffs = abs(timestamps - sig_ts)
    idx = int(diffs.argmin())
    interval_secs = (timestamps[1] - timestamps[0]).total_seconds()
    if diffs[idx].total_seconds() > interval_secs * 0.5:
        return None
    return idx


@st.fragment(run_every=RefreshRates.LIVE_PRICE)
def live_price_section(db: Database) -> None:
    tick       = db.get_live_tick(settings.symbol)
    open_trade = db.get_open_trade()

    if tick is None:
        price = get_rest_price(settings.symbol)
        if price is not None:
            tick = {"price": price}

    if tick is None:
        st.caption("live feed connecting...")
        return

    live_price = tick["price"]
    col_live, col_chart = st.columns([1, 3])

    with col_live:
        st.metric("BTC/USDT LIVE", f"${fmt(live_price)}")
        if open_trade:
            entry    = open_trade["entry_price"]
            qty      = open_trade["quantity"]
            side     = open_trade["side"]
            sign     = 1 if side == "BUY" else -1
            upnl     = sign * (live_price - entry) * qty
            upnl_pct = sign * (live_price - entry) / entry * 100
            st.metric(
                "Unrealized P&L",
                f"${fmt(upnl, '+.4f')}",
                delta=f"{fmt(upnl_pct, '+.2f')}%",
            )

    with col_chart:
        records = get_klines_cached(settings.symbol, settings.timeframe)
        df_k = pd.DataFrame(records)
        if df_k.empty:
            st.caption("chart data unavailable")
            return
        timestamps = _kline_timestamps(len(df_k), settings.timeframe)
        fig = go.Figure(data=go.Candlestick(
            x=timestamps,
            open=df_k["open"],
            high=df_k["high"],
            low=df_k["low"],
            close=df_k["close"],
            increasing_line_color=WHITE,
            decreasing_line_color=RED,
        ))
        signals = db.get_recent_signals(50)
        buy_sigs  = [s for s in signals if s["action"] == "BUY"]
        sell_sigs = [s for s in signals if s["action"] == "SELL"]

        bx, by = [], []
        for s in buy_sigs:
            idx = _match_signal_to_bar(s["timestamp"], timestamps)
            if idx is not None:
                bx.append(timestamps[idx])
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
                sx.append(timestamps[idx])
                sy.append(float(df_k["high"].iloc[idx]) * 1.001)
        if sx:
            fig.add_trace(go.Scatter(
                x=sx, y=sy, mode="markers",
                marker=dict(symbol="triangle-down", size=ChartConfig.MARKER_SIZE, color=RED, opacity=ChartConfig.MARKER_OPACITY),
                showlegend=False, hoverinfo="skip",
            ))

        fig.add_hline(
            y=live_price,
            line_dash="dash",
            line_color=GRAY,
            line_width=ChartConfig.LINE_WIDTH_THIN,
            annotation_text=f"${fmt(live_price, ',.0f')}",
            annotation_position="right",
        )
        fig.update_layout(**PLOTLY_LAYOUT, height=ChartConfig.HEIGHT_LIVE, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
