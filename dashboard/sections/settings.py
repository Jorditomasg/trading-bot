"""Mainnet credentials panel — accessible via the ⚙ popover."""

import streamlit as st

from bot.config import settings
from bot.credentials import encrypt
from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RED, RefreshRates


def _verify_mainnet_credentials(api_key: str, api_secret: str) -> tuple[bool, str]:
    """Test mainnet credentials. Returns (success, message)."""
    try:
        client  = BinanceClient(api_key=api_key, api_secret=api_secret, testnet=False)
        balance = client.get_balance("USDT")
        return True, f"${balance:,.2f} USDT"
    except Exception as exc:
        return False, str(exc)


@st.fragment(run_every=RefreshRates.PERFORMANCE)
def settings_section(db: Database) -> None:
    active_mode = db.get_active_mode()
    has_creds   = db.has_mainnet_credentials()

    # ── Mode pill ─────────────────────────────────────────────────────────────
    if active_mode == "MAINNET":
        st.markdown(
            "<span class='pill pill-stopped'>● MAINNET LIVE</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span class='pill pill-running'>● TESTNET</span>",
            unsafe_allow_html=True,
        )

    # ── Mainnet credentials ───────────────────────────────────────────────────
    st.markdown("### Mainnet Credentials")

    if has_creds:
        st.markdown(
            "<span style='font-size:0.75rem;color:#555;letter-spacing:0.1em'>"
            "API KEY &nbsp;<code>••••••••••••••••</code>&nbsp;&nbsp;"
            "API SECRET &nbsp;<code>••••••••••••••••</code></span>",
            unsafe_allow_html=True,
        )
        if st.button("Replace credentials"):
            st.session_state["show_cred_form"] = True
    else:
        st.session_state["show_cred_form"] = True

    if st.session_state.get("show_cred_form", False) or not has_creds:
        api_key    = st.text_input("API Key",    type="password", key="mn_api_key")
        api_secret = st.text_input("API Secret", type="password", key="mn_api_secret")

        if st.button("Verify connection", disabled=not (api_key and api_secret)):
            with st.spinner("Connecting to mainnet..."):
                ok, msg = _verify_mainnet_credentials(api_key, api_secret)
            if ok:
                st.session_state["mn_verified"]       = True
                st.session_state["mn_balance_msg"]    = msg
                st.session_state["mn_pending_key"]    = api_key
                st.session_state["mn_pending_secret"] = api_secret
            else:
                st.session_state["mn_verified"] = False
                st.error(f"Connection failed: {msg}")

    if st.session_state.get("mn_verified"):
        st.success(f"Connected — Balance: {st.session_state['mn_balance_msg']}")
        st.markdown(
            f"<span style='color:{RED};font-size:0.75rem;letter-spacing:0.08em'>"
            "⚠ WARNING: activating MAINNET will execute REAL orders with real money "
            "on the next bot cycle.</span>",
            unsafe_allow_html=True,
        )

        confirm_input   = st.text_input("Type CONFIRM to activate:", key="mn_confirm_input")
        activate_disabled = confirm_input.strip() != "CONFIRM"

        if st.button("ACTIVATE MAINNET", disabled=activate_disabled, type="primary"):
            fk         = settings.fernet_key
            enc_key    = encrypt(st.session_state["mn_pending_key"],    fk)
            enc_secret = encrypt(st.session_state["mn_pending_secret"], fk)
            db.save_mainnet_credentials(enc_key, enc_secret)
            db.set_active_mode("MAINNET")
            for k in ["mn_verified", "mn_balance_msg", "mn_pending_key",
                      "mn_pending_secret", "show_cred_form"]:
                st.session_state.pop(k, None)
            st.success("MAINNET activated. Takes effect on next bot cycle.")
            st.rerun()

    if active_mode == "MAINNET":
        st.markdown("---")
        if st.button("Switch to TESTNET"):
            db.set_active_mode("TESTNET")
            st.success("Switched to TESTNET.")
            st.rerun()

    st.markdown(
        "<span style='font-size:0.6rem;color:#333;letter-spacing:0.08em'>"
        "Telegram notifications → CONFIG tab</span>",
        unsafe_allow_html=True,
    )
