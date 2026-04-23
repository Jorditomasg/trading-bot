"""Dashboard section — scenario comparison (COMPARE subtab in BACKTEST tab)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import plotly.graph_objects as go
import streamlit as st

from bot.backtest.cache import fetch_and_cache
from bot.backtest.scenario_runner import SCENARIOS, ScenarioResult, ScenarioRunner
from dashboard.constants import GREEN, RED, WHITE
from dashboard.themes import NothingOS

_logger = logging.getLogger(__name__)
PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT

_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

# One distinct color per scenario
_SCENARIO_COLORS = [
    "#00BFFF", "#FF6347", "#32CD32", "#FFD700",
    "#FF69B4", "#9370DB", "#20B2AA", "#FF8C00",
]


def _equity_chart(results: list[ScenarioResult], selected: list[str]) -> go.Figure:
    fig = go.Figure()
    for i, r in enumerate(results):
        if r.scenario.name not in selected:
            continue
        curve = r.equity_curve
        if not curve:
            continue
        fig.add_trace(go.Scatter(
            x=[c["time"]    for c in curve],
            y=[c["balance"] for c in curve],
            name=r.scenario.name,
            mode="lines",
            line=dict(color=_SCENARIO_COLORS[i % len(_SCENARIO_COLORS)], width=1.5),
        ))
    fig.update_layout(
        **PLOTLY_LAYOUT,
        height=420,
        xaxis_title="",
        yaxis_title="Capital (USDT)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    )
    return fig


def scenario_compare_section() -> None:
    """Render the COMPARE subtab content."""
    st.markdown("## Scenario Comparison")
    st.caption(
        "Compare 8 profitability scenarios: 1h vs 4h timeframe, "
        "weekly momentum filter, and leverage from 1× to 10×."
    )

    with st.form("scenario_compare_form"):
        col1, col2, col3 = st.columns(3)
        with col1:
            symbol = st.selectbox("Symbol", _SYMBOLS, index=0)
        with col2:
            days = st.selectbox(
                "Lookback",
                [365, 730, 1095],
                index=2,
                format_func=lambda d: f"{d} days ({d // 365}y)",
            )
        with col3:
            risk_pct = st.number_input(
                "Risk / trade %",
                min_value=0.5, max_value=5.0, value=2.0, step=0.5,
            )
        submitted = st.form_submit_button("Run Comparison", use_container_width=True)

    if not submitted:
        st.info("Configure parameters above and click **Run Comparison** to start.")
        return

    risk = risk_pct / 100.0

    with st.spinner("Fetching market data..."):
        try:
            end_dt   = datetime.now(tz=timezone.utc)
            start_dt = end_dt - timedelta(days=days + 30)

            df_1h     = fetch_and_cache(symbol, "1h",  start_dt, end_dt)
            df_4h     = fetch_and_cache(symbol, "4h",  start_dt, end_dt)
            df_1d     = fetch_and_cache(symbol, "1d",  start_dt, end_dt)
            df_weekly = fetch_and_cache(symbol, "1w",  start_dt, end_dt)
        except Exception as exc:
            st.error(f"Data fetch failed: {exc}")
            return

    runner = ScenarioRunner(
        df_1h=df_1h, df_4h=df_4h, df_1d=df_1d, df_weekly=df_weekly,
        lookback_days=days, risk_per_trade=risk,
    )

    progress = st.progress(0, text="Running scenarios...")
    results: list[ScenarioResult] = []
    for idx, scenario in enumerate(SCENARIOS):
        progress.progress(
            (idx + 1) / len(SCENARIOS),
            text=f"Running: {scenario.name}...",
        )
        results.extend(runner.run_all([scenario], symbol=symbol))
    progress.empty()

    # ── Results table ─────────────────────────────────────────────────────────
    st.markdown("### Results")

    rows = []
    for r in results:
        rows.append({
            "Scenario":     r.scenario.name,
            "Annual %":     f"{r.annual_return_pct * 100:+.1f}%",
            "Sharpe":       f"{r.sharpe_ratio:.2f}" if r.sharpe_ratio != float("inf") else "∞",
            "Max DD %":     f"-{abs(r.max_drawdown_pct):.1f}%",
            "PF":           f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "∞",
            "Trades":       r.total_trades,
            "Liquidations": "-" if r.scenario.leverage <= 1.0 else r.liquidations,
        })

    import pandas as pd
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ── Equity curves ─────────────────────────────────────────────────────────
    st.markdown("### Equity Curves")
    all_names   = [r.scenario.name for r in results]
    default_sel = [s for s in all_names if "10×" not in s]
    selected    = st.multiselect("Show scenarios", all_names, default=default_sel)

    if selected:
        st.plotly_chart(_equity_chart(results, selected), use_container_width=True)

    # ── Key callouts ──────────────────────────────────────────────────────────
    if results:
        best = max(results, key=lambda r: r.annual_return_pct)
        no_liq = [r for r in results if r.liquidations == 0]
        safest = min(no_liq, key=lambda r: r.max_drawdown_pct, default=None)

        col_a, col_b = st.columns(2)
        with col_a:
            st.metric(
                "Highest Annual Return",
                f"{best.annual_return_pct * 100:+.1f}%",
                delta=best.scenario.name,
            )
        with col_b:
            if safest:
                st.metric(
                    "Lowest Drawdown (no liquidations)",
                    f"-{abs(safest.max_drawdown_pct):.1f}%",
                    delta=safest.scenario.name,
                )
