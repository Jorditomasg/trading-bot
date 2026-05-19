# Adding a New Strategy

Follow these steps exactly. Do not skip any.

> Reminder: the live orchestrator currently runs only `EMACrossoverStrategy`.
> Other strategies in `bot/strategy/` (e.g. `donchian_breakout.py`) exist for
> research and `BacktestEngine` overrides — they are not wired into live.
> Adding a new strategy to live requires explicit re-validation through the
> walk-forward audit before it can replace or augment the current one.

---

## Step 1 — Create the strategy file

Create `bot/strategy/my_strategy.py`:

```python
import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)

STOP_ATR_MULT = 1.5  # module-level constants for ATR multiples
TP_ATR_MULT   = 2.0


@dataclass
class MyStrategyConfig:
    some_period: int = 20
    atr_period: int = 14


class MyStrategy(BaseStrategy):
    def __init__(self, config: MyStrategyConfig = MyStrategyConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "MY_STRATEGY"  # must match StrategyName enum value

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = self.config.some_period + self.config.atr_period + 2
        if len(df) < required:
            logger.warning("MyStrategy: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        atr = compute_atr(df, self.config.atr_period)
        current_atr = atr.iloc[-1]
        current_price = float(df["close"].iloc[-1])

        # ... your signal logic ...

        if buy_condition:
            sl, tp = calculate_levels("BUY", current_price, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            return buy_signal(strength=0.7, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
```

## Step 2 — Add the enum value

In `bot/constants.py`:

```python
class StrategyName(str, Enum):
    EMA_CROSSOVER  = "EMA_CROSSOVER"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT       = "BREAKOUT"
    MY_STRATEGY    = "MY_STRATEGY"  # add here
```

## Step 3 — Register in the orchestrator

In `bot/orchestrator.py`:

```python
from bot.strategy.my_strategy import MyStrategy

# Add to _strategies dict in __init__:
self._strategies: dict[StrategyName, BaseStrategy] = {
    StrategyName.EMA_CROSSOVER:  EMACrossoverStrategy(),
    StrategyName.MEAN_REVERSION: MeanReversionStrategy(),
    StrategyName.BREAKOUT:       BreakoutStrategy(),
    StrategyName.MY_STRATEGY:    MyStrategy(),   # add here
}

# Map to a regime (or reuse an existing entry):
REGIME_STRATEGY_MAP: dict[MarketRegime, StrategyName] = {
    MarketRegime.TRENDING:  StrategyName.EMA_CROSSOVER,
    MarketRegime.RANGING:   StrategyName.MEAN_REVERSION,
    MarketRegime.VOLATILE:  StrategyName.MY_STRATEGY,   # example
}
```

## Step 4 — Write tests

Tests live in `tests/`. One test file per strategy: `tests/test_my_strategy.py`. Use the
shared OHLCV factories from `tests/conftest.py` (`make_ohlcv`, `uptrend`, `flat`,
`choppy`) and cover BUY / SELL / HOLD / insufficient-data branches.

The dashboard reads strategy names from the DB as strings — no changes needed as long as
`name` matches the `StrategyName` enum value exactly.
