from bot.strategy.base import Signal


def hold_signal(atr: float = 0.0) -> Signal:
    return Signal(action="HOLD", strength=0.0, stop_loss=0.0, take_profit=0.0, atr=atr)


def buy_signal(
    strength: float,
    stop_loss: float,
    take_profit: float,
    atr: float,
) -> Signal:
    return Signal(
        action="BUY",
        strength=min(max(strength, 0.0), 1.0),
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=atr,
    )


def sell_signal(
    strength: float,
    stop_loss: float,
    take_profit: float,
    atr: float,
) -> Signal:
    return Signal(
        action="SELL",
        strength=min(max(strength, 0.0), 1.0),
        stop_loss=stop_loss,
        take_profit=take_profit,
        atr=atr,
    )
