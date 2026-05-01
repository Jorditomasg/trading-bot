import math
from typing import List


def sharpe_ratio(equity_curve: list[dict], timeframe_hours: int = 1) -> float:
    if len(equity_curve) < 2:
        return 0.0
    import pandas as pd
    returns = pd.Series([r["balance"] for r in equity_curve]).pct_change().dropna()
    std = returns.std()
    return float((returns.mean() / std) * math.sqrt(8760 / timeframe_hours)) if std > 0 else 0.0


def max_drawdown(equity_curve: list[dict]) -> float:
    if not equity_curve:
        return 0.0
    peak = max_dd = 0.0
    for row in equity_curve:
        b = row["balance"]
        if b > peak:
            peak = b
        dd = (peak - b) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def profit_factor(trades: list[dict]) -> float:
    gross_win  = sum(t["pnl"] for t in trades if t.get("pnl") and t["pnl"] > 0)
    gross_loss = sum(abs(t["pnl"]) for t in trades if t.get("pnl") and t["pnl"] < 0)
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def max_consecutive_losses(trades: list[dict]) -> int:
    max_s = cur = 0
    for t in trades:
        if t.get("pnl") is not None and t["pnl"] < 0:
            cur += 1
            max_s = max(max_s, cur)
        else:
            cur = 0
    return max_s


def derive_equity_curve(trades: list[dict], initial_capital: float) -> list[dict]:
    """Build an equity curve from a chronological list of closed trades.

    Each row contains: timestamp, balance, drawdown.
    `trades` must be closed (`pnl is not None`) and ordered by exit_time ASC.
    Open trades are skipped silently.
    """
    closed = [t for t in trades if t.get("pnl") is not None]
    closed.sort(key=lambda t: t.get("exit_time") or "")

    balance = initial_capital
    peak    = initial_capital
    curve: list[dict] = [{
        "timestamp": closed[0].get("entry_time", "") if closed else "",
        "balance":   balance,
        "drawdown":  0.0,
    }]
    for t in closed:
        balance += t["pnl"]
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0.0
        curve.append({
            "timestamp": t.get("exit_time", ""),
            "balance":   balance,
            "drawdown":  dd,
        })
    return curve
