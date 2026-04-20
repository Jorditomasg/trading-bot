"""KPI row section — refreshes every 10s."""

import streamlit as st

from bot.database.db import Database
from bot.metrics import sharpe_ratio, max_drawdown, profit_factor
from dashboard.constants import RefreshRates
from dashboard.utils import fmt


@st.fragment(run_every=RefreshRates.KPI)
def kpi_row_section(db: Database) -> None:
    equity_curve = db.get_equity_curve()
    trades       = db.get_all_trades()
    closed       = [t for t in trades if t.get("exit_price") is not None]

    current_balance = equity_curve[-1]["balance"] if equity_curve else 0.0

    # Only count PnL the bot generated — immune to external balance changes
    total_pnl     = sum(t["pnl"] for t in closed if t.get("pnl") is not None)
    total_pnl_pct = (total_pnl / current_balance * 100) if current_balance > 0 else 0.0
    wins          = sum(1 for t in closed if t.get("pnl") and t["pnl"] > 0)
    win_rate      = (wins / len(closed) * 100) if closed else 0.0
    sharpe        = sharpe_ratio(equity_curve)
    max_dd        = max_drawdown(equity_curve)

    pf = profit_factor(closed)

    k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
    k1.metric("Balance",       f"${fmt(current_balance)}")
    k2.metric("Total PnL",     f"${fmt(total_pnl, '+,.2f')}", delta=f"{fmt(total_pnl_pct, '+.2f')}%")
    k3.metric("Win Rate",      f"{fmt(win_rate, '.1f')}%")
    k4.metric("Profit Factor", f"{pf:.2f}" if pf != float("inf") else "∞")
    k5.metric("Sharpe (ann.)", fmt(sharpe, ".2f"))
    k6.metric("Max Drawdown",  f"{fmt(max_dd * 100, '.2f')}%")
    k7.metric("Trades",        str(len(closed)))
