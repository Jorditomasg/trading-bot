import math


def calculate_levels(
    side: str,
    price: float,
    atr: float,
    sl_mult: float,
    tp_mult: float,
) -> tuple[float, float]:
    """Return (stop_loss, take_profit) for a trade entry.

    For BUY:  SL below price, TP above price.
    For SELL: SL above price, TP below price.
    ``tp_mult <= 0`` disables the take-profit: TP is returned as ``0.0``,
    which every exit check treats as "no TP" (the trade exits only via its —
    possibly trailing — stop). See gotcha #50.
    """
    if side == "BUY":
        tp = price + tp_mult * atr if tp_mult > 0 else 0.0
        return price - sl_mult * atr, tp
    tp = price - tp_mult * atr if tp_mult > 0 else 0.0
    return price + sl_mult * atr, tp


def trail_stop(
    side: str,
    current_stop: float,
    peak: float,
    atr: float,
    trail_mult: float,
) -> float:
    """Chandelier trailing stop: ``trail_mult × ATR`` behind the best price since entry.

    ``peak`` is the highest high (BUY) / lowest low (SELL) seen since entry.
    The stop only ever moves in the trade's favour. ``trail_mult <= 0`` or an
    unusable ATR leaves the stop untouched. Shared by the live bot
    (`main._update_trailing_stops`) and both backtest engines so the rule is
    identical by construction.
    """
    if trail_mult <= 0 or atr is None or not math.isfinite(atr) or atr <= 0:
        return current_stop
    if side == "BUY":
        return max(current_stop, peak - trail_mult * atr)
    return min(current_stop, peak + trail_mult * atr)


def tp_hit(side: str, take_profit: float | None, high: float, low: float) -> bool:
    """True when the bar reached an ACTIVE take-profit (``0``/``None`` = disabled)."""
    if not take_profit or take_profit <= 0:
        return False
    return high >= take_profit if side == "BUY" else low <= take_profit
