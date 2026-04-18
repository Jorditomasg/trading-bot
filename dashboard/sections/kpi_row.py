"""KPI row section — refreshes every 10s."""

import streamlit as st

from bot.database.db import Database
from bot.metrics import sharpe_ratio, max_drawdown


@st.fragment(run_every=10)
def kpi_row_section(db: Database) -> None:
    equity_curve = db.get_equity_curve()
    trades       = db.get_all_trades()
    closed       = [t for t in trades if t.get("exit_price") is not None]

    initial_balance = equity_curve[0]["balance"]  if equity_curve else 10_000.0
    current_balance = equity_curve[-1]["balance"] if equity_curve else 10_000.0

    total_pnl     = current_balance - initial_balance
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0
    wins          = sum(1 for t in closed if t.get("pnl") and t["pnl"] > 0)
    win_rate      = (wins / len(closed) * 100) if closed else 0.0
    sharpe        = sharpe_ratio(equity_curve)
    max_dd        = max_drawdown(equity_curve)

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Balance",       f"${current_balance:,.2f}")
    k2.metric("Total PnL",     f"${total_pnl:+,.2f}", delta=f"{total_pnl_pct:+.2f}%")
    k3.metric("Win Rate",      f"{win_rate:.1f}%")
    k4.metric("Sharpe (ann.)", f"{sharpe:.2f}")
    k5.metric("Max Drawdown",  f"{max_dd*100:.2f}%")
    k6.metric("Trades",        str(len(closed)))
