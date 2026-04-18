"""Open position + state + drawdown — refreshes every 10s."""

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from dashboard.themes import NothingOS
from dashboard.utils import _regime_badge

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT

_REGIME_COLORS = {
    "TRENDING": "#F5F5F5",
    "RANGING":  "#555555",
    "VOLATILE": "#FF0000",
}


def _render_regime_timeline(signals: list[dict]) -> None:
    """Render a compact horizontal stacked bar showing regime transitions."""
    if len(signals) < 2:
        return
    ordered = list(reversed(signals))
    runs: list[tuple[str, int]] = []
    for sig in ordered:
        regime = sig.get("regime", "RANGING")
        if runs and runs[-1][0] == regime:
            runs[-1] = (regime, runs[-1][1] + 1)
        else:
            runs.append((regime, 1))
    fig = go.Figure()
    for regime, count in runs:
        fig.add_trace(go.Bar(
            x=[count], y=["regime"],
            orientation="h",
            marker_color=_REGIME_COLORS.get(regime, "#333"),
            showlegend=False,
            hovertemplate=f"{regime} ({count})<extra></extra>",
        ))
    layout = {
        **PLOTLY_LAYOUT,
        "margin": dict(l=0, r=0, t=0, b=0),
        "height": 30,
        "barmode": "stack",
        "xaxis": dict(visible=False),
        "yaxis": dict(visible=False),
    }
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)


@st.fragment(run_every=10)
def open_position_section(db: Database) -> None:
    equity_curve   = db.get_equity_curve()
    open_trade     = db.get_open_trade()
    recent_signals = db.get_recent_signals(50)

    last_regime   = recent_signals[0]["regime"]   if recent_signals else "RANGING"
    last_strategy = recent_signals[0]["strategy"] if recent_signals else "—"

    st.markdown(
        f"Regime &nbsp; {_regime_badge(last_regime)} &nbsp;&nbsp; "
        f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>STRATEGY</span> "
        f"<code>{last_strategy}</code>",
        unsafe_allow_html=True,
    )
    _render_regime_timeline(recent_signals)
    st.markdown("## Drawdown")
    if len(equity_curve) >= 2:
        dd_ts  = [r["timestamp"] for r in equity_curve]
        dd_val = [r["drawdown"] * 100 for r in equity_curve]
        fig_dd = go.Figure()
        fig_dd.add_trace(go.Scatter(
            x=dd_ts, y=dd_val,
            mode="lines",
            line=dict(color="#FF0000", width=1.5),
            fill="tozeroy",
            fillcolor="rgba(255,0,0,0.08)",
            showlegend=False,
        ))
        fig_dd.add_hline(y=15.0, line_dash="dot", line_color="#333", line_width=1)
        fig_dd.update_layout(**PLOTLY_LAYOUT, height=160)
        fig_dd.update_yaxes(
            gridcolor="#111",
            showline=False,
            zeroline=False,
            autorange="reversed",
            ticksuffix="%",
        )
        st.plotly_chart(fig_dd, use_container_width=True)
    else:
        st.caption("waiting for data...")

    st.markdown("")

    if open_trade:
        entry    = open_trade["entry_price"]
        sl       = open_trade["stop_loss"]
        tp       = open_trade["take_profit"]
        side     = open_trade["side"]
        qty      = open_trade["quantity"]
        pill_cls = "pill-running" if side == "BUY" else "pill-stopped"

        st.markdown(
            f"<span class='pill {pill_cls}'>{side}</span> &nbsp; "
            f"<span style='font-size:0.8rem'>{qty:.5f} BTC</span>",
            unsafe_allow_html=True,
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Entry", f"${entry:,.0f}")
        c2.metric("SL",    f"${sl:,.0f}")
        c3.metric("TP",    f"${tp:,.0f}")
        st.caption(f"{open_trade['strategy']} · {open_trade['regime']}")
    else:
        st.markdown(
            "<span style='font-size:0.75rem;color:#333;letter-spacing:0.1em'>"
            "NO OPEN POSITION</span>",
            unsafe_allow_html=True,
        )
