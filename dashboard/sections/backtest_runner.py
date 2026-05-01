"""Backtest runner section — run multi-symbol portfolio backtests from the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import streamlit as st

from bot.backtest.cache import cache_info, fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import (
    PortfolioBacktestEngine,
    PortfolioBacktestResult,
)
from bot.database.db import Database
from dashboard.constants import GREEN, RED, ChartConfig
from dashboard.themes import NothingOS
from dashboard.utils import fmt

_logger = logging.getLogger(__name__)

PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT

_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
_TIMEFRAMES = ["1h", "2h", "4h", "8h", "1d"]
_BIAS_TF    = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}


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
            f"{symbol} {interval} — NO CACHE, will download on run</span>"
        )
    return (
        f"<span style='font-size:0.6rem;color:#555;letter-spacing:0.1em'>"
        f"{symbol} {interval} CACHE: {info['rows']:,} bars · "
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

        # Default symbol selection — active list filtered to those we support,
        # falling back to the first known symbol when none are configured yet.
        active_symbols = [s for s in db.get_symbols() if s in _SYMBOLS]
        if not active_symbols:
            active_symbols = [_SYMBOLS[0]]

        with st.form("backtest_form"):
            c1, c2 = st.columns(2)

            with c1:
                symbols = st.multiselect(
                    "Symbols", _SYMBOLS, default=active_symbols,
                    help="Pick one or more symbols. Capital is shared across all of them.",
                )

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
                use_momentum  = st.checkbox(
                    "Momentum filter (weekly 20-SMA, ±8% band)",
                    value=True,
                    help=(
                        "Block new entries when price < weekly SMA × 0.92, "
                        "scale down (×0.5) when within neutral band. "
                        "OOS-validated: cuts DD ~42% with no return loss."
                    ),
                )
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

        # Cache status — one badge per selected symbol (outside the form so it
        # updates without resubmitting).
        for sym in symbols:
            st.markdown(_cache_badge(sym, timeframe), unsafe_allow_html=True)
            if use_1m:
                st.markdown(_cache_badge(sym, "1m"), unsafe_allow_html=True)

    # ── Trigger run ───────────────────────────────────────────────────────────
    if submitted:
        if len(symbols) < 1:
            st.error("Select at least one symbol.")
        else:
            start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_dt   = datetime.combine(end_date,   datetime.min.time()).replace(tzinfo=timezone.utc)

            if start_dt >= end_dt:
                st.error("Start date must be before end date.")
            else:
                _run_portfolio_backtest(
                    symbols, timeframe, start_dt, end_dt,
                    capital, risk_pct / 100, cost_pct / 100,
                    use_bias, use_momentum, use_1m,
                )

    # ── Right: results (persist across re-runs via session_state) ─────────────
    with col_results:
        if "bt_portfolio_result" in st.session_state:
            _display_portfolio_results(st.session_state["bt_portfolio_result"])
        else:
            st.markdown("## Results")
            st.markdown(
                "<div style='color:#333;font-size:0.75rem;letter-spacing:0.1em;margin-top:2rem'>"
                "Configure parameters and run a backtest to see results here.</div>",
                unsafe_allow_html=True,
            )


# ── Runner ────────────────────────────────────────────────────────────────────

def _run_portfolio_backtest(
    symbols:      list[str],
    timeframe:    str,
    start_dt:     datetime,
    end_dt:       datetime,
    capital:      float,
    risk:         float,
    cost:         float,
    use_bias:     bool,
    use_momentum: bool,
    use_1m:       bool,
) -> None:
    bias_tf  = _BIAS_TF.get(timeframe, "1d")
    progress = st.empty()

    def on_progress(msg: str) -> None:
        progress.caption(msg)

    dfs:        dict = {}
    dfs_4h:     dict = {}
    dfs_weekly: dict = {}
    dfs_1m:     dict = {}

    # ── Per-symbol fetch ──────────────────────────────────────────────────────
    for sym in symbols:
        # Primary bars — hard requirement; skip the symbol if this fails.
        with st.spinner(f"Fetching {sym} {timeframe} data…"):
            try:
                dfs[sym] = fetch_and_cache(sym, timeframe, start_dt, end_dt, on_progress=on_progress)
            except Exception as exc:
                st.error(f"Failed to fetch {sym} {timeframe} data: {exc} — skipping {sym}.")
                continue

        # Bias bars — fail-soft per symbol.
        if use_bias:
            with st.spinner(f"Fetching {sym} {bias_tf} klines for BiasFilter…"):
                try:
                    dfs_4h[sym] = fetch_and_cache(sym, bias_tf, start_dt, end_dt, on_progress=on_progress)
                except Exception as exc:
                    dfs_4h[sym] = None
                    st.warning(f"Could not fetch {sym} {bias_tf} data ({exc}) — running {sym} without BiasFilter.")

        # Weekly bars for momentum filter — fail-soft per symbol.
        if use_momentum:
            with st.spinner(f"Fetching {sym} weekly klines for momentum filter…"):
                try:
                    dfs_weekly[sym] = fetch_and_cache(sym, "1w", start_dt, end_dt, on_progress=on_progress)
                except Exception as exc:
                    dfs_weekly[sym] = None
                    st.warning(f"Could not fetch {sym} weekly data ({exc}) — momentum filter pass-through for {sym}.")

        # 1m precision bars — fail-soft per symbol.
        if use_1m:
            with st.spinner(f"Fetching {sym} 1m klines for precision exits (this may take a while)…"):
                try:
                    dfs_1m[sym] = fetch_and_cache(sym, "1m", start_dt, end_dt, on_progress=on_progress)
                    on_progress(f"{sym} 1m cache ready: {len(dfs_1m[sym]):,} bars")
                except Exception as exc:
                    dfs_1m[sym] = None
                    st.warning(f"Could not fetch {sym} 1m data ({exc}) — bar-level precision for {sym}.")

    progress.empty()

    if not dfs:
        st.error("No primary data could be fetched for any selected symbol — aborting.")
        return

    # ── Run engine ────────────────────────────────────────────────────────────
    cfg = BacktestConfig(
        initial_capital         = capital,
        risk_per_trade          = risk,
        timeframe               = timeframe,
        cost_per_side_pct       = cost,
        momentum_filter_enabled = use_momentum,
        momentum_sma_period     = 20,
        momentum_neutral_band   = 0.08,
    )
    engine = PortfolioBacktestEngine(cfg)

    with st.spinner("Simulating portfolio…"):
        try:
            result = engine.run_portfolio(
                dfs,
                dfs_4h     = dfs_4h     or None,
                dfs_weekly = dfs_weekly or None,
                dfs_1m     = dfs_1m     or None,
            )
        except Exception as exc:
            st.error(f"Portfolio backtest error: {exc}")
            return

    # Drop legacy single-symbol session keys so the old display path stays dark.
    st.session_state.pop("bt_result",  None)
    st.session_state.pop("bt_summary", None)
    st.session_state["bt_portfolio_result"] = result
    st.rerun()


# ── Results display ───────────────────────────────────────────────────────────

def _display_portfolio_results(result: PortfolioBacktestResult) -> None:
    summary = result.portfolio_summary

    def _get(key: str, default: str = "—") -> object:
        return summary[key] if key in summary else default

    # ── Header ────────────────────────────────────────────────────────────────
    st.markdown("## Results")
    n_sym = len(result.symbols)
    st.caption(
        f"{n_sym} symbol(s)  ·  {result.timeframe}  ·  "
        f"{result.start_date} → {result.end_date}"
    )

    # ── Portfolio KPI metrics ────────────────────────────────────────────────
    pnl         = _get("total_pnl")
    pnl_pct     = _get("total_pnl_pct")
    pf          = _get("profit_factor")
    wr          = _get("win_rate_pct")
    sharpe      = _get("sharpe_ratio")
    dd          = _get("max_drawdown_pct")
    streak      = _get("max_loss_streak")
    n_trades    = _get("total_trades")

    pnl_str     = f"{'+' if isinstance(pnl, (int, float)) and pnl >= 0 else ''}${fmt(abs(pnl))}" if isinstance(pnl, (int, float)) else "—"
    pnl_delta   = f"{'+' if isinstance(pnl_pct, (int, float)) and pnl_pct >= 0 else ''}{pnl_pct:.1f}%" if isinstance(pnl_pct, (int, float)) else None
    pf_str      = (f"{pf:.2f}" if pf != float("inf") else "∞") if isinstance(pf, (int, float)) else "—"
    wr_str      = f"{wr:.1f}%" if isinstance(wr, (int, float)) else "—"
    sharpe_str  = f"{sharpe:.2f}" if isinstance(sharpe, (int, float)) else "—"
    dd_str      = f"{dd:.1f}%" if isinstance(dd, (int, float)) else "—"
    streak_str  = str(streak) if isinstance(streak, (int, float)) else "—"
    trades_str  = str(n_trades) if isinstance(n_trades, (int, float)) else "—"

    m1, m2, m3 = st.columns(3)
    m1.metric("Net PnL",       pnl_str,  delta=pnl_delta)
    m2.metric("Profit Factor", pf_str)
    m3.metric("Win Rate",      wr_str,   delta=f"{trades_str} trades")

    m4, m5, m6 = st.columns(3)
    m4.metric("Sharpe (ann.)",   sharpe_str)
    m5.metric("Max Drawdown",    dd_str)
    m6.metric("Max Loss Streak", streak_str)

    m7, m8 = st.columns(2)
    m7.metric("Total Trades",  trades_str)
    m8.metric("Total PnL %",   f"{pnl_pct:+.1f}%" if isinstance(pnl_pct, (int, float)) else "—")

    # ── Combined equity curve ─────────────────────────────────────────────────
    if result.combined_equity_curve:
        st.markdown("## Equity")
        st.plotly_chart(
            _equity_chart(result.combined_equity_curve, result.initial_capital),
            use_container_width=True,
        )

    # ── Per-symbol expander ───────────────────────────────────────────────────
    with st.expander("Per-Symbol Detail", expanded=False):
        for symbol in result.symbols:
            st.markdown(f"### {symbol}")

            sym_summary = result.per_symbol_summary.get(symbol, {})
            sym_pnl     = sym_summary.get("total_pnl",       0.0)
            sym_pf      = sym_summary.get("profit_factor",   0.0)
            sym_wr      = sym_summary.get("win_rate_pct",    0.0)
            sym_n       = sym_summary.get("total_trades",    0)
            sym_sharpe  = sym_summary.get("sharpe_ratio",    0.0)
            sym_dd      = sym_summary.get("max_drawdown_pct", 0.0)

            sym_pnl_str = (
                f"{'+' if sym_pnl >= 0 else '-'}${fmt(abs(sym_pnl))}"
                if isinstance(sym_pnl, (int, float)) else "—"
            )
            sym_pf_str  = (f"{sym_pf:.2f}" if sym_pf != float("inf") else "∞") if isinstance(sym_pf, (int, float)) else "—"
            sym_wr_str  = f"{sym_wr:.1f}%" if isinstance(sym_wr, (int, float)) else "—"

            c1, c2, c3 = st.columns(3)
            c1.metric("Net PnL",       sym_pnl_str)
            c2.metric("Profit Factor", sym_pf_str)
            c3.metric("Win Rate",      sym_wr_str)

            st.caption(
                f"Trades: {sym_n}  ·  Sharpe: {sym_sharpe:.2f}  ·  MaxDD: {sym_dd:.1f}%"
            )

            sym_trades = result.per_symbol_trades.get(symbol, [])
            if sym_trades:
                rows = [
                    {
                        "entry_time":  t.get("entry_time"),
                        "side":        t.get("side"),
                        "entry_price": t.get("entry_price"),
                        "exit_price":  t.get("exit_price"),
                        "pnl":         t.get("pnl"),
                        "exit_reason": t.get("exit_reason"),
                    }
                    for t in sym_trades[-20:]
                ]
                st.dataframe(rows, use_container_width=True, hide_index=True)
