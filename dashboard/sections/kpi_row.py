"""KPI row section — refreshes every 10s."""

import streamlit as st

from bot.database.db import Database
from bot.metrics import sharpe_ratio, max_drawdown
from dashboard.constants import RefreshRates
from dashboard.utils import fmt


@st.fragment(run_every=RefreshRates.KPI)
def kpi_row_section(db: Database) -> None:
    equity_curve = db.get_equity_curve()
    trades       = db.get_all_trades()
    closed       = [t for t in trades if t.get("exit_price") is not None]

    current_balance = equity_curve[-1]["balance"] if equity_curve else 0.0

    total_pnl     = sum(t["pnl"] for t in closed if t.get("pnl") is not None)
    total_pnl_pct = (total_pnl / current_balance * 100) if current_balance > 0 else 0.0
    sharpe        = sharpe_ratio(equity_curve)
    max_dd        = max_drawdown(equity_curve)

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Balance",      f"${fmt(current_balance)}")
    k2.metric("Total PnL",    f"${fmt(total_pnl, '+,.2f')}", delta=f"{fmt(total_pnl_pct, '+.2f')}%")
    k3.metric("Max Drawdown", f"{fmt(max_dd * 100, '.2f')}%")
    k4.metric("Sharpe (ann.)", fmt(sharpe, ".2f"))
