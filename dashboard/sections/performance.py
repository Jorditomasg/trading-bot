"""Strategy/regime performance + trade history — refreshes every 30s."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from bot.metrics import profit_factor, max_consecutive_losses
from dashboard.constants import RED, WHITE, MUTED, REGIME_COLORS, ChartConfig, Thresholds, RefreshRates
from dashboard.themes import NothingOS
from dashboard.utils import fmt, parse_fmt

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


@st.fragment(run_every=RefreshRates.PERFORMANCE)
def performance_section(db: Database) -> None:
    strategy_perf = db.get_performance_by_strategy()
    regime_perf   = db.get_performance_by_regime()
    trades        = db.get_all_trades()
    closed        = [t for t in trades if t.get("exit_price") is not None]
    wins          = sum(1 for t in closed if t.get("pnl") and t["pnl"] > 0)

    col_strat, col_hist, col_risk = st.columns(3)

    with col_strat:
        st.markdown("## Strategy Performance")
        if strategy_perf:
            df_p       = pd.DataFrame(strategy_perf)
            bar_colors = [WHITE if wr >= Thresholds.WIN_RATE_MID else RED for wr in df_p["win_rate"]]
            fig = go.Figure(go.Bar(
                x=df_p["win_rate"],
                y=df_p["strategy"],
                orientation="h",
                marker_color=bar_colors,
                marker_line_width=0,
                text=[f"{wr:.0f}%  ({t}T)" for wr, t in zip(df_p["win_rate"], df_p["total_trades"])],
                textfont=dict(family="Space Mono", size=10, color="#0A0A0A"),
                textposition="inside",
            ))
            fig.add_vline(x=Thresholds.WIN_RATE_MID, line_dash="dot", line_color="#333", line_width=1)
            fig.update_layout(
                **PLOTLY_LAYOUT,
                xaxis=dict(range=[0, 100], gridcolor="#111", showline=False, zeroline=False),
                height=ChartConfig.HEIGHT_PERFORMANCE,
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("no closed trades yet")

    with col_hist:
        st.markdown("## P&L Distribution")
        if closed:
            pnl_vals = [t["pnl"] for t in closed if t.get("pnl") is not None]
            pos_vals = [v for v in pnl_vals if v >= 0]
            neg_vals = [v for v in pnl_vals if v < 0]
            fig_hist = go.Figure()
            if neg_vals:
                fig_hist.add_trace(go.Histogram(
                    x=neg_vals, name="Loss", marker_color=RED,
                    opacity=0.85, nbinsx=15,
                ))
            if pos_vals:
                fig_hist.add_trace(go.Histogram(
                    x=pos_vals, name="Win", marker_color=WHITE,
                    opacity=0.85, nbinsx=15,
                ))
            fig_hist.add_vline(x=0, line_color="#555", line_width=1)
            fig_hist.update_layout(
                **PLOTLY_LAYOUT, barmode="overlay", showlegend=False, height=ChartConfig.HEIGHT_HIST,
            )
            st.plotly_chart(fig_hist, use_container_width=True)
        else:
            st.caption("no closed trades yet")

    with col_risk:
        st.markdown("## Risk Metrics")
        pf         = profit_factor(closed)
        max_streak = max_consecutive_losses(closed)
        loss_n     = len(closed) - wins
        avg_win    = (
            sum(t["pnl"] for t in closed if t.get("pnl") and t["pnl"] > 0) / wins
            if wins > 0 else 0.0
        )
        avg_loss = (
            sum(t["pnl"] for t in closed if t.get("pnl") and t["pnl"] < 0) / loss_n
            if loss_n > 0 else 0.0
        )
        r1, r2 = st.columns(2)
        r1.metric("Profit Factor",   f"{pf:.2f}" if pf != float("inf") else "∞")
        r2.metric("Max Loss Streak", str(max_streak))
        r1.metric("Avg Win",         f"${fmt(avg_win, '+.2f')}")
        r2.metric("Avg Loss",        f"${fmt(avg_loss, '+.2f')}")

        st.markdown("## Regime Performance")
        if regime_perf:
            df_reg     = pd.DataFrame(regime_perf)
                fig_reg = go.Figure(go.Bar(
                x=df_reg["win_rate"],
                y=df_reg["regime"],
                orientation="h",
                marker_color=[REGIME_COLORS.get(r, "#555") for r in df_reg["regime"]],
                marker_line_width=0,
                text=[f"{wr:.0f}%  ({t}T)" for wr, t in zip(df_reg["win_rate"], df_reg["total_trades"])],
                textfont=dict(family="Space Mono", size=10, color="#0A0A0A"),
                textposition="inside",
            ))
            fig_reg.add_vline(x=Thresholds.WIN_RATE_MID, line_dash="dot", line_color="#333", line_width=1)
            fig_reg.update_layout(
                **PLOTLY_LAYOUT,
                xaxis=dict(range=[0, 100], gridcolor="#111", showline=False, zeroline=False),
                height=ChartConfig.HEIGHT_REGIME,
            )
            st.plotly_chart(fig_reg, use_container_width=True)
        else:
            st.caption("no regime data yet")

    st.divider()

    st.markdown("## Trade History")
    if closed:
        rows = []
        for t in closed[:50]:
            pnl     = t.get("pnl") or 0.0
            pnl_pct = (t.get("pnl_pct") or 0.0) * 100
            rows.append({
                "DATE":     (t["entry_time"] or "")[:19].replace("T", " "),
                "SIDE":     t["side"],
                "STRATEGY": t["strategy"],
                "ENTRY":    fmt(t["entry_price"], ",.2f"),
                "EXIT":     fmt(t["exit_price"], ",.2f") if t.get("exit_price") else "—",
                "PNL $":    fmt(pnl, "+.4f"),
                "PNL %":    f"{fmt(pnl_pct, '+.2f')}%",
                "REASON":   t.get("exit_reason") or "—",
            })
        df_t = pd.DataFrame(rows)

        def _style_row(val: str):
            try:
                return f"color: {WHITE if parse_fmt(val) >= 0 else RED}; font-weight: 700"
            except ValueError:
                return ""

        def _style_side(val: str):
            return f"color: {WHITE}; font-weight: 700" if val == "BUY" else f"color: {RED}; font-weight: 700"

        styled = (
            df_t.style
            .applymap(_style_row,  subset=["PNL $", "PNL %"])
            .applymap(_style_side, subset=["SIDE"])
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.caption("no completed trades yet")

    st.divider()
    st.markdown("## Adaptive Parameters Log")
    adaptive = db.get_adaptive_params(10)
    if adaptive:
        df_a = pd.DataFrame([
            {
                "TIME":     a["timestamp"][:19].replace("T", " "),
                "STRATEGY": a["strategy"],
                "PARAM":    a["param_name"],
                "OLD":      f"{a['old_value']:.4f}",
                "NEW":      f"{a['new_value']:.4f}",
                "REASON":   a["reason"],
            }
            for a in adaptive
        ])
        st.dataframe(df_a, use_container_width=True, hide_index=True)
    else:
        st.caption("no parameter adaptations yet")
