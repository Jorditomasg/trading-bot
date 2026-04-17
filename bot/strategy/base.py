from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    action: str       # "BUY" | "SELL" | "HOLD"
    strength: float   # 0.0 – 1.0
    stop_loss: float
    take_profit: float
    atr: float


class BaseStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal: ...
