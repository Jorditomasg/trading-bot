"""Equity curve section — refreshes every 30s."""

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from dashboard.constants import RED, WHITE, CAPTION, ChartConfig, RefreshRates
from dashboard.range import current_range, filter_curve_by_range
from dashboard.themes import NothingOS

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG


@st.fragment(run_every=RefreshRates.CHARTS)
def equity_chart_section(db: Database) -> None:
    full_curve = db.get_equity_curve()
    equity_curve = filter_curve_by_range(full_curve, current_range())

    if len(equity_curve) < 2:
        st.caption("waiting for data...")
        return

    # Reference line is the bot's true starting capital (first snapshot ever),
    # not the first one inside the filtered range — otherwise the "above
    # initial" highlight shifts as the user changes range.
    initial_balance = full_curve[0]["balance"]
    ts  = [r["timestamp"] for r in equity_curve]
    bal = [r["balance"]   for r in equity_curve]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=bal,
        mode="lines",
        line=dict(color=RED, width=ChartConfig.LINE_WIDTH),
        fill="tozeroy",
        fillcolor="rgba(255,0,0,0.04)",
        showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=ts, y=[b if b >= initial_balance else None for b in bal],
        mode="lines",
        line=dict(color=WHITE, width=ChartConfig.LINE_WIDTH),
        fill="tozeroy",
        fillcolor="rgba(245,245,245,0.04)",
        showlegend=False,
    ))
    fig.add_hline(y=initial_balance, line_dash="dot", line_color=CAPTION, line_width=1)
    fig.update_layout(**PLOTLY_LAYOUT, height=ChartConfig.HEIGHT_EQUITY)
    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
