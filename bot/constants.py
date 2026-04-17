from enum import Enum


class ExitReason(str, Enum):
    TRAILING_STOP    = "TRAILING_STOP"
    STOP_LOSS        = "STOP_LOSS"
    TAKE_PROFIT      = "TAKE_PROFIT"
    SIGNAL_REVERSAL  = "SIGNAL_REVERSAL"


class TradeAction(str, Enum):
    OPEN  = "OPEN"
    CLOSE = "CLOSE"


class OrderSide(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class StrategyName(str, Enum):
    EMA_CROSSOVER  = "EMA_CROSSOVER"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT       = "BREAKOUT"
