from enum import Enum


class ExitReason(str, Enum):
    STOP_LOSS    = "STOP_LOSS"
    TAKE_PROFIT  = "TAKE_PROFIT"


class TradeAction(str, Enum):
    OPEN  = "OPEN"
    CLOSE = "CLOSE"


class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyName(str, Enum):
    EMA_CROSSOVER = "EMA_CROSSOVER"
