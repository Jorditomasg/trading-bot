"""Streamlit dashboard — Nothing OS design language."""

import os
from datetime import datetime

import streamlit as st

from bot.database.db import Database
from dashboard.sections.equity_chart import equity_chart_section
from dashboard.sections.kpi_row import kpi_row_section
from dashboard.sections.live_price import live_price_section
from dashboard.sections.open_position import open_position_section
from dashboard.sections.performance import performance_section
from dashboard.sections.settings import settings_section
from dashboard.sections.signal_log import signal_log_section
from dashboard.themes import NothingOS
from dashboard.utils import _regime_badge

DB_PATH = os.getenv("DB_PATH", "trading_bot.db")

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="BOT / BTC-USDT",
    page_icon="*",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─── Nothing OS styles ────────────────────────────────────────────────────────
st.markdown(NothingOS.NOTHING_CSS, unsafe_allow_html=True)


# ─── DB ───────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_db() -> Database:
    return Database(DB_PATH)


# ─── Render ───────────────────────────────────────────────────────────────────
def render() -> None:
    db = get_db()

    recent_signals = db.get_recent_signals(1)
    last_regime    = recent_signals[0]["regime"]   if recent_signals else "RANGING"
    last_strategy  = recent_signals[0]["strategy"] if recent_signals else "—"
    last_ts        = datetime.now().strftime("%Y-%m-%d %H:%M")

    st.markdown(
        f"""
        <div class="topbar">
            <span class="bot-name"><span class="glyph">*</span> BOT / BTC·USDT</span>
            <span class="pill pill-running">● RUNNING</span>
            <span class="pill pill-testnet">TESTNET</span>
            {_regime_badge(last_regime)}
            <span class="neu" style="font-size:0.65rem;letter-spacing:0.1em;margin-left:auto">{last_ts} UTC</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()

    kpi_row_section(db)
    st.divider()

    st.markdown("## Live Price")
    live_price_section(db)
    st.divider()

    col_eq, col_state = st.columns([3, 2])
    with col_eq:
        st.markdown("## Equity Curve")
        equity_chart_section(db)
    with col_state:
        st.markdown("## State")
        open_position_section(db)
    st.divider()

    performance_section(db)
    st.divider()

    st.markdown("## Signal Log")
    signal_log_section(db)
    st.divider()

    with st.expander("⚙ Configuration", expanded=False):
        settings_section(db)

    st.markdown(
        "<div style='text-align:right;font-size:0.55rem;color:#1A1A1A;"
        "letter-spacing:0.15em;margin-top:2rem'>* TRADING BOT · BINANCE TESTNET · "
        "FRAGMENTS: 5s/10s/30s</div>",
        unsafe_allow_html=True,
    )


render()
