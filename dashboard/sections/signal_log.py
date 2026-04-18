"""Signal log section — refreshes every 10s."""

import pandas as pd
import streamlit as st

from bot.database.db import Database
from dashboard.constants import RED, WHITE, MUTED, CAPTION, RefreshRates


@st.fragment(run_every=RefreshRates.POSITION)
def signal_log_section(db: Database) -> None:
    recent_signals = db.get_recent_signals(20)

    if not recent_signals:
        st.caption("no signals yet")
        return

    df_s = pd.DataFrame([
        {
            "TIME":     s["timestamp"][:19].replace("T", " "),
            "STRATEGY": s["strategy"],
            "REGIME":   s["regime"],
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

    styled_s = (
        df_s.style
        .applymap(_style_action, subset=["ACTION"])
        .applymap(_style_regime, subset=["REGIME"])
    )
    st.dataframe(styled_s, use_container_width=True, hide_index=True)
