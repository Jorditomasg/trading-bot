"""ADX gate kill-switch for the walk-forward audit harness.

The kill-switch is a SAMPLE-SIZE QUALITY GATE on a candidate config — it belongs
to the validation layer, not inside BacktestEngine (design D5).

When the total number of trades across the provided WindowResult list falls below
``min_trades``, the gate triggers and the comparator should emit a CRITICAL log,
downgrade the verdict, or reject the challenger automatically.

This is a pure function with no I/O — fully testable in isolation.

Usage:
    from bot.audit.kill_switch import evaluate_adx_kill_switch

    results = walk_forward.run_all(...)
    ks = evaluate_adx_kill_switch(results)
    if ks["triggered"]:
        log.critical("ADX kill-switch triggered: %d trades < %d", ks["total_trades"], ks["threshold"])
"""
from __future__ import annotations


def evaluate_adx_kill_switch(
    results: list,         # list[WindowResult] — duck-typed to avoid circular import
    *,
    min_trades: int = 30,
) -> dict:
    """Evaluate whether the ADX gate kill-switch should trigger.

    Sums ``total_trades`` across all provided ``WindowResult`` objects. When the
    sum is below ``min_trades``, the kill-switch is triggered — meaning the
    challenger config produced too few trades to be statistically meaningful.

    Parameters
    ----------
    results:
        List of ``WindowResult`` objects from ``bot.audit.walk_forward.run_all()``.
        May be empty (→ triggered=True with 0 trades).
    min_trades:
        Minimum number of total trades required across all windows.
        Default 30 (spec REQ-ADX-4). Triggered when ``total_trades < min_trades``.

    Returns
    -------
    dict with keys:
        ``triggered`` (bool)  — True when kill-switch should fire.
        ``total_trades`` (int) — sum of total_trades across all results.
        ``threshold`` (int)   — the min_trades value used.
    """
    total = sum(int(getattr(r, "total_trades", 0)) for r in results)
    return {
        "triggered":    total < min_trades,
        "total_trades": total,
        "threshold":    min_trades,
    }
