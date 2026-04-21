"""Bot configuration manager — edit runtime parameters from the dashboard."""

from __future__ import annotations

import streamlit as st

from bot.config import settings
from bot.database.db import Database

_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
_TIMEFRAMES = ["1h", "2h", "4h", "8h", "1d"]


def config_manager_section(db: Database) -> None:
    cfg = db.get_runtime_config()

    # Current values: DB → fallback to .env
    cur_symbol            = cfg.get("symbol",                  settings.symbol)
    cur_tf                = cfg.get("timeframe",               settings.timeframe)
    cur_risk              = float(cfg.get("risk_per_trade",    settings.risk_per_trade))
    cur_max_dd            = float(cfg.get("max_drawdown",      0.15))
    cur_max_conc          = int(cfg.get("max_concurrent",      1))
    cur_cooldown          = int(cfg.get("cooldown_hours",      4))
    cur_trail_atr         = float(cfg.get("trail_atr_mult",    1.5))
    cur_trail_act         = float(cfg.get("trail_act_mult",    1.0))
    cur_bias_passthrough  = cfg.get("bias_neutral_passthrough", "true") == "true"
    cur_bias_threshold    = float(cfg.get("bias_neutral_threshold", "0.001"))

    st.markdown("## Trading")

    with st.form("bot_config_form"):
        col_sym, col_tf = st.columns(2)
        with col_sym:
            sym_idx = _SYMBOLS.index(cur_symbol) if cur_symbol in _SYMBOLS else 0
            symbol = st.selectbox("Symbol", _SYMBOLS, index=sym_idx)
        with col_tf:
            tf_idx = _TIMEFRAMES.index(cur_tf) if cur_tf in _TIMEFRAMES else 2
            timeframe = st.selectbox("Timeframe", _TIMEFRAMES, index=tf_idx)

        st.markdown("## Risk")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            risk_pct = st.number_input(
                "Risk / Trade (%)", min_value=0.1, max_value=5.0,
                value=round(cur_risk * 100, 2), step=0.1, format="%.1f",
            )
        with c2:
            max_dd_pct = st.number_input(
                "Max Drawdown (%)", min_value=5.0, max_value=50.0,
                value=round(cur_max_dd * 100, 1), step=1.0, format="%.1f",
            )
        with c3:
            max_concurrent = st.number_input(
                "Max Positions", min_value=1, max_value=5, value=cur_max_conc, step=1,
            )
        with c4:
            cooldown_hours = st.number_input(
                "Cooldown (h)", min_value=1, max_value=48, value=cur_cooldown, step=1,
            )

        st.markdown("## Trailing Stop")
        t1, t2 = st.columns(2)
        with t1:
            trail_atr = st.number_input(
                "ATR Distance Mult", min_value=0.5, max_value=5.0,
                value=cur_trail_atr, step=0.5, format="%.1f",
                help="Stop placed at price ± trail_atr × ATR once activated",
            )
        with t2:
            trail_act = st.number_input(
                "Activation Mult", min_value=0.5, max_value=5.0,
                value=cur_trail_act, step=0.5, format="%.1f",
                help="Trailing activates when price moves activation_mult × ATR from entry",
            )

        st.markdown("## BiasFilter")
        b1, b2 = st.columns(2)
        with b1:
            bias_passthrough = st.checkbox(
                "Allow trades in NEUTRAL bias",
                value=cur_bias_passthrough,
                help=(
                    "NEUTRAL = daily EMAs have no clear direction. "
                    "Enabled (recommended): both BUY and SELL signals pass through — "
                    "the filter only blocks signals that go AGAINST a confirmed trend. "
                    "Disabled: all signals are blocked when bias is indeterminate."
                ),
            )
        with b2:
            bias_threshold_pct = st.number_input(
                "Neutral threshold (%)",
                min_value=0.05, max_value=1.0,
                value=round(cur_bias_threshold * 100, 2),
                step=0.05, format="%.2f",
                help=(
                    "EMA9/EMA21 gap must exceed this % of price to be classified as "
                    "BULLISH or BEARISH. Below this = NEUTRAL. Default 0.10%."
                ),
            )

        saved = st.form_submit_button("💾  Save Configuration", use_container_width=True)

    if saved:
        db.set_runtime_config(
            symbol=symbol,
            timeframe=timeframe,
            risk_per_trade=str(round(risk_pct / 100, 4)),
            max_drawdown=str(round(max_dd_pct / 100, 3)),
            max_concurrent=str(max_concurrent),
            cooldown_hours=str(cooldown_hours),
            trail_atr_mult=str(trail_atr),
            trail_act_mult=str(trail_act),
            bias_neutral_passthrough="true" if bias_passthrough else "false",
            bias_neutral_threshold=str(round(bias_threshold_pct / 100, 4)),
        )
        st.success("Configuration saved — restart the bot to apply all changes.")

    st.divider()

    # ── Bot controls ──────────────────────────────────────────────────────────
    st.markdown("## Controls")
    col_restart, col_pause, col_mode = st.columns(3)

    with col_restart:
        st.caption("RESTART")
        if st.button("🔄  Restart Bot", use_container_width=True, type="primary"):
            db.set_config("restart_requested", "1")
            st.warning("Restart signal sent — bot will exit within 10 s and Docker will restart it.")

    with col_pause:
        st.caption("PAUSE / RESUME")
        paused = db.get_bot_paused()
        label  = "▶  Resume Bot" if paused else "⏸  Pause Bot"
        if st.button(label, use_container_width=True):
            db.set_bot_paused(not paused)
            st.info("Resumed." if paused else "Paused — position manager still runs (SL/TP active).")

    with col_mode:
        st.caption("ENVIRONMENT")
        mode        = db.get_active_mode()
        other_mode  = "MAINNET" if mode == "TESTNET" else "TESTNET"
        mode_label  = f"Switch to {other_mode}"
        if st.button(mode_label, use_container_width=True):
            db.set_active_mode(other_mode)
            st.info(f"Mode set to {other_mode}. Restart required.")

    st.divider()

    # ── Current config snapshot ───────────────────────────────────────────────
    st.markdown("## Active Configuration")
    st.caption("Values the bot will read on next restart. Greyed fields from .env, bold from DB.")

    snap_cfg = db.get_runtime_config()
    bias_pass = snap_cfg.get("bias_neutral_passthrough", "true") == "true"
    bias_thr  = float(snap_cfg.get("bias_neutral_threshold", "0.001")) * 100
    rows = {
        "Symbol":            snap_cfg.get("symbol",         settings.symbol),
        "Timeframe":         snap_cfg.get("timeframe",       settings.timeframe),
        "Risk / Trade":      f"{float(snap_cfg.get('risk_per_trade', settings.risk_per_trade)) * 100:.2f}%",
        "Max Drawdown":      f"{float(snap_cfg.get('max_drawdown', 0.15)) * 100:.1f}%",
        "Max Positions":     snap_cfg.get("max_concurrent",  "1"),
        "Cooldown":          f"{snap_cfg.get('cooldown_hours', '4')}h",
        "Trail ATR Mult":    snap_cfg.get("trail_atr_mult",  "1.5"),
        "Trail Act. Mult":   snap_cfg.get("trail_act_mult",  "1.0"),
        "Environment":       db.get_active_mode(),
        "BiasFilter":        "Daily EMA9/21 (1d candles)",
        "Neutral Passthru":  "ON (trades in neutral market)" if bias_pass else "OFF (blocks all in neutral)",
        "Neutral Threshold": f"{bias_thr:.2f}%",
    }
    for label, val in rows.items():
        col_l, col_v = st.columns([2, 3])
        col_l.markdown(
            f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>{label.upper()}</span>",
            unsafe_allow_html=True,
        )
        col_v.markdown(f"`{val}`")
