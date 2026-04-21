import logging
from dataclasses import dataclass, field

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)


@dataclass
class EMACrossoverConfig:
    fast_period:      int   = 9
    slow_period:      int   = 21
    atr_period:       int   = 14
    max_distance_atr: float = 1.5   # max ATR dist from EMA9 for continuation entry
    stop_atr_mult:    float = 1.5
    tp_atr_mult:      float = 3.5

    # ── Quality filters (0 / False = disabled → backward-compatible defaults) ──
    volume_period:         int   = 20
    volume_multiplier:     float = 0.0   # crossover: require vol > mult × SMA; 0 = off
    min_atr_pct:           float = 0.0   # skip if ATR < pct × price (dead market); 0 = off
    require_bar_direction: bool  = False  # crossover bar must close in signal direction
    require_ema_momentum:  bool  = False  # continuation: EMA9 must be rising/falling


class EMACrossoverStrategy(BaseStrategy):
    def __init__(self, config: EMACrossoverConfig = EMACrossoverConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "EMA_CROSSOVER"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = (
            max(self.config.slow_period, self.config.volume_period)
            + self.config.atr_period
            + 4
        )
        if len(df) < required:
            logger.warning("EMACrossover: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        close = df["close"]
        fast  = close.ewm(span=self.config.fast_period,  adjust=False).mean()
        slow  = close.ewm(span=self.config.slow_period,  adjust=False).mean()
        atr   = compute_atr(df, self.config.atr_period)

        current_atr   = atr.iloc[-1]
        current_price = float(close.iloc[-1])

        # ── Dead-market filter ────────────────────────────────────────────────
        if self.config.min_atr_pct > 0 and current_atr < current_price * self.config.min_atr_pct:
            return hold_signal(atr=current_atr)

        # ── Volume context ────────────────────────────────────────────────────
        volume_ok_crossover = True
        if self.config.volume_multiplier > 0 and "volume" in df.columns:
            volume_sma = df["volume"].rolling(self.config.volume_period).mean()
            avg_vol    = float(volume_sma.iloc[-1])
            cur_vol    = float(df["volume"].iloc[-1])
            if not pd.isna(avg_vol) and avg_vol > 0:
                volume_ok_crossover = cur_vol >= self.config.volume_multiplier * avg_vol

        # ── Bar direction (last closed bar) ───────────────────────────────────
        last_open  = float(df["open"].iloc[-1])
        last_close = float(df["close"].iloc[-1])
        bar_bullish = last_close > last_open
        bar_bearish = last_close < last_open

        # ── EMA9 momentum — rising / falling over last 3 bars ─────────────────
        ema9_rising  = float(fast.iloc[-1]) > float(fast.iloc[-4])
        ema9_falling = float(fast.iloc[-1]) < float(fast.iloc[-4])

        # ── Crossover detection with optional quality gates ───────────────────
        crossed_up = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
        if crossed_up and self.config.require_bar_direction:
            crossed_up = crossed_up and bar_bullish
        if crossed_up and self.config.volume_multiplier > 0:
            crossed_up = crossed_up and volume_ok_crossover

        crossed_down = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
        if crossed_down and self.config.require_bar_direction:
            crossed_down = crossed_down and bar_bearish
        if crossed_down and self.config.volume_multiplier > 0:
            crossed_down = crossed_down and volume_ok_crossover

        # ── Continuation (pullback to EMA9) with optional momentum gate ───────
        dist_atr = (
            abs(current_price - float(fast.iloc[-1])) / current_atr
            if current_atr > 0 else float("inf")
        )
        in_trend_buy = (
            float(fast.iloc[-1]) > float(slow.iloc[-1])
            and dist_atr < self.config.max_distance_atr
        )
        in_trend_sell = (
            float(fast.iloc[-1]) < float(slow.iloc[-1])
            and dist_atr < self.config.max_distance_atr
        )
        if self.config.require_ema_momentum:
            in_trend_buy  = in_trend_buy  and ema9_rising  and bar_bullish
            in_trend_sell = in_trend_sell and ema9_falling and bar_bearish

        # ── Action selection ──────────────────────────────────────────────────
        action = "HOLD"
        if crossed_up or in_trend_buy:
            action = "BUY"
        elif crossed_down or in_trend_sell:
            action = "SELL"

        if action == "BUY":
            if crossed_up:
                fast_slope = fast.iloc[-1] - fast.iloc[-2]
                strength = max(min(abs(fast_slope) / current_atr * 5, 1.0), 0.65) if current_atr > 0 else 0.65
            else:
                strength = max(min(0.5 * (1 - dist_atr / self.config.max_distance_atr) + 0.4, 0.8), 0.4)
            sl, tp = calculate_levels(
                "BUY", current_price, current_atr,
                self.config.stop_atr_mult, self.config.tp_atr_mult,
            )
            return buy_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        if action == "SELL":
            if crossed_down:
                fast_slope = fast.iloc[-1] - fast.iloc[-2]
                strength = max(min(abs(fast_slope) / current_atr * 5, 1.0), 0.65) if current_atr > 0 else 0.65
            else:
                strength = max(min(0.5 * (1 - dist_atr / self.config.max_distance_atr) + 0.4, 0.8), 0.4)
            sl, tp = calculate_levels(
                "SELL", current_price, current_atr,
                self.config.stop_atr_mult, self.config.tp_atr_mult,
            )
            return sell_signal(strength=strength, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
