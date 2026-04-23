"""Streamlit dashboard — Nothing OS design language."""

import os
from datetime import datetime

import streamlit as st

from bot.database.db import Database
from dashboard.sections.backtest_runner import backtest_runner_section
from dashboard.sections.scenario_compare import scenario_compare_section
from dashboard.sections.config_manager import config_manager_section
from dashboard.sections.equity_chart import equity_chart_section
from dashboard.sections.export import export_section
from dashboard.sections.kpi_row import kpi_row_section
from dashboard.sections.live_price import live_price_section
from dashboard.sections.open_position import drawdown_section, open_position_section
from dashboard.sections.performance import performance_section
from dashboard.sections.signal_log import signal_log_section
from dashboard.constants import RefreshRates
from dashboard.themes import NothingOS
from dashboard.utils import _bias_badge, _regime_badge

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


# ─── Topbar fragment (updates clock + mode + bias every 5s) ───────────────────
@st.fragment(run_every=RefreshRates.TOPBAR)
def _topbar(db: Database) -> None:
    recent_signals = db.get_recent_signals(1)
    last_regime    = recent_signals[0]["regime"]       if recent_signals else "RANGING"
    last_bias      = recent_signals[0].get("bias")     if recent_signals else None
    is_paused      = db.get_bot_paused()
    active_mode    = db.get_active_mode()
    last_ts        = datetime.now().strftime("%H:%M:%S")

    mode_pill = (
        "<span class='pill pill-stopped'>● MAINNET</span>"
        if active_mode == "MAINNET"
        else "<span class='pill pill-testnet'>● DEMO</span>"
    )
    status_pill = (
        "<span class='pill pill-testnet'>⏸ PAUSED</span>"
        if is_paused
        else "<span class='pill pill-running'>● RUNNING</span>"
    )

    st.markdown(
        f"""
        <div class="topbar">
            <span class="bot-name"><span class="glyph">*</span> BOT / BTC·USDT</span>
            {status_pill}
            {mode_pill}
            {_regime_badge(last_regime)}
            {_bias_badge(last_bias)}
            <span class="neu" style="font-size:0.65rem;letter-spacing:0.1em;margin-left:auto">{last_ts} UTC</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─── Render ───────────────────────────────────────────────────────────────────
def render() -> None:
    db = get_db()

    # Topbar + action buttons — always visible across all tabs
    bar_col, exp_col = st.columns([14, 1])
    with bar_col:
        _topbar(db)
    with exp_col:
        with st.popover("⬇"):
            export_section(db)

    st.divider()

    # ── Navigation tabs ────────────────────────────────────────────────────
    tab_monitor, tab_config, tab_backtest = st.tabs(
        ["MONITOR", "CONFIG", "BACKTEST"]
    )

    # ── MONITOR ────────────────────────────────────────────────────────────
    with tab_monitor:
        kpi_row_section(db)

        col_eq, col_dd = st.columns([3, 2])
        with col_eq:
            st.markdown("## Equity")
            equity_chart_section(db)
        with col_dd:
            st.markdown("## Drawdown")
            drawdown_section(db)

        st.divider()

        st.markdown("## Live")
        live_price_section(db)

        st.markdown("## State")
        open_position_section(db)
        st.divider()

        st.markdown("## Signals")
        signal_log_section(db)
        st.divider()

        performance_section(db)

    # ── CONFIG ─────────────────────────────────────────────────────────────
    with tab_config:
        config_manager_section(db)

    # ── BACKTEST ───────────────────────────────────────────────────────────
    with tab_backtest:
        sub_backtest, sub_compare = st.tabs(["BACKTEST", "COMPARE"])
        with sub_backtest:
            backtest_runner_section(db)
        with sub_compare:
            scenario_compare_section()

    st.markdown(
        "<div style='text-align:right;font-size:0.55rem;color:#1A1A1A;"
        "letter-spacing:0.15em;margin-top:2rem'>* TRADING BOT · BINANCE · "
        "FRAGMENTS: 5s/10s/30s</div>",
        unsafe_allow_html=True,
    )


render()
