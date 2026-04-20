"""Open position state + drawdown — refreshes every 10s."""

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from dashboard.constants import RED, REGIME_COLORS, ChartConfig, Thresholds, RefreshRates
from dashboard.themes import NothingOS
from dashboard.utils import _regime_badge, fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


def _render_regime_timeline(signals: list[dict]) -> None:
    """Render a compact horizontal CSS strip showing regime transitions (no Plotly)."""
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

    total = sum(c for _, c in runs)
    parts = []
    for regime, count in runs:
        pct   = count / total * 100
        color = REGIME_COLORS.get(regime, "#333")
        title = f"{regime} ({count})"
        parts.append(
            f'<div style="width:{pct:.1f}%;background:{color};height:5px" title="{title}"></div>'
        )

    st.markdown(
        '<div style="display:flex;width:100%;gap:1px;margin:6px 0 12px">'
        + "".join(parts)
        + "</div>",
        unsafe_allow_html=True,
    )


@st.fragment(run_every=RefreshRates.DRAWDOWN)
def drawdown_section(db: Database) -> None:
    """Drawdown chart — separate fragment so it can stand alone."""
    equity_curve = db.get_equity_curve()

    if len(equity_curve) < 2:
        st.caption("waiting for data...")
        return

    dd_ts  = [r["timestamp"] for r in equity_curve]
    dd_val = [r["drawdown"] * 100 for r in equity_curve]

    fig_dd = go.Figure()
    fig_dd.add_trace(go.Scatter(
        x=dd_ts, y=dd_val,
        mode="lines",
        line=dict(color=RED, width=ChartConfig.LINE_WIDTH),
        fill="tozeroy",
        fillcolor="rgba(255,0,0,0.08)",
        showlegend=False,
    ))
    fig_dd.add_hline(y=Thresholds.CIRCUIT_BREAKER_PCT, line_dash="dot", line_color="#333", line_width=1)
    fig_dd.update_layout(**PLOTLY_LAYOUT, height=ChartConfig.HEIGHT_DRAWDOWN)
    fig_dd.update_yaxes(
        gridcolor="#111",
        showline=False,
        zeroline=False,
        autorange="reversed",
        ticksuffix="%",
    )
    st.plotly_chart(fig_dd, use_container_width=True)


@st.fragment(run_every=RefreshRates.POSITION)
def open_position_section(db: Database) -> None:
    """Regime status + open trade details — compact horizontal layout."""
    open_trades    = db.get_open_trades()
    recent_signals = db.get_recent_signals(50)

    last_regime   = recent_signals[0]["regime"]   if recent_signals else "RANGING"
    last_strategy = recent_signals[0]["strategy"] if recent_signals else "—"

    col_regime, col_pos = st.columns([2, 3])

    with col_regime:
        st.markdown(
            f"Regime &nbsp; {_regime_badge(last_regime)} &nbsp;&nbsp; "
            f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>STRATEGY</span> "
            f"<code>{last_strategy}</code>",
            unsafe_allow_html=True,
        )
        _render_regime_timeline(recent_signals)

    with col_pos:
        if open_trades:
            for i, trade in enumerate(open_trades):
                if i > 0:
                    st.divider()
                entry    = trade["entry_price"]
                sl       = trade["stop_loss"]
                tp       = trade["take_profit"]
                side     = trade["side"]
                qty      = trade["quantity"]
                tf       = trade.get("timeframe", "1h")
                pill_cls = "pill-running" if side == "BUY" else "pill-stopped"

                st.markdown(
                    f"<span class='pill {pill_cls}'>{side}</span> &nbsp; "
                    f"<span style='font-size:0.8rem'>{qty:.5f} BTC</span> &nbsp; "
                    f"<code style='font-size:0.7rem;color:#888'>{tf}</code>",
                    unsafe_allow_html=True,
                )
                c1, c2, c3 = st.columns(3)
                active_sl = trade.get("trailing_sl") or sl
                c1.metric("Entry", f"${fmt(entry, ',.0f')}")
                c2.metric("SL",    f"${fmt(active_sl, ',.0f')}")
                c3.metric("TP",    f"${fmt(tp, ',.0f')}")
                st.caption(f"{trade['strategy']} · {trade['regime']}")
        else:
            st.markdown(
                "<span style='font-size:0.75rem;color:#333;letter-spacing:0.1em'>"
                "NO OPEN POSITIONS</span>",
                unsafe_allow_html=True,
            )
