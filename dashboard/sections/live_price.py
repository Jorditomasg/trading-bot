"""Live price section — refreshes every 5s."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.config import settings
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.themes import NothingOS

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


@st.cache_data(ttl=300)
def get_klines_cached(symbol: str, timeframe: str, limit: int = 50) -> list:
    try:
        client = BinanceClient()
        df = client.get_klines(symbol, timeframe, limit)
        return df.to_dict("records")
    except Exception:
        return []


@st.cache_data(ttl=5)
def get_rest_price(symbol: str) -> float | None:
    """REST ticker price — fallback when WebSocket has not written yet."""
    try:
        client = BinanceClient()
        return client.get_ticker_price(symbol)
    except Exception:
        return None


@st.fragment(run_every=5)
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
        st.metric("BTC/USDT LIVE", f"${live_price:,.2f}")
        if open_trade:
            entry    = open_trade["entry_price"]
            qty      = open_trade["quantity"]
            side     = open_trade["side"]
            sign     = 1 if side == "BUY" else -1
            upnl     = sign * (live_price - entry) * qty
            upnl_pct = sign * (live_price - entry) / entry * 100
            st.metric(
                "Unrealized P&L",
                f"${upnl:+.4f}",
                delta=f"{upnl_pct:+.2f}%",
            )

    with col_chart:
        records = get_klines_cached(settings.symbol, settings.timeframe)
        df_k = pd.DataFrame(records)
        if df_k.empty:
            st.caption("chart data unavailable")
            return
        fig = go.Figure(data=go.Candlestick(
            x=df_k.index,
            open=df_k["open"],
            high=df_k["high"],
            low=df_k["low"],
            close=df_k["close"],
            increasing_line_color="#F5F5F5",
            decreasing_line_color="#FF0000",
        ))
        fig.add_hline(
            y=live_price,
            line_dash="dash",
            line_color="#888",
            line_width=1,
            annotation_text=f"${live_price:,.0f}",
            annotation_position="right",
        )
        fig.update_layout(**PLOTLY_LAYOUT, height=200, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)
