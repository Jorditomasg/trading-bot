"""Live price section — refreshes every 5s.

The chart always renders in the bot's operating timeframe (`settings.timeframe`)
with a buffer of historical bars stored per-symbol in `st.session_state`.
A "Load more" button doubles the buffer up to the Binance API cap so the user
can pan back further without paying the full historical fetch on first load.

Decoupled from the MONITOR range selector on purpose: that selector controls
the equity / drawdown windows (P&L over time), not the candle chart context
(which only makes sense in the bot's own timeframe).
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.config import settings
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RED, WHITE, GRAY, GREEN, ChartConfig, RefreshRates, CacheTTL, LiveBars
from dashboard.themes import NothingOS
from dashboard.utils import fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG


@st.cache_data(ttl=CacheTTL.KLINES)
def get_klines_cached(symbol: str, timeframe: str, limit: int) -> list:
    """Fetch klines via REST. Cached per (symbol, timeframe, limit) — when the
    user clicks 'Load more' a new (symbol, tf, larger_limit) tuple is fetched
    and the previous buffer stays warm for instant re-renders."""
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


def _bars_session_key(symbol: str) -> str:
    return f"live_bars_{symbol}"


def _current_bars(symbol: str) -> int:
    return st.session_state.get(_bars_session_key(symbol), LiveBars.INITIAL)


def _match_signal_to_bar(sig_ts_str: str, timestamps: pd.Series) -> int | None:
    """Return bar index for a signal timestamp, or None if outside the window."""
    if len(timestamps) == 0:
        return None
    sig_ts = pd.to_datetime(sig_ts_str)
    diffs = (timestamps - sig_ts).abs()
    idx = int(diffs.values.argmin())
    if len(timestamps) >= 2:
        interval_secs = (timestamps.iloc[1] - timestamps.iloc[0]).total_seconds()
    else:
        interval_secs = 3600
    if diffs.iloc[idx].total_seconds() > interval_secs * 0.5:
        return None
    return idx


def _add_position_levels(
    fig: go.Figure,
    trades: list[dict],
    chart_end: pd.Timestamp,
) -> None:
    """Overlay Entry / SL / TP segments for each open position.

    Lines start at the trade's `entry_time` and extend to `chart_end` (the
    rightmost timestamp on the kline x-axis). This makes it visually clear
    when each level became active — a trade opened 6 hours ago doesn't get
    its SL/TP painted across earlier candles where it didn't exist.
    """
    for trade in trades:
        entry    = trade["entry_price"]
        sl       = trade["stop_loss"]
        tp       = trade["take_profit"]
        entry_ts = pd.to_datetime(trade["entry_time"])
        if entry_ts > chart_end:
            continue
        x0 = entry_ts

        for y, color, dash, label in (
            (entry, WHITE, "dash", f"ENTRY  ${fmt(entry, ',.0f')}"),
            (tp,    GREEN, "dot",  f"TP  ${fmt(tp, ',.0f')}"),
            (sl,    RED,   "dot",  f"SL  ${fmt(sl, ',.0f')}"),
        ):
            fig.add_shape(
                type="line",
                x0=x0, x1=chart_end,
                y0=y,  y1=y,
                line=dict(color=color, dash=dash, width=ChartConfig.LINE_WIDTH),
            )
            fig.add_annotation(
                x=x0, y=y,
                text=label,
                showarrow=False,
                xanchor="left",
                yanchor="bottom",
                font=dict(color=color, size=10),
                bgcolor="rgba(10,10,10,0.6)",
            )


@st.fragment(run_every=RefreshRates.LIVE_PRICE)
def live_price_section(db: Database, symbol: str) -> None:
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
    col_live, col_chart = st.columns([1, 3])

    with col_live:
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

    with col_chart:
        tf      = settings.timeframe
        n_bars  = _current_bars(symbol)
        records = get_klines_cached(symbol, tf, n_bars)
        df_k    = pd.DataFrame(records)
        if df_k.empty:
            st.caption("chart data unavailable")
            return

        # Real Binance open_time — no synthetic timestamps. Falls back to a
        # synthetic series only if the column is missing (e.g. old cache hit
        # before binance_client started preserving open_time).
        if "open_time" in df_k.columns:
            timestamps = pd.to_datetime(df_k["open_time"])
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
        signals = db.get_recent_signals(50, symbol=symbol)
        buy_sigs  = [s for s in signals if s["action"] == "BUY"]
        sell_sigs = [s for s in signals if s["action"] == "SELL"]

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

        fig.add_hline(
            y=live_price,
            line_dash="dash",
            line_color=GRAY,
            line_width=ChartConfig.LINE_WIDTH_THIN,
            annotation_text=f"${fmt(live_price, ',.0f')}",
            annotation_position="right",
        )

        if open_trades:
            _add_position_levels(fig, open_trades, chart_end=timestamps.iloc[-1])

        fig.update_layout(**PLOTLY_LAYOUT, height=ChartConfig.HEIGHT_LIVE, showlegend=False)
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)

        # Buffer controls — sit below the chart, kept compact
        loaded     = len(df_k)
        at_max     = loaded >= LiveBars.MAX
        ctrl_left, ctrl_caption = st.columns([1, 4])
        with ctrl_left:
            if st.button(
                "← Load more",
                key=f"load_more_{symbol}",
                disabled=at_max,
                use_container_width=True,
            ):
                new_bars = min(loaded + LiveBars.STEP, LiveBars.MAX)
                st.session_state[_bars_session_key(symbol)] = new_bars
                st.rerun(scope="fragment")
        with ctrl_caption:
            cap_note = " (max)" if at_max else ""
            st.caption(f"{loaded} {tf} bars{cap_note}")
