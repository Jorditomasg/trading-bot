"""Signal log section — refreshes every 10s."""

import pandas as pd
import streamlit as st

from bot.database.db import Database


@st.fragment(run_every=10)
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
        if val == "BUY":  return "color: #F5F5F5; font-weight: 700"
        if val == "SELL": return "color: #FF0000; font-weight: 700"
        return "color: #333"

    def _style_regime(val: str):
        if val == "VOLATILE": return "color: #FF0000"
        if val == "TRENDING": return "color: #F5F5F5"
        return "color: #555"

    styled_s = (
        df_s.style
        .applymap(_style_action, subset=["ACTION"])
        .applymap(_style_regime, subset=["REGIME"])
    )
    st.dataframe(styled_s, use_container_width=True, hide_index=True)
