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
from dashboard.sections.mini_cards import mini_cards_section
from dashboard.sections.open_position import drawdown_section, open_position_section
from dashboard.sections.performance import adaptive_params_section, performance_section
from dashboard.sections.signal_log import signal_log_section
from dashboard.constants import RefreshRates
from dashboard.themes import NothingOS
from dashboard.utils import _bias_badge, _regime_badge

DB_PATH = os.getenv("DB_PATH", "trading_bot.db")

st.set_page_config(
    page_title="BOT / Trading Dashboard",
    page_icon="*",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(NothingOS.NOTHING_CSS, unsafe_allow_html=True)


@st.cache_resource
def get_db() -> Database:
    return Database(DB_PATH)


def _bot_label(symbols: list[str]) -> str:
    if len(symbols) == 1:
        return f"BOT / {symbols[0]}"
    return f"BOT / {len(symbols)} SYMBOLS"


@st.fragment(run_every=RefreshRates.TOPBAR)
def _topbar(db: Database) -> None:
    recent_signals = db.get_recent_signals(1)
    last_regime    = recent_signals[0]["regime"]       if recent_signals else "RANGING"
    last_bias      = recent_signals[0].get("bias")     if recent_signals else None
    is_paused      = db.get_bot_paused()
    active_mode    = db.get_active_mode()
    last_ts        = datetime.now().strftime("%H:%M:%S")
    symbols        = db.get_symbols()

    mode_pill = (
        "<span class='pill pill-mainnet'>⚠ MAINNET</span>"
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
            <span class="bot-name"><span class="glyph">*</span> {_bot_label(symbols)}</span>
            {status_pill}
            {mode_pill}
            {_regime_badge(last_regime)}
            {_bias_badge(last_bias)}
            <span class="neu" style="font-size:0.65rem;letter-spacing:0.1em;margin-left:auto">{last_ts} UTC</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render() -> None:
    db = get_db()

    bar_col, exp_col = st.columns([14, 1])
    with bar_col:
        _topbar(db)
    with exp_col:
        with st.popover("⬇"):
            export_section(db)

    st.divider()

    tab_monitor, tab_config, tab_backtest = st.tabs(
        ["MONITOR", "CONFIG", "BACKTEST"]
    )

    with tab_monitor:
        # Persistent strip: 4 KPIs globales + cards mini per-symbol
        col_kpi, col_cards = st.columns([3, 4])
        with col_kpi:
            kpi_row_section(db)
        with col_cards:
            mini_cards_section(db)

        st.divider()

        # Global charts
        col_eq, col_dd = st.columns([3, 2])
        with col_eq:
            st.markdown("## Equity")
            equity_chart_section(db)
        with col_dd:
            st.markdown("## Drawdown")
            drawdown_section(db)

        st.divider()

        # Per-symbol tabs
        symbols = db.get_symbols()
        if not symbols:
            st.caption("No symbols configured — add symbols in CONFIG.")
        else:
            sym_tabs = st.tabs(symbols)
            for sym, sym_tab in zip(symbols, sym_tabs):
                with sym_tab:
                    st.markdown("## Live")
                    live_price_section(db, sym)

                    st.markdown("## State")
                    open_position_section(db, sym)
                    st.divider()

                    st.markdown("## Signals")
                    signal_log_section(db, sym)
                    st.divider()

                    performance_section(db, sym)

        st.divider()

        # Bot-wide adaptive params log
        adaptive_params_section(db)

    with tab_config:
        config_manager_section(db)

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
