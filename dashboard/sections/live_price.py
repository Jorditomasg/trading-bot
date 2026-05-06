"""Live price section — refreshes every 5s.

The live chart always renders 1h candles (`LiveChart.TIMEFRAME`) with a fixed
buffer of `LiveChart.BARS` bars. It's intentionally decoupled from both
`settings.timeframe` (the bot's operational TF) and the MONITOR range selector:
the "Live" view exists to show fine-grained context (price action per hour),
regardless of what timeframe the bot trades on or what window the user picked
for equity / drawdown.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RED, WHITE, GRAY, GREEN, ChartConfig, RefreshRates, CacheTTL, LiveChart
from dashboard.themes import NothingOS
from dashboard.utils import fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG


@st.cache_data(ttl=CacheTTL.KLINES)
def get_klines_cached(symbol: str, timeframe: str, limit: int) -> list:
    """Fetch klines via REST. Cached per (symbol, timeframe, limit)."""
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


def _match_signal_to_bar(sig_ts_str: str, timestamps: pd.Series) -> int | None:
    """Return bar index for a signal timestamp, or None if outside the window."""
    if len(timestamps) == 0:
        return None
    sig_ts = pd.to_datetime(sig_ts_str)
    # Strip tz so naive timestamps from Binance compare cleanly with stored signals
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
    """Overlay Entry / SL / TP segments for each open position.

    Lines start at the trade's `entry_time` and extend to `chart_end` so each
    level is only painted across the bars where it was actually active.
    """
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
        records = get_klines_cached(symbol, LiveChart.TIMEFRAME, LiveChart.BARS)
        df_k    = pd.DataFrame(records)
        if df_k.empty:
            st.caption("chart data unavailable")
            return

        # Real Binance open_time. Fallback to a synthetic series only if the
        # column is missing (stale cache from before binance_client started
        # preserving open_time).
        if "open_time" in df_k.columns:
            timestamps = pd.to_datetime(df_k["open_time"]).reset_index(drop=True)
        else:
            end        = pd.Timestamp.now().floor("h")
            timestamps = pd.Series(pd.date_range(end=end, periods=len(df_k), freq="h"))

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

        # Show only the last ~50 bars on initial render — full buffer is loaded
        # so the user can pan left through the rest without any refetch.
        visible_bars = min(50, len(timestamps))
        x_start      = timestamps.iloc[-visible_bars]
        x_end        = timestamps.iloc[-1]
        fig.update_xaxes(range=[x_start, x_end], rangeslider=dict(visible=False))

        fig.update_layout(**PLOTLY_LAYOUT, height=ChartConfig.HEIGHT_LIVE, showlegend=False)
        st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
