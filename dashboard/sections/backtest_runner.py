"""Backtest runner section — run backtests from the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import streamlit as st

from bot.backtest.cache import cache_info, fetch_and_cache
from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.database.db import Database
from dashboard.constants import GREEN, RED, WHITE, MUTED, ChartConfig
from dashboard.themes import NothingOS
from dashboard.utils import fmt

_logger = logging.getLogger(__name__)

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT

_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
_TIMEFRAMES = ["1h", "2h", "4h", "8h", "1d"]
_BIAS_TF    = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}

# ── Verdict helpers ───────────────────────────────────────────────────────────

def _verdict(summary: dict) -> tuple[bool, list[str]]:
    notes: list[str] = []
    passed = True
    wr, sharpe = summary["win_rate_pct"], summary["sharpe_ratio"]
    dd, pf, n   = summary["max_drawdown_pct"], summary["profit_factor"], summary["total_trades"]

    if n < 20:
        notes.append(f"Too few trades ({n}) — not statistically significant"); passed = False
    if wr < 30.0:
        notes.append(f"Win rate {wr:.1f}% below 30% minimum"); passed = False
    elif wr < 40.0:
        notes.append(f"Win rate {wr:.1f}% below 40% (acceptable if PF compensates)")
    if sharpe < 0.5:
        notes.append(f"Sharpe {sharpe:.2f} below 0.5 — poor risk-adjusted return"); passed = False
    if dd > 25.0:
        notes.append(f"Max drawdown {dd:.1f}% exceeds 25% limit"); passed = False
    elif dd > 20.0:
        notes.append(f"Max drawdown {dd:.1f}% exceeds 20% (watch closely)")
    if pf != float("inf") and pf < 1.1:
        notes.append(f"Profit factor {pf:.2f} below 1.1 — marginal edge"); passed = False
    elif pf != float("inf") and pf < 1.2:
        notes.append(f"Profit factor {pf:.2f} below 1.2 (solid but improvable)")
    if passed:
        notes.append("All minimum viability thresholds met")
    return passed, notes


# ── Equity mini-chart ─────────────────────────────────────────────────────────

def _equity_chart(equity_curve: list[dict], initial_capital: float) -> go.Figure:
    bal = [r["balance"] for r in equity_curve]
    color = GREEN if bal[-1] >= initial_capital else RED
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        y=bal, mode="lines",
        line=dict(color=color, width=ChartConfig.LINE_WIDTH),
        fill="tozeroy", fillcolor="rgba(245,245,245,0.04)",
        showlegend=False,
    ))
    fig.add_hline(y=initial_capital, line_dash="dot", line_color="#333", line_width=1)
    fig.update_layout(**PLOTLY_LAYOUT, height=180)
    fig.update_yaxes(gridcolor="#111", showline=False, zeroline=False)
    return fig


# ── Cache status badge ────────────────────────────────────────────────────────

def _cache_badge(symbol: str, interval: str) -> str:
    info = cache_info(symbol, interval)
    if info is None:
        return (
            f"<span style='font-size:0.6rem;color:#555;letter-spacing:0.1em'>"
            f"NO CACHE — will download on run</span>"
        )
    return (
        f"<span style='font-size:0.6rem;color:#555;letter-spacing:0.1em'>"
        f"CACHE: {info['rows']:,} bars · "
        f"{info['from'].strftime('%Y-%m-%d')} → {info['to'].strftime('%Y-%m-%d')} · "
        f"{info['size_mb']} MB</span>"
    )


# ── Main section ──────────────────────────────────────────────────────────────

def backtest_runner_section(db: Database) -> None:
    cfg = db.get_runtime_config()

    col_form, col_results = st.columns([1, 1], gap="large")

    # ── Left: form ────────────────────────────────────────────────────────────
    with col_form:
        st.markdown("## Parameters")

        with st.form("backtest_form"):
            c1, c2 = st.columns(2)

            with c1:
                sym_default = cfg.get("symbol", "BTCUSDT")
                sym_idx     = _SYMBOLS.index(sym_default) if sym_default in _SYMBOLS else 0
                symbol      = st.selectbox("Symbol", _SYMBOLS, index=sym_idx)

                tf_default = cfg.get("timeframe", "4h")
                tf_idx     = _TIMEFRAMES.index(tf_default) if tf_default in _TIMEFRAMES else 2
                timeframe  = st.selectbox("Timeframe", _TIMEFRAMES, index=tf_idx)

                capital = st.number_input(
                    "Capital ($)", min_value=100.0, value=10_000.0, step=1_000.0,
                )
                risk_pct = st.number_input(
                    "Risk / Trade (%)", min_value=0.1, max_value=5.0,
                    value=round(float(cfg.get("risk_per_trade", 0.01)) * 100, 1),
                    step=0.1, format="%.1f",
                )

            with c2:
                end_default   = datetime.now(tz=timezone.utc).date()
                start_default = end_default - timedelta(days=180)
                start_date = st.date_input("From", value=start_default)
                end_date   = st.date_input("To",   value=end_default)

                cost_pct = st.number_input(
                    "Fee / side (%)", min_value=0.0, max_value=1.0,
                    value=0.07, step=0.01, format="%.2f",
                    help="0.02% maker · 0.07% recommended · 0.10% taker",
                )
                use_bias      = st.checkbox("BiasFilter (daily EMA9/21)", value=True)
                use_1m        = st.checkbox(
                    "1m precision exits",
                    value=False,
                    help=(
                        "Download 1-minute bars and use them for exact SL/TP/trailing "
                        "stop timing. Much more realistic — requires more data."
                    ),
                )

            submitted = st.form_submit_button(
                "▶  Run Backtest", use_container_width=True, type="primary",
            )

        # Cache status (outside form so it updates without submit)
        st.markdown(_cache_badge(symbol, timeframe), unsafe_allow_html=True)
        if use_1m:
            st.markdown(_cache_badge(symbol, "1m"), unsafe_allow_html=True)

    # ── Trigger run ───────────────────────────────────────────────────────────
    if submitted:
        start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt   = datetime.combine(end_date,   datetime.min.time()).replace(tzinfo=timezone.utc)

        if start_dt >= end_dt:
            st.error("Start date must be before end date.")
        else:
            _run_backtest(
                symbol, timeframe, start_dt, end_dt,
                capital, risk_pct / 100, cost_pct / 100, use_bias, use_1m,
            )

    # ── Right: results (persist across re-runs via session_state) ─────────────
    with col_results:
        if "bt_result" in st.session_state:
            _display_results(st.session_state["bt_result"], st.session_state["bt_summary"])
        else:
            st.markdown("## Results")
            st.markdown(
                "<div style='color:#333;font-size:0.75rem;letter-spacing:0.1em;margin-top:2rem'>"
                "Configure parameters and run a backtest to see results here.</div>",
                unsafe_allow_html=True,
            )


# ── Runner ────────────────────────────────────────────────────────────────────

def _run_backtest(
    symbol: str,
    timeframe: str,
    start_dt: datetime,
    end_dt: datetime,
    capital: float,
    risk: float,
    cost: float,
    use_bias: bool,
    use_1m: bool,
) -> None:
    bias_tf   = _BIAS_TF.get(timeframe, "1d")
    progress  = st.empty()

    def on_progress(msg: str) -> None:
        progress.caption(msg)

    # ── Fetch primary bars ────────────────────────────────────────────────────
    with st.spinner(f"Fetching {symbol} {timeframe} data…"):
        try:
            df = fetch_and_cache(symbol, timeframe, start_dt, end_dt, on_progress=on_progress)
        except Exception as exc:
            st.error(f"Failed to fetch {timeframe} data: {exc}")
            progress.empty()
            return

    # ── Fetch bias bars ───────────────────────────────────────────────────────
    df_bias = None
    if use_bias:
        with st.spinner(f"Fetching {bias_tf} klines for BiasFilter…"):
            try:
                df_bias = fetch_and_cache(symbol, bias_tf, start_dt, end_dt, on_progress=on_progress)
            except Exception as exc:
                st.warning(f"Could not fetch {bias_tf} data ({exc}) — running without BiasFilter.")

    # ── Fetch 1m bars for precision exits ─────────────────────────────────────
    df_1m = None
    if use_1m:
        with st.spinner("Fetching 1m klines for precision exits (this may take a while)…"):
            try:
                df_1m = fetch_and_cache(symbol, "1m", start_dt, end_dt, on_progress=on_progress)
                on_progress(f"1m cache ready: {len(df_1m):,} bars")
            except Exception as exc:
                st.warning(f"Could not fetch 1m data ({exc}) — falling back to bar-level precision.")

    progress.empty()

    # ── Run engine ────────────────────────────────────────────────────────────
    cfg = BacktestConfig(
        initial_capital=capital,
        risk_per_trade=risk,
        timeframe=timeframe,
        cost_per_side_pct=cost,
    )
    engine = BacktestEngine(cfg)

    with st.spinner("Simulating…"):
        try:
            result  = engine.run(df, df_4h=df_bias, symbol=symbol, df_1m=df_1m)
            summary = engine.summary(result)
        except Exception as exc:
            st.error(f"Backtest error: {exc}")
            return

    st.session_state["bt_result"]  = result
    st.session_state["bt_summary"] = summary
    st.rerun()


# ── Results display ───────────────────────────────────────────────────────────

def _display_results(result, summary: dict) -> None:
    pnl     = summary["total_pnl"]
    pnl_pct = summary["total_pnl_pct"]
    wr      = summary["win_rate_pct"]
    pf      = summary["profit_factor"]
    sharpe  = summary["sharpe_ratio"]
    dd      = summary["max_drawdown_pct"]
    streak  = summary["max_loss_streak"]
    n       = summary["total_trades"]
    best    = summary["best_trade_pnl"]
    worst   = summary["worst_trade_pnl"]

    passed, notes = _verdict(summary)

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("## Results")
    st.caption(
        f"{result.symbol}  ·  {result.timeframe}  ·  "
        f"{result.start_date} → {result.end_date}  ·  {result.total_bars:,} bars"
    )

    # ── Verdict banner ────────────────────────────────────────────────────────
    verdict_color = GREEN if passed else RED
    verdict_label = "PASS" if passed else "NEEDS REVIEW"
    st.markdown(
        f"<div style='border:1px solid {verdict_color};padding:8px 16px;"
        f"font-size:0.75rem;letter-spacing:0.18em;color:{verdict_color};"
        f"margin-bottom:0.5rem'>"
        f"● {verdict_label}"
        f"</div>",
        unsafe_allow_html=True,
    )
    for note in notes:
        marker = "✓" if passed else "·"
        color  = MUTED if passed else "#888"
        st.markdown(
            f"<span style='font-size:0.65rem;color:{color};letter-spacing:0.08em'>"
            f"{marker} {note}</span>",
            unsafe_allow_html=True,
        )

    st.markdown("")

    # ── KPI metrics ──────────────────────────────────────────────────────────
    pnl_sign = "+" if pnl >= 0 else ""
    m1, m2, m3 = st.columns(3)
    m1.metric("Net PnL",       f"{pnl_sign}${fmt(abs(pnl))}",  delta=f"{pnl_sign}{pnl_pct:.1f}%")
    m2.metric("Profit Factor", f"{pf:.2f}" if pf != float("inf") else "∞")
    m3.metric("Win Rate",      f"{wr:.1f}%",  delta=f"{n} trades")

    m4, m5, m6 = st.columns(3)
    m4.metric("Sharpe (ann.)", f"{sharpe:.2f}")
    m5.metric("Max Drawdown",  f"{dd:.1f}%")
    m6.metric("Max Loss Streak", str(streak))

    m7, m8 = st.columns(2)
    m7.metric("Best Trade",  f"+${fmt(best)}")
    m8.metric("Worst Trade", f"-${fmt(abs(worst))}")

    # ── Equity curve ──────────────────────────────────────────────────────────
    if result.equity_curve:
        st.markdown("## Equity")
        st.plotly_chart(_equity_chart(result.equity_curve, result.initial_capital),
                        use_container_width=True)
