"""Streamlit dashboard — Nothing OS design language."""

import math
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database

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
# Palette: #0A0A0A bg · #111 surface · #1A1A1A border · #FF0000 accent · #F5F5F5 text
NOTHING_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap');

/* ── Root ──────────────────────────────────────────────── */
html, body, [class*="css"] {
    font-family: 'Space Mono', 'Courier New', monospace !important;
    background-color: #0A0A0A;
    color: #F5F5F5;
}

/* ── Hide Streamlit chrome ──────────────────────────────── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1.5rem 2rem 1rem; }

/* ── Divider ────────────────────────────────────────────── */
hr { border-color: #1A1A1A !important; }

/* ── Metric cards ───────────────────────────────────────── */
[data-testid="metric-container"] {
    background: #111111;
    border: 1px solid #1A1A1A;
    padding: 1rem 1.2rem;
    border-radius: 0 !important;
}
[data-testid="metric-container"] label {
    font-size: 0.65rem !important;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #555 !important;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 1.55rem !important;
    font-weight: 700;
    color: #F5F5F5;
}
[data-testid="stMetricDelta"] svg { display: none; }
[data-testid="stMetricDelta"] > div {
    font-size: 0.75rem !important;
    letter-spacing: 0.05em;
}

/* ── Dataframes ─────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border: 1px solid #1A1A1A;
    border-radius: 0 !important;
}
thead tr th {
    background: #111 !important;
    color: #555 !important;
    font-size: 0.65rem !important;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    border-bottom: 1px solid #1A1A1A !important;
}
tbody tr:nth-child(even) { background: #0D0D0D !important; }

/* ── Section headers ────────────────────────────────────── */
h1, h2, h3 {
    font-family: 'Space Mono', monospace !important;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: #F5F5F5 !important;
}
h2 { font-size: 0.85rem !important; letter-spacing: 0.18em; text-transform: uppercase; color: #555 !important; }

/* ── Status pills ───────────────────────────────────────── */
.pill {
    display: inline-block;
    padding: 2px 10px;
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
}
.pill-running  { border: 1px solid #FF0000; color: #FF0000; }
.pill-stopped  { border: 1px solid #333;    color: #555; }
.pill-testnet  { border: 1px solid #333;    color: #888; }
.pill-live     { border: 1px solid #FF0000; color: #FF0000; }

/* ── Regime badges ──────────────────────────────────────── */
.regime {
    display: inline-block;
    padding: 2px 10px;
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
    border-radius: 0 !important;
}
.regime-TRENDING  { border: 1px solid #F5F5F5; color: #F5F5F5; }
.regime-RANGING   { border: 1px solid #555;    color: #888; }
.regime-VOLATILE  { border: 1px solid #FF0000; color: #FF0000; }

/* ── PnL colours ────────────────────────────────────────── */
.pos { color: #F5F5F5; font-weight: 700; }
.neg { color: #FF0000; font-weight: 700; }
.neu { color: #555; }

/* ── Topbar ─────────────────────────────────────────────── */
.topbar {
    display: flex;
    align-items: baseline;
    gap: 1.5rem;
    margin-bottom: 0.25rem;
}
.bot-name {
    font-size: 1.2rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: #F5F5F5;
}
.glyph {
    color: #FF0000;
    font-size: 1.1rem;
    margin-right: 0.3rem;
}

/* ── Info box ────────────────────────────────────────────── */
[data-testid="stInfo"], [data-testid="stSuccess"], [data-testid="stWarning"] {
    background: #111 !important;
    border-left: 2px solid #1A1A1A !important;
    border-radius: 0 !important;
    font-size: 0.75rem;
}

/* ── Captions ───────────────────────────────────────────── */
[data-testid="stCaptionContainer"] {
    color: #333 !important;
    font-size: 0.6rem !important;
    letter-spacing: 0.08em;
}
</style>
"""
st.markdown(NOTHING_CSS, unsafe_allow_html=True)

# ─── Plotly base layout (shared) ──────────────────────────────────────────────
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#0A0A0A",
    font=dict(family="Space Mono, Courier New, monospace", color="#555", size=10),
    margin=dict(l=0, r=0, t=4, b=0),
    xaxis=dict(gridcolor="#111", showline=False, zeroline=False),
    yaxis=dict(gridcolor="#111", showline=False, zeroline=False),
)


# ─── DB ───────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_db() -> Database:
    return Database(DB_PATH)


# ─── Calculations ─────────────────────────────────────────────────────────────
def _sharpe(equity_curve: list[dict], tf_hours: int = 1) -> float:
    if len(equity_curve) < 2:
        return 0.0
    returns = pd.Series([r["balance"] for r in equity_curve]).pct_change().dropna()
    std = returns.std()
    return float((returns.mean() / std) * math.sqrt(8760 / tf_hours)) if std > 0 else 0.0


def _max_drawdown(equity_curve: list[dict]) -> float:
    if not equity_curve:
        return 0.0
    peak = max_dd = 0.0
    for row in equity_curve:
        b = row["balance"]
        if b > peak:
            peak = b
        dd = (peak - b) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _profit_factor(trades: list[dict]) -> float:
    gross_win  = sum(t["pnl"] for t in trades if t.get("pnl") and t["pnl"] > 0)
    gross_loss = sum(abs(t["pnl"]) for t in trades if t.get("pnl") and t["pnl"] < 0)
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def _max_loss_streak(trades: list[dict]) -> int:
    max_s = cur = 0
    for t in trades:
        if t.get("pnl") is not None and t["pnl"] < 0:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


def _regime_badge(regime: str) -> str:
    r = regime.upper()
    return f'<span class="regime regime-{r}">{r}</span>'


def _pnl_span(val: float, text: str) -> str:
    cls = "pos" if val >= 0 else "neg"
    return f'<span class="{cls}">{text}</span>'


# ─── Render ───────────────────────────────────────────────────────────────────
def render() -> None:
    db = get_db()

    trades          = db.get_all_trades()
    equity_curve    = db.get_equity_curve()
    open_trade      = db.get_open_trade()
    strategy_perf   = db.get_performance_by_strategy()
    recent_signals  = db.get_recent_signals(20)

    closed = [t for t in trades if t.get("exit_price") is not None]

    initial_balance = equity_curve[0]["balance"]  if equity_curve else 10_000.0
    current_balance = equity_curve[-1]["balance"] if equity_curve else 10_000.0
    current_dd      = equity_curve[-1]["drawdown"] if equity_curve else 0.0

    total_pnl     = current_balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0
    wins          = sum(1 for t in closed if t.get("pnl") and t["pnl"] > 0)
    win_rate      = (wins / len(closed) * 100) if closed else 0.0
    sharpe        = _sharpe(equity_curve)
    max_dd        = _max_drawdown(equity_curve)

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
        st.markdown(
            f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>DRAWDOWN</span> "
            f"{'<span class=\"neg\">' if current_dd > 0.05 else '<span class=\"neu\">'}"
            f"{current_dd*100:.2f}%</span>",
            unsafe_allow_html=True,
        )
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
    col_strat, col_risk = st.columns(2)

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

    with col_risk:
        st.markdown("## Risk Metrics")
        pf         = _profit_factor(closed)
        max_streak = _max_loss_streak(closed)
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

    time.sleep(REFRESH_INTERVAL)
    st.rerun()


render()
