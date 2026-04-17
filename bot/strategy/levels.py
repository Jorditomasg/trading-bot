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
    """
    if side == "BUY":
        return price - sl_mult * atr, price + tp_mult * atr
    return price + sl_mult * atr, price - tp_mult * atr
