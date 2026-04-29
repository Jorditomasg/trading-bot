"""Bot configuration manager — runtime params, notifications, credentials."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from bot.config import settings
from bot.credentials import encrypt
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from bot.optimizer.auto_optimizer import OPTIMIZER_INTERVAL_DAYS, LAST_RUN_KEY
from bot.telegram_notifier import TelegramNotifier
from dashboard.constants import RED

_SYMBOLS    = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"]
_TIMEFRAMES = ["1h", "2h", "4h", "8h", "1d"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_mainnet(api_key: str, api_secret: str) -> tuple[bool, str]:
    try:
        client  = BinanceClient(api_key=api_key, api_secret=api_secret, testnet=False)
        balance = client.get_balance("USDT")
        return True, f"${balance:,.2f} USDT"
    except Exception as exc:
        return False, str(exc)


# ── Main section ──────────────────────────────────────────────────────────────

def config_manager_section(db: Database) -> None:
    # ── Mainnet safety guard ──────────────────────────────────────────────────
    if db.get_active_mode() == "MAINNET" and not db.has_mainnet_credentials():
        st.error(
            "⛔ MAINNET is active but no credentials are stored. "
            "The bot cannot connect to the exchange. Reverting to TESTNET."
        )
        db.set_active_mode("TESTNET")
        st.rerun()

    cfg = db.get_runtime_config()

    cur_symbols_raw = cfg.get("symbols", cfg.get("symbol", settings.symbol))
    cur_symbols     = [s.strip() for s in cur_symbols_raw.split(",") if s.strip()]
    cur_tf               = cfg.get("timeframe",               settings.timeframe)
    cur_risk             = float(cfg.get("risk_per_trade",    settings.risk_per_trade))
    cur_max_dd           = float(cfg.get("max_drawdown",      0.15))
    cur_max_conc         = int(cfg.get("max_concurrent",      1))
    cur_cooldown         = int(cfg.get("cooldown_hours",      4))
    cur_bias_passthrough = cfg.get("bias_neutral_passthrough", "true") == "true"
    cur_bias_threshold   = float(cfg.get("bias_neutral_threshold", "0.001"))
    cur_long_only        = cfg.get("long_only", "false") == "true"

    # ── Trading ───────────────────────────────────────────────────────────────
    st.markdown("## Trading")

    with st.form("bot_config_form"):
        col_sym, col_tf = st.columns(2)
        with col_sym:
            symbols = st.multiselect(
                "Active Symbols",
                _SYMBOLS,
                default=[s for s in cur_symbols if s in _SYMBOLS] or [_SYMBOLS[0]],
                help="Symbols traded simultaneously. One position per symbol max. Restart required to apply.",
            )
            if not symbols:
                st.warning("Select at least one symbol.")
        with col_tf:
            tf_idx    = _TIMEFRAMES.index(cur_tf) if cur_tf in _TIMEFRAMES else 2
            timeframe = st.selectbox("Timeframe", _TIMEFRAMES, index=tf_idx)

        st.markdown("## Risk")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            risk_pct = st.number_input(
                "Risk / Trade (%)", min_value=0.1, max_value=10.0,
                value=round(cur_risk * 100, 2), step=0.1, format="%.1f",
            )
            if risk_pct > 5.0:
                st.warning(f"⚠ {risk_pct:.0f}% risk — max drawdown scales proportionally. Only use if you accept large swings.")
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

        st.markdown("## BiasFilter & Direction")
        b1, b2, b3 = st.columns(3)
        with b1:
            bias_passthrough = st.checkbox(
                "Allow trades in NEUTRAL bias",
                value=cur_bias_passthrough,
                help=(
                    "NEUTRAL = daily EMAs have no clear direction. "
                    "Enabled: both BUY and SELL pass — filter only blocks AGAINST-trend signals. "
                    "Disabled: all signals blocked in indeterminate markets."
                ),
            )
        with b2:
            bias_threshold_pct = st.number_input(
                "Neutral threshold (%)", min_value=0.05, max_value=1.0,
                value=round(cur_bias_threshold * 100, 2), step=0.05, format="%.2f",
                help="EMA9/EMA21 gap must exceed this % to be directional. Default 0.10%.",
            )
        with b3:
            long_only_mode = st.checkbox(
                "Long-only mode (BUY only)",
                value=cur_long_only,
                help=(
                    "Disable all SELL/short signals — only BUY entries are taken. "
                    "Recommended for bull-market conditions. "
                    "Historically ~doubles annual return vs BUY+SELL over a full cycle."
                ),
            )

        saved = st.form_submit_button("💾  Save Configuration", use_container_width=True)

    if saved:
        if not symbols:
            st.error("Cannot save — select at least one symbol.")
        else:
            db.set_runtime_config(
                symbols=",".join(symbols),
                timeframe=timeframe,
                risk_per_trade=str(round(risk_pct / 100, 4)),
                max_drawdown=str(round(max_dd_pct / 100, 3)),
                max_concurrent=str(max_concurrent),
                cooldown_hours=str(cooldown_hours),
                bias_neutral_passthrough="true" if bias_passthrough else "false",
                bias_neutral_threshold=str(round(bias_threshold_pct / 100, 4)),
                long_only="true" if long_only_mode else "false",
            )
            st.success("Configuration saved — restart the bot to apply all changes.")

    st.divider()

    # ── Telegram notifications ────────────────────────────────────────────────
    st.markdown("## Notifications")

    tg_cfg       = db.get_telegram_config()
    has_tg_token = bool(tg_cfg["token"])
    tg_enabled   = st.checkbox(
        "Enable Telegram notifications",
        value=tg_cfg["enabled"],
        key="cfg_tg_enabled",
    )

    if has_tg_token and not st.session_state.get("cfg_show_tg_form", False):
        st.markdown(
            "<span style='font-size:0.75rem;color:#555;letter-spacing:0.1em'>"
            "Token &nbsp;<code>••••••••••••••••</code></span>",
            unsafe_allow_html=True,
        )
        st.text_input("Chat ID", value=tg_cfg["chat_id"], key="cfg_tg_chat_display", disabled=True)

        col_change, col_test = st.columns(2)
        with col_change:
            if st.button("Change credentials", key="cfg_tg_change", use_container_width=True):
                st.session_state["cfg_show_tg_form"] = True
                st.rerun()
        with col_test:
            if st.button("Send test message", key="cfg_tg_test", use_container_width=True):
                with st.spinner("Sending..."):
                    ok, msg = TelegramNotifier.test_send(tg_cfg["token"], tg_cfg["chat_id"])
                st.success(msg) if ok else st.error(msg)

        if st.session_state.get("_cfg_tg_enabled_prev") != tg_enabled:
            st.session_state["_cfg_tg_enabled_prev"] = tg_enabled
            db.save_telegram_config(tg_cfg["token"], tg_cfg["chat_id"], tg_enabled)
    else:
        tg_token = st.text_input(
            "Bot token", type="password", key="cfg_tg_token",
            placeholder="123456789:ABCdef...",
            help="Create a bot via @BotFather on Telegram and paste the token here.",
        )
        tg_chat_id = st.text_input(
            "Chat ID", key="cfg_tg_chat_id",
            placeholder="-100123456789",
            help="Your personal or group chat ID. Use @userinfobot to find it.",
        )
        _tg_ready = bool(tg_token.strip() and tg_chat_id.strip())

        col_save, col_test = st.columns(2)
        with col_save:
            if st.button("Save", key="cfg_tg_save", use_container_width=True, disabled=not _tg_ready):
                db.save_telegram_config(tg_token.strip(), tg_chat_id.strip(), tg_enabled)
                st.session_state.pop("cfg_show_tg_form", None)
                st.success("Saved.")
                st.rerun()
        with col_test:
            if st.button("Test", key="cfg_tg_test2", use_container_width=True, disabled=not _tg_ready):
                with st.spinner("Sending..."):
                    ok, msg = TelegramNotifier.test_send(tg_token.strip(), tg_chat_id.strip())
                st.success(msg) if ok else st.error(msg)

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
        mode = db.get_active_mode()
        if mode == "MAINNET":
            if st.button("Switch to TESTNET", use_container_width=True):
                db.set_active_mode("TESTNET")
                st.info("Switched to TESTNET. Restart required.")
        else:
            st.markdown(
                "<span style='font-size:0.65rem;color:#555;letter-spacing:0.1em'>"
                "To activate MAINNET, use the<br>Mainnet Credentials section below.</span>",
                unsafe_allow_html=True,
            )

    st.divider()

    # ── Mainnet credentials ───────────────────────────────────────────────────
    with st.expander("🔑  Mainnet Credentials", expanded=False):
        active_mode = db.get_active_mode()
        has_creds   = db.has_mainnet_credentials()

        mode_color = "#e05252" if active_mode == "MAINNET" else "#4caf7d"
        mode_label = "● MAINNET LIVE" if active_mode == "MAINNET" else "● TESTNET"
        st.markdown(
            f"<span style='font-size:0.7rem;letter-spacing:0.12em;color:{mode_color}'>"
            f"{mode_label}</span>",
            unsafe_allow_html=True,
        )

        if has_creds and not st.session_state.get("cfg_show_cred_form", False):
            st.markdown(
                "<span style='font-size:0.75rem;color:#555;letter-spacing:0.1em'>"
                "API KEY &nbsp;<code>••••••••••••••••</code>&nbsp;&nbsp;"
                "SECRET &nbsp;<code>••••••••••••••••</code></span>",
                unsafe_allow_html=True,
            )
            if st.button("Replace credentials", key="cfg_cred_replace"):
                st.session_state["cfg_show_cred_form"] = True
                st.rerun()
        else:
            api_key    = st.text_input("API Key",    type="password", key="cfg_mn_api_key")
            api_secret = st.text_input("API Secret", type="password", key="cfg_mn_api_secret")

            if st.button("Verify connection", key="cfg_cred_verify",
                         disabled=not (api_key and api_secret)):
                with st.spinner("Connecting to mainnet..."):
                    ok, msg = _verify_mainnet(api_key, api_secret)
                if ok:
                    st.session_state.update({
                        "cfg_mn_verified":       True,
                        "cfg_mn_balance_msg":    msg,
                        "cfg_mn_pending_key":    api_key,
                        "cfg_mn_pending_secret": api_secret,
                    })
                else:
                    st.session_state["cfg_mn_verified"] = False
                    st.error(f"Connection failed: {msg}")

        if st.session_state.get("cfg_mn_verified"):
            st.success(f"Connected — Balance: {st.session_state['cfg_mn_balance_msg']}")
            st.markdown(
                f"<span style='color:{RED};font-size:0.75rem;letter-spacing:0.08em'>"
                "⚠ WARNING: MAINNET will execute REAL orders with real money.</span>",
                unsafe_allow_html=True,
            )
            confirm = st.text_input("Type CONFIRM to activate:", key="cfg_mn_confirm")
            if st.button("ACTIVATE MAINNET", key="cfg_mn_activate",
                         disabled=confirm.strip() != "CONFIRM", type="primary"):
                fk = settings.fernet_key
                db.save_mainnet_credentials(
                    encrypt(st.session_state["cfg_mn_pending_key"],    fk),
                    encrypt(st.session_state["cfg_mn_pending_secret"], fk),
                )
                db.set_active_mode("MAINNET")
                for k in ["cfg_mn_verified", "cfg_mn_balance_msg", "cfg_mn_pending_key",
                          "cfg_mn_pending_secret", "cfg_show_cred_form"]:
                    st.session_state.pop(k, None)
                st.success("MAINNET activated. Takes effect on next bot cycle.")
                st.rerun()

        if active_mode == "MAINNET":
            st.markdown("---")
            if st.button("Switch to TESTNET", key="cfg_switch_testnet"):
                db.set_active_mode("TESTNET")
                st.success("Switched to TESTNET.")
                st.rerun()

    st.divider()

    # ── Active configuration snapshot ─────────────────────────────────────────
    st.markdown("## Active Configuration")
    st.caption("Values the bot will use on next restart.")

    snap_cfg   = db.get_runtime_config()
    bias_pass  = snap_cfg.get("bias_neutral_passthrough", "true") == "true"
    bias_thr   = float(snap_cfg.get("bias_neutral_threshold", "0.001")) * 100
    long_only  = snap_cfg.get("long_only", "false") == "true"
    tg_snap    = db.get_telegram_config()

    rows = {
        "Symbol":            snap_cfg.get("symbol",       settings.symbol),
        "Timeframe":         snap_cfg.get("timeframe",    settings.timeframe),
        "Risk / Trade":      f"{float(snap_cfg.get('risk_per_trade', settings.risk_per_trade)) * 100:.2f}%",
        "Max Drawdown":      f"{float(snap_cfg.get('max_drawdown', 0.15)) * 100:.1f}%",
        "Max Positions":     snap_cfg.get("max_concurrent", "1"),
        "Cooldown":          f"{snap_cfg.get('cooldown_hours', '4')}h",
        "Environment":       db.get_active_mode(),
        "Neutral Passthru":  "ON" if bias_pass else "OFF (blocks all in neutral)",
        "Neutral Threshold": f"{bias_thr:.2f}%",
        "Long-only Mode":    "ON (BUY signals only)" if long_only else "OFF (BUY + SELL)",
        "Telegram":          (
            f"{'enabled' if tg_snap['enabled'] else 'disabled'} — chat {tg_snap['chat_id']}"
            if tg_snap["token"] else "not configured"
        ),
    }
    for label, val in rows.items():
        col_l, col_v = st.columns([2, 3])
        col_l.markdown(
            f"<span style='font-size:0.65rem;letter-spacing:0.12em;color:#555'>{label.upper()}</span>",
            unsafe_allow_html=True,
        )
        col_v.markdown(f"`{val}`")

    st.divider()

    # ── Auto-optimizer status (read-only) ─────────────────────────────────────
    _auto_optimizer_status_section(db)


def _auto_optimizer_status_section(db: Database) -> None:
    """Read-only panel showing the last auto-optimizer run and current EMA params."""
    st.markdown("## Auto-optimizer")
    st.caption(
        f"Runs automatically every {OPTIMIZER_INTERVAL_DAYS} days. "
        "Finds the SL/TP multipliers with the best profit factor and applies them live — no restart needed."
    )

    try:
        _auto_optimizer_status_content(db)
    except Exception as exc:
        st.warning(f"Could not load auto-optimizer status: {exc}")


def _auto_optimizer_status_content(db: Database) -> None:
    cfg          = db.get_runtime_config()
    last_run_str = cfg.get(LAST_RUN_KEY)
    cur_stop     = float(cfg.get("ema_stop_mult", 1.5))
    cur_tp       = float(cfg.get("ema_tp_mult",   3.5))

    # Last-run timing
    if last_run_str:
        try:
            last_run = datetime.fromisoformat(last_run_str)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            now         = datetime.now(tz=timezone.utc)
            elapsed     = now - last_run
            elapsed_d   = elapsed.days
            next_run_d  = max(0, OPTIMIZER_INTERVAL_DAYS - elapsed_d)
            last_label  = (
                "today" if elapsed_d == 0
                else f"{elapsed_d}d ago ({last_run.strftime('%Y-%m-%d')})"
            )
            next_label  = "overdue — will run on next bot startup" if next_run_d == 0 else f"in {next_run_d}d"
        except ValueError:
            last_label = last_run_str
            next_label = "unknown"
    else:
        last_label = "never (will run on next bot startup)"
        next_label = "on next bot startup"

    # Best recent auto-applied run
    recent_runs = db.get_optimizer_runs(limit=5)
    auto_applied = [r for r in recent_runs if r.get("status") == "auto_applied"]
    best_pf_label = f"{auto_applied[0]['profit_factor']:.2f}" if auto_applied else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Last run",   last_label)
    c2.metric("Next run",   next_label)
    c3.metric("SL mult",    f"{cur_stop:.2f}×")
    c4.metric("TP mult",    f"{cur_tp:.2f}×")

    if auto_applied:
        r = auto_applied[0]
        st.markdown(
            f"<span style='font-size:0.7rem;color:#555;letter-spacing:0.08em'>"
            f"Last applied: SL {r['ema_stop_mult']:.2f}× · TP {r['ema_tp_mult']:.2f}×  "
            f"| PF {r['profit_factor']:.2f}  Sharpe {r['sharpe_ratio']:.2f}  "
            f"WR {r['win_rate']:.1f}%  MaxDD {r['max_drawdown']:.1f}%  "
            f"Trades {r['total_trades']}"
            f"</span>",
            unsafe_allow_html=True,
        )
