"""Data export section — ZIP of CSVs with optional date-range filter."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime

import pandas as pd
import streamlit as st

from bot.database.db import Database


def _iso(d: date, end_of_day: bool = False) -> str:
    if end_of_day:
        return datetime(d.year, d.month, d.day, 23, 59, 59).isoformat()
    return datetime(d.year, d.month, d.day, 0, 0, 0).isoformat()


def _build_zip(
    datasets: dict[str, list[dict]],
    metadata: dict | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in datasets.items():
            df  = pd.DataFrame(rows) if rows else pd.DataFrame()
            csv = df.to_csv(index=False).encode("utf-8")
            zf.writestr(f"{name}.csv", csv)
        if metadata:
            zf.writestr("metadata.json", json.dumps(metadata, indent=2))
    return buf.getvalue()


@st.fragment
def export_section(db: Database) -> None:
    st.markdown("## Export")

    # ── Date range ────────────────────────────────────────────────────────────
    all_time = st.checkbox("All time", value=True, key="exp_all_time")

    from_dt = to_dt = None
    date_label = "all-time"

    if not all_time:
        col_f, col_t = st.columns(2)
        with col_f:
            from_date: date = st.date_input("From", key="exp_from")
        with col_t:
            to_date: date = st.date_input("To", value=date.today(), key="exp_to")

        if from_date > to_date:
            st.warning("'From' must be before 'To'.")
            return

        from_dt    = _iso(from_date)
        to_dt      = _iso(to_date, end_of_day=True)
        date_label = f"{from_date}_to_{to_date}"

    # ── Dataset selection ─────────────────────────────────────────────────────
    st.markdown(
        "<span style='font-size:0.65rem;letter-spacing:0.1em;color:#555'>DATASETS</span>",
        unsafe_allow_html=True,
    )
    c1, c2, c3, c4 = st.columns(4)
    inc_trades  = c1.checkbox("Trades",          value=True, key="exp_inc_trades")
    inc_equity  = c2.checkbox("Equity",          value=True, key="exp_inc_equity")
    inc_signals = c3.checkbox("Signals",         value=True, key="exp_inc_signals")
    inc_params  = c4.checkbox("Adaptive Params", value=True, key="exp_inc_params")

    st.markdown(
        "<span style='font-size:0.65rem;letter-spacing:0.1em;color:#555'>BOT STATE</span>",
        unsafe_allow_html=True,
    )
    b1, b2, b3, _ = st.columns(4)
    inc_perf_strat  = b1.checkbox("Perf / Strategy", value=True, key="exp_inc_perf_s")
    inc_perf_regime = b2.checkbox("Perf / Regime",   value=True, key="exp_inc_perf_r")
    inc_config      = b3.checkbox("Bot Config",      value=True, key="exp_inc_config")

    if not any([inc_trades, inc_equity, inc_signals, inc_params,
                inc_perf_strat, inc_perf_regime, inc_config]):
        st.caption("Select at least one dataset.")
        return

    # ── Query ─────────────────────────────────────────────────────────────────
    datasets: dict[str, list[dict]] = {}
    if inc_trades:
        datasets["trades"]               = db.get_trades_range(from_dt, to_dt)
    if inc_equity:
        datasets["equity"]               = db.get_equity_range(from_dt, to_dt)
    if inc_signals:
        datasets["signals"]              = db.get_signals_range(from_dt, to_dt)
    if inc_params:
        datasets["adaptive_params"]      = db.get_adaptive_params_range(from_dt, to_dt)
    # Performance is always full-history (aggregate, not range-filtered)
    if inc_perf_strat:
        datasets["perf_by_strategy"]     = db.get_performance_by_strategy()
    if inc_perf_regime:
        datasets["perf_by_regime"]       = db.get_performance_by_regime()

    # Bot config snapshot (no CSV — included as metadata.json)
    metadata: dict | None = None
    if inc_config:
        tg  = db.get_telegram_config()
        metadata = {
            "exported_at":      datetime.utcnow().isoformat() + "Z",
            "date_filter":      date_label,
            "active_mode":      db.get_active_mode(),
            "bot_paused":       db.get_bot_paused(),
            "telegram_enabled": tg["enabled"],
            "telegram_chat_id": tg["chat_id"],
        }

    # ── Row counts ────────────────────────────────────────────────────────────
    total_rows = sum(len(v) for v in datasets.values())
    counts_parts = [f"<code>{name}</code> {len(rows):,}" for name, rows in datasets.items()]
    if metadata:
        counts_parts.append("<code>metadata</code> —")
    st.markdown(
        f"<span style='font-size:0.65rem;color:#555'>"
        f"{'  ·  '.join(counts_parts)}</span>",
        unsafe_allow_html=True,
    )

    # ── Download ──────────────────────────────────────────────────────────────
    if total_rows == 0 and not metadata:
        st.caption("No data in selected range.")
        return

    zip_bytes = _build_zip(datasets, metadata)
    filename  = f"trading-bot-{date_label}.zip"

    st.download_button(
        label=f"⬇  Export  ({total_rows:,} rows)",
        data=zip_bytes,
        file_name=filename,
        mime="application/zip",
        use_container_width=True,
    )
