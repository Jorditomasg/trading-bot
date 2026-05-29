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
    # Total return is measured against starting capital, not the current balance,
    # so the % matches the equity curve's gain from its origin.
    initial_balance = equity_curve[0]["balance"] if equity_curve else 0.0

    pnls          = [t["pnl"] for t in closed if t.get("pnl") is not None]
    total_pnl     = sum(pnls)
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0
    sharpe        = sharpe_ratio(equity_curve)
    max_dd        = max_drawdown(equity_curve)

    n_closed   = len(pnls)
    wins       = sum(1 for p in pnls if p > 0)
    win_rate   = (wins / n_closed * 100) if n_closed else 0.0
    pf         = profit_factor(closed)
    pf_str     = "∞" if pf == float("inf") else f"{pf:.2f}"
    expectancy = (total_pnl / n_closed) if n_closed else 0.0

    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    r1c1.metric("Balance",   f"${fmt(current_balance)}")
    r1c2.metric("Total PnL", f"${fmt(total_pnl, '+,.2f')}", delta=f"{fmt(total_pnl_pct, '+.2f')}%")
    r1c3.metric("Win Rate",  f"{fmt(win_rate, '.1f')}%", delta=f"{wins}/{n_closed}", delta_color="off")
    r1c4.metric("Trades",    str(n_closed))

    r2c1, r2c2, r2c3, r2c4 = st.columns(4)
    r2c1.metric("Profit Factor", pf_str)
    r2c2.metric("Expectancy",    f"${fmt(expectancy, '+,.2f')}", delta="per trade", delta_color="off")
    r2c3.metric("Max Drawdown",  f"{fmt(max_dd * 100, '.2f')}%")
    r2c4.metric("Sharpe (ann.)", fmt(sharpe, ".2f"))
