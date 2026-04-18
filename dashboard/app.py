"""Streamlit dashboard — Nothing OS design language."""

import os
from datetime import datetime

import streamlit as st

from bot.database.db import Database
from dashboard.sections.equity_chart import equity_chart_section
from dashboard.sections.export import export_section
from dashboard.sections.kpi_row import kpi_row_section
from dashboard.sections.live_price import live_price_section
from dashboard.sections.open_position import drawdown_section, open_position_section
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


# ─── Topbar fragment (updates clock + mode every 5s) ──────────────────────────
@st.fragment(run_every=5)
def _topbar(db: Database) -> None:
    recent_signals = db.get_recent_signals(1)
    last_regime    = recent_signals[0]["regime"] if recent_signals else "RANGING"
    active_mode    = db.get_active_mode()
    last_ts        = datetime.now().strftime("%H:%M:%S")

    mode_pill = (
        "<span class='pill pill-stopped'>● MAINNET</span>"
        if active_mode == "MAINNET"
        else "<span class='pill pill-testnet'>● DEMO</span>"
    )

    st.markdown(
        f"""
        <div class="topbar">
            <span class="bot-name"><span class="glyph">*</span> BOT / BTC·USDT</span>
            <span class="pill pill-running">● RUNNING</span>
            {mode_pill}
            {_regime_badge(last_regime)}
            <span class="neu" style="font-size:0.65rem;letter-spacing:0.1em;margin-left:auto">{last_ts} UTC</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─── Render ───────────────────────────────────────────────────────────────────
def render() -> None:
    db = get_db()

    # Topbar + action buttons (export · config)
    bar_col, exp_col, cfg_col = st.columns([13, 1, 1])
    with bar_col:
        _topbar(db)
    with exp_col:
        with st.popover("⬇"):
            export_section(db)
    with cfg_col:
        with st.popover("⚙"):
            settings_section(db)

    st.divider()

    # Key metrics summary
    kpi_row_section(db)
    st.divider()

    # Live market context
    st.markdown("## Live")
    live_price_section(db)
    st.divider()

    # Charts row: equity + drawdown at the same fixed height (no height mismatch)
    col_eq, col_dd = st.columns([3, 2])
    with col_eq:
        st.markdown("## Equity")
        equity_chart_section(db)
    with col_dd:
        st.markdown("## Drawdown")
        drawdown_section(db)

    # State row: full width — regime, timeline, open position
    st.markdown("## State")
    open_position_section(db)

    st.divider()

    # Signal history
    st.markdown("## Signals")
    signal_log_section(db)
    st.divider()

    # Strategy performance breakdown
    performance_section(db)

    st.markdown(
        "<div style='text-align:right;font-size:0.55rem;color:#1A1A1A;"
        "letter-spacing:0.15em;margin-top:2rem'>* TRADING BOT · BINANCE · "
        "FRAGMENTS: 5s/10s/30s</div>",
        unsafe_allow_html=True,
    )


render()
