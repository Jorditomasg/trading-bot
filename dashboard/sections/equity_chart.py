"""Equity curve section — refreshes every 30s.

Loads the full equity series and clips the visible window via `xaxis.range`.
Pan-back to older history is free because all points are already in the
figure. Yaxis is pinned to the visible window's min/max so old extremes
don't compress the recent series; on ALL we let Plotly autoscale.
"""

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from dashboard.constants import RED, WHITE, CAPTION, ChartConfig, RefreshRates
from dashboard.range import current_range, slice_by_window, window_xaxis_range
from dashboard.themes import NothingOS

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG


@st.fragment(run_every=RefreshRates.CHARTS)
def equity_chart_section(db: Database) -> None:
    full_curve = db.get_equity_curve()
    if len(full_curve) < 2:
        st.caption("waiting for data...")
        return

    initial_balance = full_curve[0]["balance"]
    ts  = [r["timestamp"] for r in full_curve]
    bal = [r["balance"]   for r in full_curve]

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
    fig.update_layout(
        **PLOTLY_LAYOUT,
        height=ChartConfig.HEIGHT_EQUITY,
        uirevision=f"equity_{current_range()}",
    )

    window = window_xaxis_range()
    if window is not None:
        visible = slice_by_window(full_curve, bal, window)
        if visible:
            y_min, y_max = min(visible), max(visible)
            margin = max((y_max - y_min) * 0.05, y_max * 0.001)
            fig.update_xaxes(range=list(window))
            fig.update_yaxes(range=[y_min - margin, y_max + margin], autorange=False)

    st.plotly_chart(fig, use_container_width=True, config=PLOTLY_CONFIG)
