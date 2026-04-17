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
