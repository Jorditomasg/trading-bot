"""Signal log section — refreshes every 10s."""

import pandas as pd
import streamlit as st

from bot.database.db import Database
from dashboard.constants import GREEN, RED, WHITE, MUTED, CAPTION, RefreshRates


@st.fragment(run_every=RefreshRates.POSITION)
def signal_log_section(db: Database, symbol: str) -> None:
    recent_signals = db.get_recent_signals(50, symbol=symbol)

    if not recent_signals:
        st.caption(f"no signals yet for {symbol}")
        return

    df_s = pd.DataFrame([
        {
            "TIME":     s["timestamp"][:19].replace("T", " "),
            "STRATEGY": s["strategy"],
            "REGIME":   s["regime"],
            "BIAS":     s.get("bias")     or "—",
            "MOMENTUM": s.get("momentum") or "—",
            "ACTION":   s["action"],
            "STR":      f"{s['strength']:.2f}",
        }
        for s in recent_signals
    ])

    def _style_action(val: str):
        if val == "BUY":  return f"color: {WHITE}; font-weight: 700"
        if val == "SELL": return f"color: {RED}; font-weight: 700"
        return f"color: {CAPTION}"

    def _style_regime(val: str):
        if val == "VOLATILE": return f"color: {RED}"
        if val == "TRENDING": return f"color: {WHITE}"
        return f"color: {MUTED}"

    def _style_bias(val: str):
        if val == "BULLISH": return f"color: {GREEN}; font-weight: 700"
        if val == "BEARISH": return f"color: {RED}; font-weight: 700"
        return f"color: {CAPTION}"

    def _style_momentum(val: str):
        if val == "BULLISH": return f"color: {GREEN}; font-weight: 700"
        if val == "BEARISH": return f"color: {RED}; font-weight: 700"
        if val == "NEUTRAL": return f"color: {MUTED}"
        return f"color: {CAPTION}"

    styled_s = (
        df_s.style
        .map(_style_action,   subset=["ACTION"])
        .map(_style_regime,   subset=["REGIME"])
        .map(_style_bias,     subset=["BIAS"])
        .map(_style_momentum, subset=["MOMENTUM"])
    )
    st.dataframe(styled_s, use_container_width=True, hide_index=True)
