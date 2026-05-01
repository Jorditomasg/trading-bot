"""Walk-forward optimizer section — run parameter grid search from the dashboard."""

from __future__ import annotations

import logging

import plotly.graph_objects as go
import streamlit as st

from bot.database.db import Database
from bot.optimizer.walk_forward import STOP_GRID, TP_GRID, run_grid_search
from bot.optimizer.entry_quality_optimizer import (
    VOL_GRID, BAR_DIR_GRID, MOMENTUM_GRID, ATR_PCT_GRID,
    run_entry_quality_grid_search,
)
_EQ_TOTAL = len(VOL_GRID) * len(BAR_DIR_GRID) * len(MOMENTUM_GRID) * len(ATR_PCT_GRID)
from dashboard.constants import GREEN, RED, WHITE, MUTED, SURFACE, ChartConfig
from dashboard.themes import NothingOS
from dashboard.utils import fmt

_logger = logging.getLogger(__name__)
PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
PLOTLY_CONFIG = NothingOS.PLOTLY_CONFIG

_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
_TIMEFRAMES = ["1h", "2h", "4h", "8h", "1d"]
_PERIODS    = [90, 180, 270, 365]


# ── Heatmap ───────────────────────────────────────────────────────────────────

def _heatmap(results: list[dict]) -> go.Figure:
    """Profit Factor heatmap over stop_mult × tp_mult grid."""
    stops = sorted(set(r["stop_mult"] for r in results))
    tps   = sorted(set(r["tp_mult"]   for r in results))
    pf_map = {(r["stop_mult"], r["tp_mult"]): r["profit_factor"] for r in results}

    z = []
    for stop in stops:
        row = []
        for tp in tps:
            pf = pf_map.get((stop, tp), 0.0)
            row.append(pf if pf != float("inf") else 5.0)
        z.append(row)

    fig = go.Figure(go.Heatmap(
        z=z,
        x=[f"TP {t}" for t in tps],
        y=[f"SL {s}" for s in stops],
        colorscale=[[0, "#1A1A1A"], [0.4, "#FF0000"], [0.7, "#888"], [1.0, "#F5F5F5"]],
        zmin=0.8, zmax=2.0,
        text=[[f"{v:.2f}" for v in row] for row in z],
        texttemplate="%{text}",
        textfont=dict(size=9, family="Space Mono"),
        showscale=True,
    ))
    fig.update_layout(
        **PLOTLY_LAYOUT,
        height=260,
        coloraxis_showscale=True,
    )
    return fig


# ── Pending proposal banner ───────────────────────────────────────────────────

def _pending_banner(db: Database) -> None:
    pending = db.get_best_pending_optimizer_run()
    if pending is None:
        return

    cfg      = db.get_runtime_config()
    cur_stop = float(cfg.get("ema_stop_mult", 1.5))
    cur_tp   = float(cfg.get("ema_tp_mult",   3.5))

    pf_color = GREEN if pending["profit_factor"] >= 1.2 else WHITE

    st.markdown(
        f"<div style='border:1px solid {GREEN};padding:12px 16px;margin-bottom:1rem'>"
        f"<span style='font-size:0.65rem;letter-spacing:0.18em;color:{GREEN}'>● PENDING PROPOSAL</span><br>"
        f"<span style='font-size:0.75rem;color:#F5F5F5'>"
        f"SL {pending['ema_stop_mult']:.2f} ATR &nbsp;·&nbsp; "
        f"TP {pending['ema_tp_mult']:.2f} ATR &nbsp;·&nbsp; "
        f"PF <strong>{pending['profit_factor']:.2f}</strong> &nbsp;·&nbsp; "
        f"WR {pending['win_rate']:.1f}% &nbsp;·&nbsp; "
        f"DD {pending['max_drawdown']:.1f}% &nbsp;·&nbsp; "
        f"{pending['total_trades']} trades"
        f"</span><br>"
        f"<span style='font-size:0.6rem;color:#555'>"
        f"Current: SL {cur_stop:.2f} · TP {cur_tp:.2f} &nbsp;—&nbsp; "
        f"{pending['symbol']} {pending['timeframe']} · {pending['period_days']}d lookback"
        f"</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    col_a, col_r, col_o = st.columns(3)
    with col_a:
        if st.button("✓ Approve & Apply", type="primary", use_container_width=True):
            db.set_runtime_config(
                ema_stop_mult=str(pending["ema_stop_mult"]),
                ema_tp_mult=str(pending["ema_tp_mult"]),
            )
            db.set_optimizer_run_status(pending["id"], "approved")
            st.success(
                f"Applied: SL {pending['ema_stop_mult']:.2f} · TP {pending['ema_tp_mult']:.2f}. "
                "Restart the bot to activate."
            )
            st.rerun()
    with col_r:
        if st.button("✕ Reject", use_container_width=True):
            db.set_optimizer_run_status(pending["id"], "rejected")
            st.info("Proposal rejected — current config unchanged.")
            st.rerun()
    with col_o:
        st.markdown(
            "<span style='font-size:0.6rem;color:#555;display:block;margin-top:8px'>"
            "Override: edit manually in CONFIG tab</span>",
            unsafe_allow_html=True,
        )


# ── History table ─────────────────────────────────────────────────────────────

def _history_table(db: Database) -> None:
    runs = db.get_optimizer_runs(limit=30)
    if not runs:
        st.caption("no optimization runs yet")
        return

    import pandas as pd
    rows = []
    for r in runs:
        status_icon = {"pending": "⏳", "approved": "✓", "rejected": "✕"}.get(r["status"], "?")
        rows.append({
            "DATE":    r["timestamp"][:16].replace("T", " "),
            "SYMBOL":  r["symbol"],
            "TF":      r["timeframe"],
            "SL":      f"{r['ema_stop_mult']:.2f}",
            "TP":      f"{r['ema_tp_mult']:.2f}",
            "PF":      f"{r['profit_factor']:.2f}",
            "WR":      f"{r['win_rate']:.1f}%",
            "DD":      f"{r['max_drawdown']:.1f}%",
            "TRADES":  str(r["total_trades"]),
            "STATUS":  f"{status_icon} {r['status'].upper()}",
        })

    df = pd.DataFrame(rows)

    def _style_status(val: str):
        if "approved" in val.lower(): return f"color: {GREEN}; font-weight: 700"
        if "rejected" in val.lower(): return f"color: #333"
        return f"color: {WHITE}"

    def _style_pf(val: str):
        try:
            v = float(val)
            return f"color: {GREEN}; font-weight: 700" if v >= 1.2 else (
                f"color: {WHITE}" if v >= 1.1 else f"color: {RED}"
            )
        except ValueError:
            return ""

    styled = (
        df.style
        .applymap(_style_status, subset=["STATUS"])
        .applymap(_style_pf,     subset=["PF"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ── Main section ──────────────────────────────────────────────────────────────

def optimizer_section(db: Database) -> None:
    # Show pending proposal if any
    _pending_banner(db)

    col_form, col_results = st.columns([1, 1], gap="large")

    with col_form:
        st.markdown("## Grid Search")
        st.caption(
            f"Tests {len(STOP_GRID) * len(TP_GRID)} combinations of SL/TP ATR multipliers "
            "on recent data. Viable configs saved to DB for approval."
        )

        with st.form("optimizer_form"):
            cfg = db.get_runtime_config()

            c1, c2 = st.columns(2)
            with c1:
                sym_default = cfg.get("symbol", "BTCUSDT")
                symbol = st.selectbox(
                    "Symbol", _SYMBOLS,
                    index=_SYMBOLS.index(sym_default) if sym_default in _SYMBOLS else 0,
                )
                tf_default = cfg.get("timeframe", "4h")
                timeframe = st.selectbox(
                    "Timeframe", _TIMEFRAMES,
                    index=_TIMEFRAMES.index(tf_default) if tf_default in _TIMEFRAMES else 2,
                )
            with c2:
                period_days = st.selectbox("Lookback", _PERIODS, index=1,
                                           format_func=lambda d: f"{d} days")
                risk_pct = st.number_input(
                    "Risk / Trade (%)", min_value=0.1, max_value=5.0,
                    value=round(float(cfg.get("risk_per_trade", 0.01)) * 100, 1),
                    step=0.1, format="%.1f",
                )
                cost_pct = st.number_input(
                    "Fee / side (%)", min_value=0.0, max_value=1.0,
                    value=0.07, step=0.01, format="%.2f",
                )

            run = st.form_submit_button(
                f"▶  Run Optimizer ({len(STOP_GRID) * len(TP_GRID)} combos)",
                use_container_width=True, type="primary",
            )

    if run:
        _run_optimizer(db, symbol, timeframe, period_days, risk_pct / 100, cost_pct / 100)

    with col_results:
        if "opt_results" in st.session_state:
            _display_results(st.session_state["opt_results"])
        else:
            st.markdown("## Results")
            st.markdown(
                "<div style='color:#333;font-size:0.75rem;letter-spacing:0.1em;margin-top:2rem'>"
                "Run the optimizer to see the parameter heatmap and ranking here.</div>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("## Optimization History")
    _history_table(db)

    # ── Entry Quality Optimizer ───────────────────────────────────────────────
    st.divider()
    st.markdown("## Entry Quality Optimizer")
    st.caption(
        f"Tests {_EQ_TOTAL} combinations of EMA entry filters "
        "(volume threshold · bar direction · EMA momentum · min ATR). "
        "TP/SL fixed at current approved values."
    )

    _entry_quality_pending_banner(db)

    col_eq_form, col_eq_results = st.columns([1, 1], gap="large")

    with col_eq_form:
        with st.form("eq_optimizer_form"):
            cfg = db.get_runtime_config()

            c1, c2 = st.columns(2)
            with c1:
                sym_default = cfg.get("symbol", "BTCUSDT")
                eq_symbol = st.selectbox(
                    "Symbol", _SYMBOLS,
                    index=_SYMBOLS.index(sym_default) if sym_default in _SYMBOLS else 0,
                    key="eq_symbol",
                )
                tf_default = cfg.get("timeframe", "1h")
                eq_tf = st.selectbox(
                    "Timeframe", _TIMEFRAMES,
                    index=_TIMEFRAMES.index(tf_default) if tf_default in _TIMEFRAMES else 0,
                    key="eq_tf",
                )
            with c2:
                eq_period = st.selectbox(
                    "Lookback", _PERIODS, index=2,
                    format_func=lambda d: f"{d} days",
                    key="eq_period",
                )
                eq_risk_pct = st.number_input(
                    "Risk / Trade (%)", min_value=0.1, max_value=5.0,
                    value=round(float(cfg.get("risk_per_trade", 0.01)) * 100, 1),
                    step=0.1, format="%.1f", key="eq_risk",
                )
                eq_cost_pct = st.number_input(
                    "Fee / side (%)", min_value=0.0, max_value=1.0,
                    value=0.07, step=0.01, format="%.2f", key="eq_cost",
                )

            eq_run = st.form_submit_button(
                f"▶  Run Entry Quality Optimizer ({_EQ_TOTAL} combos)",
                use_container_width=True, type="primary",
            )

    if eq_run:
        _run_entry_quality_optimizer(
            db, eq_symbol, eq_tf, eq_period,
            eq_risk_pct / 100, eq_cost_pct / 100,
        )

    with col_eq_results:
        if "eq_results" in st.session_state:
            _display_entry_quality_results(st.session_state["eq_results"])
        else:
            st.markdown("## Results")
            st.markdown(
                "<div style='color:#333;font-size:0.75rem;letter-spacing:0.1em;margin-top:2rem'>"
                "Run the entry quality optimizer to see results here.</div>",
                unsafe_allow_html=True,
            )

    st.divider()
    st.markdown("## Entry Quality Optimization History")
    _entry_quality_history_table(db)


# ── Runner ────────────────────────────────────────────────────────────────────

def _run_optimizer(db, symbol, timeframe, period_days, risk, cost):
    progress_placeholder = st.empty()
    completed = [0]
    viable    = [0]

    def on_progress(idx, total, stop, tp, summary):
        completed[0] = idx
        if summary is not None:
            viable[0] += 1
        pct = idx / total
        progress_placeholder.progress(
            pct,
            text=f"Testing SL={stop:.2f} TP={tp:.2f} … {idx}/{total} · {viable[0]} viable",
        )

    with st.spinner("Fetching historical data…"):
        try:
            results = run_grid_search(
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                lookback_days=period_days,
                cost_per_side=cost,
                risk_per_trade=risk,
                on_progress=on_progress,
            )
        except Exception as exc:
            st.error(f"Optimizer error: {exc}")
            progress_placeholder.empty()
            return

    progress_placeholder.empty()
    st.session_state["opt_results"] = results
    st.rerun()


# ── Display ───────────────────────────────────────────────────────────────────

def _display_results(results: list[dict]) -> None:
    if not results:
        st.warning("No viable configurations found. Try a longer lookback or different symbol.")
        return

    viable = [r for r in results if r["viable"]]
    best   = results[0]

    st.markdown("## Best Config")
    pf_color = GREEN if best["profit_factor"] >= 1.2 else WHITE
    st.markdown(
        f"<span style='font-size:1rem;color:{pf_color};font-weight:700'>"
        f"SL {best['stop_mult']:.2f} ATR &nbsp;·&nbsp; TP {best['tp_mult']:.2f} ATR</span> "
        f"<span style='font-size:0.65rem;color:#555'> → PF {best['profit_factor']:.2f} "
        f"· WR {best['win_rate']:.1f}% · {best['total_trades']} trades</span>",
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Viable configs", str(len(viable)))
    m2.metric("Best PF",        f"{best['profit_factor']:.2f}")
    m3.metric("Best WR",        f"{best['win_rate']:.1f}%")
    m4.metric("Best DD",        f"{best['max_drawdown']:.1f}%")

    st.markdown("## Parameter Heatmap")
    st.caption("Profit Factor by SL × TP ATR multipliers. White = best. Grey cells = viable.")
    st.plotly_chart(_heatmap(results), use_container_width=True, config=PLOTLY_CONFIG)

    st.markdown("## Top 10 Configs")
    import pandas as pd
    top = results[:10]
    df  = pd.DataFrame([{
        "SL":     f"{r['stop_mult']:.2f}",
        "TP":     f"{r['tp_mult']:.2f}",
        "R:R":    f"{r['tp_mult']/r['stop_mult']:.2f}",
        "PF":     f"{r['profit_factor']:.2f}",
        "WR":     f"{r['win_rate']:.1f}%",
        "Sharpe": f"{r['sharpe_ratio']:.2f}",
        "DD":     f"{r['max_drawdown']:.1f}%",
        "Trades": str(r["total_trades"]),
        "Viable": "✓" if r["viable"] else "✕",
    } for r in top])

    def _style_viable(val):
        return f"color: {GREEN}; font-weight:700" if val == "✓" else f"color: {RED}"

    styled = df.style.applymap(_style_viable, subset=["Viable"])
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ── Entry Quality pending banner ──────────────────────────────────────────────

def _entry_quality_pending_banner(db: Database) -> None:
    pending = db.get_best_pending_entry_quality_run()
    if pending is None:
        return

    cfg       = db.get_runtime_config()
    cur_vol   = float(cfg.get("ema_vol_mult", 0.0))
    cur_bar   = cfg.get("ema_bar_dir",  "false") == "true"
    cur_mom   = cfg.get("ema_momentum", "false") == "true"
    cur_atr   = float(cfg.get("ema_min_atr", 0.0))

    pf_color  = GREEN if pending["profit_factor"] >= 1.2 else WHITE
    bar_icon  = "✓" if pending["bar_direction"] else "—"
    mom_icon  = "✓" if pending["ema_momentum"]  else "—"

    st.markdown(
        f"<div style='border:1px solid {GREEN};padding:12px 16px;margin-bottom:1rem'>"
        f"<span style='font-size:0.65rem;letter-spacing:0.18em;color:{GREEN}'>● ENTRY QUALITY PENDING PROPOSAL</span><br>"
        f"<span style='font-size:0.75rem;color:#F5F5F5'>"
        f"Vol {pending['vol_mult']:.1f}× &nbsp;·&nbsp; "
        f"BarDir {bar_icon} &nbsp;·&nbsp; "
        f"Momentum {mom_icon} &nbsp;·&nbsp; "
        f"MinATR {pending['min_atr_pct']:.3f} &nbsp;·&nbsp; "
        f"PF <strong style='color:{pf_color}'>{pending['profit_factor']:.2f}</strong> &nbsp;·&nbsp; "
        f"WR {pending['win_rate']:.1f}% &nbsp;·&nbsp; "
        f"DD {pending['max_drawdown']:.1f}% &nbsp;·&nbsp; "
        f"{pending['total_trades']} trades"
        f"</span><br>"
        f"<span style='font-size:0.6rem;color:#555'>"
        f"Current: vol {cur_vol:.1f} · bar {cur_bar} · mom {cur_mom} · minATR {cur_atr:.3f} &nbsp;—&nbsp; "
        f"{pending['symbol']} {pending['timeframe']} · {pending['period_days']}d lookback"
        f"</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    col_a, col_r, _ = st.columns(3)
    with col_a:
        if st.button("✓ Apply Entry Quality Config", type="primary",
                     use_container_width=True, key="eq_approve"):
            db.set_runtime_config(
                ema_vol_mult=str(pending["vol_mult"]),
                ema_bar_dir="true" if pending["bar_direction"] else "false",
                ema_momentum="true" if pending["ema_momentum"] else "false",
                ema_min_atr=str(pending["min_atr_pct"]),
            )
            db.set_entry_quality_run_status(pending["id"], "approved")
            st.success("Applied. Hot-reloads on next bot cycle (no restart needed).")
            st.rerun()
    with col_r:
        if st.button("✕ Reject", use_container_width=True, key="eq_reject"):
            db.set_entry_quality_run_status(pending["id"], "rejected")
            st.info("Entry quality proposal rejected — current config unchanged.")
            st.rerun()


# ── Entry Quality results table ───────────────────────────────────────────────

def _display_entry_quality_results(results: list[dict]) -> None:
    if not results:
        st.warning("No viable entry quality configurations found. Try a longer lookback.")
        return

    import pandas as pd
    top = results[:10]
    rows = []
    for r in top:
        pf = r["profit_factor"]
        rows.append({
            "VOL":    f"{r['vol_mult']:.1f}×",
            "BAR":    "✓" if r["bar_direction"] else "—",
            "MOM":    "✓" if r["ema_momentum"]  else "—",
            "MINATR": f"{r['min_atr_pct']:.3f}",
            "PF":     f"{pf:.2f}",
            "WR":     f"{r['win_rate']:.1f}%",
            "DD":     f"{r['max_drawdown']:.1f}%",
            "TRADES": str(r["total_trades"]),
            "VIABLE": "✓" if r["viable"] else "✕",
        })

    df = pd.DataFrame(rows)

    def _style_pf(val: str):
        try:
            v = float(val)
            if v >= 1.2: return f"color: {GREEN}; font-weight: 700"
            if v >= 1.1: return f"color: {WHITE}"
            return f"color: {RED}"
        except ValueError:
            return ""

    def _style_viable(val: str):
        return f"color: {GREEN}; font-weight: 700" if val == "✓" else f"color: {RED}"

    styled = (
        df.style
        .map(_style_pf,     subset=["PF"])
        .map(_style_viable, subset=["VIABLE"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)

    viable = [r for r in results if r["viable"]]
    if viable:
        best = viable[0]
        st.success(
            f"Best: vol {best['vol_mult']:.1f}× · bar {'✓' if best['bar_direction'] else '—'} · "
            f"mom {'✓' if best['ema_momentum'] else '—'} · minATR {best['min_atr_pct']:.3f} "
            f"→ PF {best['profit_factor']:.2f}"
        )


# ── Entry Quality history table ───────────────────────────────────────────────

def _entry_quality_history_table(db: Database) -> None:
    runs = db.get_entry_quality_runs(limit=30)
    if not runs:
        st.caption("no entry quality optimization runs yet")
        return

    import pandas as pd
    rows = []
    for r in runs:
        status_icon = {"pending": "⏳", "approved": "✓", "rejected": "✕"}.get(r["status"], "?")
        rows.append({
            "DATE":    r["timestamp"][:16].replace("T", " "),
            "SYMBOL":  r["symbol"],
            "TF":      r["timeframe"],
            "VOL":     f"{r['vol_mult']:.1f}×",
            "BAR":     "✓" if r["bar_direction"] else "—",
            "MOM":     "✓" if r["ema_momentum"]  else "—",
            "MINATR":  f"{r['min_atr_pct']:.3f}",
            "PF":      f"{r['profit_factor']:.2f}",
            "WR":      f"{r['win_rate']:.1f}%",
            "DD":      f"{r['max_drawdown']:.1f}%",
            "TRADES":  str(r["total_trades"]),
            "STATUS":  f"{status_icon} {r['status'].upper()}",
        })

    df = pd.DataFrame(rows)

    def _style_status(val: str):
        if "APPROVED" in val: return f"color: {GREEN}; font-weight: 700"
        if "REJECTED" in val: return f"color: #333"
        return f"color: {WHITE}"

    def _style_pf(val: str):
        try:
            v = float(val)
            if v >= 1.2: return f"color: {GREEN}; font-weight: 700"
            if v >= 1.1: return f"color: {WHITE}"
            return f"color: {RED}"
        except ValueError:
            return ""

    styled = (
        df.style
        .map(_style_status, subset=["STATUS"])
        .map(_style_pf,     subset=["PF"])
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ── Entry Quality runner ──────────────────────────────────────────────────────

def _run_entry_quality_optimizer(db, symbol, timeframe, period_days, risk, cost):
    progress_placeholder = st.empty()
    completed = [0]
    viable    = [0]

    def on_progress(idx, total, vol, bar, mom, min_atr, summary):
        completed[0] = idx
        if summary is not None:
            viable[0] += 1
        pct = idx / total
        bar_icon = "✓" if bar else "—"
        mom_icon = "✓" if mom else "—"
        progress_placeholder.progress(
            pct,
            text=(
                f"vol={vol:.1f} bar={bar_icon} mom={mom_icon} atr={min_atr:.3f} "
                f"… {idx}/{total} · {viable[0]} viable"
            ),
        )

    with st.spinner("Fetching historical data…"):
        try:
            results = run_entry_quality_grid_search(
                db=db,
                symbol=symbol,
                timeframe=timeframe,
                lookback_days=period_days,
                cost_per_side=cost,
                risk_per_trade=risk,
                on_progress=on_progress,
            )
        except Exception as exc:
            st.error(f"Entry quality optimizer error: {exc}")
            progress_placeholder.empty()
            return

    progress_placeholder.empty()
    st.session_state["eq_results"] = results
    st.rerun()


