"""Streamlit dashboard — Nothing OS design language."""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from bot.database.db import Database
from bot.metrics import sharpe_ratio, max_drawdown, profit_factor, max_consecutive_losses
from dashboard.themes import NothingOS

import os

REFRESH_INTERVAL = 60  # seconds
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

# ─── Plotly base layout (shared) ──────────────────────────────────────────────
PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT


# ─── DB ───────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_db() -> Database:
    return Database(DB_PATH)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _regime_badge(regime: str) -> str:
    r = regime.upper()
    return f'<span class="regime regime-{r}">{r}</span>'


def _pnl_span(val: float, text: str) -> str:
    cls = "pos" if val >= 0 else "neg"
    return f'<span class="{cls}">{text}</span>'


# ─── Render ───────────────────────────────────────────────────────────────────
def render() -> None:
    db = get_db()
    st_autorefresh(interval=REFRESH_INTERVAL * 1000, key="dashboard_refresh")

    trades          = db.get_all_trades()
    equity_curve    = db.get_equity_curve()
    open_trade      = db.get_open_trade()
    strategy_perf   = db.get_performance_by_strategy()
    regime_perf     = db.get_performance_by_regime()
    recent_signals  = db.get_recent_signals(20)

    closed = [t for t in trades if t.get("exit_price") is not None]

    initial_balance = equity_curve[0]["balance"]  if equity_curve else 10_000.0
    current_balance = equity_curve[-1]["balance"] if equity_curve else 10_000.0
    current_dd      = equity_curve[-1]["drawdown"] if equity_curve else 0.0

    total_pnl     = current_balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0
    wins          = sum(1 for t in closed if t.get("pnl") and t["pnl"] > 0)
    win_rate      = (wins / len(closed) * 100) if closed else 0.0
    sharpe        = sharpe_ratio(equity_curve)
    max_dd        = max_drawdown(equity_curve)

    last_regime   = recent_signals[0]["regime"]   if recent_signals else "RANGING"
    last_strategy = recent_signals[0]["strategy"] if recent_signals else "—"
    last_ts       = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # ── TOPBAR ────────────────────────────────────────────────────────────────
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

    # ── KPI ROW ───────────────────────────────────────────────────────────────
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Balance",       f"${current_balance:,.2f}")
    k2.metric("Total PnL",     f"${total_pnl:+,.2f}", delta=f"{total_pnl_pct:+.2f}%")
    k3.metric("Win Rate",      f"{win_rate:.1f}%")
    k4.metric("Sharpe (ann.)", f"{sharpe:.2f}")
    k5.metric("Max Drawdown",  f"{max_dd*100:.2f}%")
    k6.metric("Trades",        str(len(closed)))

    st.divider()

    # ── ROW 2 — Equity + State ────────────────────────────────────────────────
    col_eq, col_state = st.columns([3, 2])

    with col_eq:
        st.markdown("## Equity Curve")
        if len(equity_curve) >= 2:
            ts  = [r["timestamp"] for r in equity_curve]
            bal = [r["balance"]   for r in equity_curve]
            above_start = [b >= initial_balance for b in bal]

            fig = go.Figure()
            # Red fill below initial
            fig.add_trace(go.Scatter(
                x=ts, y=bal,
                mode="lines",
                line=dict(color="#FF0000", width=1.5),
                fill="tozeroy",
                fillcolor="rgba(255,0,0,0.04)",
                showlegend=False,
            ))
            # White line above initial
            fig.add_trace(go.Scatter(
                x=ts, y=[b if b >= initial_balance else None for b in bal],
                mode="lines",
                line=dict(color="#F5F5F5", width=1.5),
                fill="tozeroy",
                fillcolor="rgba(245,245,245,0.04)",
                showlegend=False,
            ))
            # Initial capital reference line
            fig.add_hline(
                y=initial_balance,
                line_dash="dot",
                line_color="#333",
                line_width=1,
            )
            fig.update_layout(**PLOTLY_LAYOUT, height=240)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.caption("waiting for data...")

    with col_state:
        st.markdown("## State")
        st.markdown(
            f"Regime &nbsp; {_regime_badge(last_regime)} &nbsp;&nbsp; "
            f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>STRATEGY</span> "
            f"<code>{last_strategy}</code>",
            unsafe_allow_html=True,
        )
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
            fig_dd.add_hline(
                y=15.0,
                line_dash="dot",
                line_color="#333",
                line_width=1,
            )
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
            entry = open_trade["entry_price"]
            sl    = open_trade["stop_loss"]
            tp    = open_trade["take_profit"]
            side  = open_trade["side"]
            qty   = open_trade["quantity"]

            side_cls = "pos" if side == "BUY" else "neg"
            st.markdown(
                f"<span class='pill {'pill-running' if side == 'BUY' else 'pill-stopped'}'>"
                f"{side}</span> &nbsp; "
                f"<span style='font-size:0.8rem'>{qty:.5f} BTC</span>",
                unsafe_allow_html=True,
            )
            c1, c2, c3 = st.columns(3)
            c1.metric("Entry",  f"${entry:,.0f}")
            c2.metric("SL",     f"${sl:,.0f}")
            c3.metric("TP",     f"${tp:,.0f}")
            st.caption(f"{open_trade['strategy']} · {open_trade['regime']}")
        else:
            st.markdown(
                "<span style='font-size:0.75rem;color:#333;letter-spacing:0.1em'>"
                "NO OPEN POSITION</span>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── ROW 3 — Strategy perf + Risk metrics ──────────────────────────────────
    col_strat, col_hist, col_risk = st.columns(3)

    with col_strat:
        st.markdown("## Strategy Performance")
        if strategy_perf:
            df_p = pd.DataFrame(strategy_perf)
            bar_colors = [
                "#F5F5F5" if wr >= 50 else "#FF0000"
                for wr in df_p["win_rate"]
            ]
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
            fig.add_vline(x=50, line_dash="dot", line_color="#333", line_width=1)
            fig.update_layout(
                **PLOTLY_LAYOUT,
                xaxis=dict(range=[0, 100], gridcolor="#111", showline=False, zeroline=False),
                height=200,
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
                    x=neg_vals,
                    name="Loss",
                    marker_color="#FF0000",
                    opacity=0.85,
                    nbinsx=15,
                ))
            if pos_vals:
                fig_hist.add_trace(go.Histogram(
                    x=pos_vals,
                    name="Win",
                    marker_color="#F5F5F5",
                    opacity=0.85,
                    nbinsx=15,
                ))
            fig_hist.add_vline(x=0, line_color="#555", line_width=1)
            fig_hist.update_layout(
                **PLOTLY_LAYOUT,
                barmode="overlay",
                showlegend=False,
                height=220,
            )
            st.plotly_chart(fig_hist, use_container_width=True)
        else:
            st.caption("no closed trades yet")

    with col_risk:
        st.markdown("## Risk Metrics")
        pf         = profit_factor(closed)
        max_streak = max_consecutive_losses(closed)
        avg_win    = (
            sum(t["pnl"] for t in closed if t.get("pnl") and t["pnl"] > 0) / wins
            if wins > 0 else 0.0
        )
        loss_n = len(closed) - wins
        avg_loss = (
            sum(t["pnl"] for t in closed if t.get("pnl") and t["pnl"] < 0) / loss_n
            if loss_n > 0 else 0.0
        )
        r1, r2 = st.columns(2)
        r1.metric("Profit Factor",   f"{pf:.2f}" if pf != float("inf") else "∞")
        r2.metric("Max Loss Streak", str(max_streak))
        r1.metric("Avg Win",         f"${avg_win:+.2f}")
        r2.metric("Avg Loss",        f"${avg_loss:+.2f}")

        st.markdown("## Regime Performance")
        if regime_perf:
            df_reg = pd.DataFrame(regime_perf)
            reg_colors = {
                "TRENDING": "#F5F5F5",
                "RANGING": "#888888",
                "VOLATILE": "#FF0000",
            }
            fig_reg = go.Figure(go.Bar(
                x=df_reg["win_rate"],
                y=df_reg["regime"],
                orientation="h",
                marker_color=[reg_colors.get(r, "#555") for r in df_reg["regime"]],
                marker_line_width=0,
                text=[f"{wr:.0f}%  ({t}T)" for wr, t in zip(df_reg["win_rate"], df_reg["total_trades"])],
                textfont=dict(family="Space Mono", size=10, color="#0A0A0A"),
                textposition="inside",
            ))
            fig_reg.add_vline(x=50, line_dash="dot", line_color="#333", line_width=1)
            fig_reg.update_layout(
                **PLOTLY_LAYOUT,
                xaxis=dict(range=[0, 100], gridcolor="#111", showline=False, zeroline=False),
                height=180,
            )
            st.plotly_chart(fig_reg, use_container_width=True)
        else:
            st.caption("no regime data yet")

    st.divider()

    # ── ROW 4 — Trade history ─────────────────────────────────────────────────
    st.markdown("## Trade History")
    if closed:
        rows = []
        for t in closed[:50]:
            pnl     = t.get("pnl") or 0.0
            pnl_pct = (t.get("pnl_pct") or 0.0) * 100
            rows.append({
                "DATE":     (t["entry_time"] or "")[:16],
                "SIDE":     t["side"],
                "STRATEGY": t["strategy"],
                "ENTRY":    f"{t['entry_price']:,.2f}",
                "EXIT":     f"{t['exit_price']:,.2f}" if t.get("exit_price") else "—",
                "PNL $":    f"{pnl:+.4f}",
                "PNL %":    f"{pnl_pct:+.2f}%",
                "REASON":   t.get("exit_reason") or "—",
            })
        df_t = pd.DataFrame(rows)

        def _style_row(val: str):
            try:
                n = float(val.replace("+", "").replace("%", ""))
                return f"color: {'#F5F5F5' if n >= 0 else '#FF0000'}; font-weight: 700"
            except ValueError:
                return ""

        def _style_side(val: str):
            return "color: #F5F5F5; font-weight: 700" if val == "BUY" else "color: #FF0000; font-weight: 700"

        styled = (
            df_t.style
            .applymap(_style_row,  subset=["PNL $", "PNL %"])
            .applymap(_style_side, subset=["SIDE"])
        )
        st.dataframe(styled, use_container_width=True, hide_index=True)
    else:
        st.caption("no completed trades yet")

    st.divider()

    # ── ROW 5 — Signal log ────────────────────────────────────────────────────
    st.markdown("## Signal Log")
    if recent_signals:
        df_s = pd.DataFrame([
            {
                "TIME":     s["timestamp"][:16],
                "STRATEGY": s["strategy"],
                "REGIME":   s["regime"],
                "ACTION":   s["action"],
                "STR":      f"{s['strength']:.2f}",
            }
            for s in recent_signals
        ])

        def _style_action(val: str):
            if val == "BUY":  return "color: #F5F5F5; font-weight: 700"
            if val == "SELL": return "color: #FF0000; font-weight: 700"
            return "color: #333"

        def _style_regime(val: str):
            if val == "VOLATILE": return "color: #FF0000"
            if val == "TRENDING": return "color: #F5F5F5"
            return "color: #555"

        styled_s = (
            df_s.style
            .applymap(_style_action, subset=["ACTION"])
            .applymap(_style_regime, subset=["REGIME"])
        )
        st.dataframe(styled_s, use_container_width=True, hide_index=True)
    else:
        st.caption("no signals yet")

    # ── Footer ────────────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='text-align:right;font-size:0.55rem;color:#1A1A1A;"
        f"letter-spacing:0.15em;margin-top:2rem'>* TRADING BOT · BINANCE TESTNET · "
        f"REFRESH {REFRESH_INTERVAL}S</div>",
        unsafe_allow_html=True,
    )



render()
