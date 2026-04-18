"""Equity curve section — refreshes every 30s."""

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from dashboard.themes import NothingOS

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


@st.fragment(run_every=30)
def equity_chart_section(db: Database) -> None:
    equity_curve = db.get_equity_curve()

    if len(equity_curve) < 2:
        st.caption("waiting for data...")
        return

    initial_balance = equity_curve[0]["balance"]
    ts  = [r["timestamp"] for r in equity_curve]
    bal = [r["balance"]   for r in equity_curve]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=ts, y=bal,
        mode="lines",
        line=dict(color="#FF0000", width=1.5),
        fill="tozeroy",
        fillcolor="rgba(255,0,0,0.04)",
        showlegend=False,
    ))
    fig.add_trace(go.Scatter(
        x=ts, y=[b if b >= initial_balance else None for b in bal],
        mode="lines",
        line=dict(color="#F5F5F5", width=1.5),
        fill="tozeroy",
        fillcolor="rgba(245,245,245,0.04)",
        showlegend=False,
    ))
    fig.add_hline(y=initial_balance, line_dash="dot", line_color="#333", line_width=1)
    fig.update_layout(**PLOTLY_LAYOUT, height=220)
    st.plotly_chart(fig, use_container_width=True)
