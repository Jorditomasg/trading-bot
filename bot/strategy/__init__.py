from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.ema_crossover import EMACrossoverStrategy
from bot.strategy.mean_reversion import MeanReversionStrategy
from bot.strategy.breakout import BreakoutStrategy

__all__ = [
    "BaseStrategy",
    "Signal",
    "EMACrossoverStrategy",
    "MeanReversionStrategy",
    "BreakoutStrategy",
]
