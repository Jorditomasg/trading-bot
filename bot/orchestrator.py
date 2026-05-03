import logging
from typing import Optional

import pandas as pd

from bot.bias.filter import Bias, BiasFilter
from bot.config_presets import get_regime_config, get_strategy_configs
from bot.constants import TradeAction, StrategyName
from bot.momentum.filter import MomentumFilter, MomentumState
from bot.database.db import Database
from bot.regime.detector import MarketRegime, RegimeDetector
from bot.risk.kelly import compute_kelly_fraction, kelly_risk_fraction
from bot.risk.manager import RiskConfig, RiskManager
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.signal_factory import hold_signal

logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    def __init__(
        self,
        db: Database,
        symbol: str,
        risk_config: Optional[RiskConfig] = None,
        bias_filter: Optional[BiasFilter] = None,
        timeframe: str = "1h",
    ) -> None:
        self.db = db
        self.symbol = symbol
        self.timeframe = timeframe
        self.risk_manager = RiskManager(risk_config or RiskConfig(), symbol=symbol)
        self.regime_detector = RegimeDetector(config=get_regime_config(timeframe))
        self.bias_filter = bias_filter

        strategy_cfgs = get_strategy_configs(timeframe)
        self._strategies: dict[StrategyName, BaseStrategy] = {
            StrategyName.EMA_CROSSOVER: EMACrossoverStrategy(
                EMACrossoverConfig(**strategy_cfgs[StrategyName.EMA_CROSSOVER])
            ),
        }
        self._peak_capital: float = db.get_peak_capital() or 0.0
        self._last_momentum_state: MomentumState = MomentumState.BULLISH

    def get_strategy(self, name: StrategyName) -> BaseStrategy:
        """Return the strategy instance for *name*. Raises KeyError if not registered."""
        return self._strategies[name]

    def step(
        self,
        df: pd.DataFrame,
        current_balance: float,
        df_high: Optional[pd.DataFrame] = None,
        df_weekly: Optional[pd.DataFrame] = None,
        total_balance: Optional[float] = None,
    ) -> list[dict]:
        sym = self.symbol
        cb_balance = total_balance if total_balance is not None else current_balance

        # Update High Water Mark (HWM)
        if cb_balance > self._peak_capital:
            self._peak_capital = cb_balance
            self.db.set_peak_capital(self._peak_capital)
            logger.info("[%s] HWM updated: peak=%.2f", sym, self._peak_capital)

        if self.risk_manager.check_circuit_breaker(cb_balance, self._peak_capital):
            logger.warning("[%s] circuit breaker active — no trading this cycle", sym)
            return []

        current_price = float(df["close"].iloc[-1])
        momentum_state = MomentumFilter.get_state(df_weekly, current_price)
        self._last_momentum_state = momentum_state
        logger.info("[%s] momentum=%s", sym, momentum_state)

        regime = self.regime_detector.detect(df)
        logger.info("[%s] regime=%s balance=%.2f", sym, regime.value, current_balance)

        strategy = self._strategies[StrategyName.EMA_CROSSOVER]
        if regime != MarketRegime.TRENDING:
            logger.info("[%s] regime=%s — holding (only TRENDING entries)", sym, regime.value)
            signal = hold_signal(atr=0.0)
        else:
            signal = strategy.generate_signal(df)

        bias: Optional[Bias] = None
        if self.bias_filter is not None:
            bias = self.bias_filter.get_bias(df_high)
            if not self.bias_filter.allows_signal(signal, bias):
                logger.info(
                    "[%s] BiasFilter blocked signal action=%s bias=%s — holding",
                    sym, signal.action, bias.value,
                )
                signal = hold_signal(atr=signal.atr)

        if signal.action != "HOLD":
            self.db.insert_signal(
                symbol=self.symbol,
                strategy=strategy.name,
                regime=regime.value,
                action=signal.action,
                strength=signal.strength,
                bias=bias.value if bias is not None else None,
                momentum=momentum_state.value,
            )
        else:
            logger.debug("[%s] HOLD signal — skipping signals table insert", sym)
        logger.info(
            "[%s] signal action=%s strength=%.2f strategy=%s bias=%s",
            sym, signal.action, signal.strength, strategy.name,
            bias.value if bias is not None else "N/A",
        )

        open_trades = self.db.get_open_trades(symbol=self.symbol)

        if momentum_state == "BEARISH":
            logger.info("[%s] momentum BEARISH — new entry blocked this cycle", sym)
            return []

        # Entry guard: respect max_concurrent_trades
        if len(open_trades) >= self.risk_manager.config.max_concurrent_trades:
            logger.debug(
                "[%s] max concurrent trades reached (%d/%d) — skipping new entry",
                sym, len(open_trades), self.risk_manager.config.max_concurrent_trades,
            )
            return []

        # Duplicate guard: no two open trades with the same side + strategy
        if any(t["side"] == signal.action and t["strategy"] == strategy.name for t in open_trades):
            logger.info(
                "[%s] duplicate %s %s already open — skipping",
                sym, signal.action, strategy.name,
            )
            return []

        # Validate signal strength and direction
        if not self.risk_manager.validate_signal(signal):
            logger.debug("[%s] signal not valid for execution — skipping", sym)
            return []

        kelly_stats = self.db.get_kelly_stats(
            strategy.name,
            self.risk_manager.config.kelly_min_trades,
        )
        if kelly_stats:
            kf = compute_kelly_fraction(
                kelly_stats["win_rate"],
                kelly_stats["avg_win_pct"],
                kelly_stats["avg_loss_pct"],
                half=self.risk_manager.config.kelly_half,
            )
            risk_frac = kelly_risk_fraction(
                kf,
                signal.strength,
                self.risk_manager.config.risk_per_trade,
                max_mult=self.risk_manager.config.kelly_max_mult,
                min_mult=self.risk_manager.config.kelly_min_mult,
            )
            logger.info(
                "[%s] Kelly sizing: strategy=%s win_rate=%.1f%% b=%.2f kf=%.4f strength=%.2f → risk_frac=%.4f (base=%.4f)",
                sym, strategy.name,
                kelly_stats["win_rate"] * 100,
                kelly_stats["avg_win_pct"] / kelly_stats["avg_loss_pct"],
                kf,
                signal.strength,
                risk_frac,
                self.risk_manager.config.risk_per_trade,
            )
        else:
            risk_frac = None
            logger.debug(
                "[%s] Kelly sizing: insufficient trades for %s — using fixed risk_per_trade",
                sym, strategy.name,
            )

        risk_frac_mult = 0.5 if momentum_state == "NEUTRAL" else 1.0
        if risk_frac_mult != 1.0:
            logger.info("[%s] momentum NEUTRAL — risk scaled to 50%%", sym)
        quantity = self.risk_manager.compute_position_size(
            capital=current_balance * risk_frac_mult,
            entry=current_price,
            stop_loss=signal.stop_loss,
            risk_fraction=risk_frac,
        )
        if quantity <= 0:
            logger.warning("[%s] computed quantity=0 — skipping", sym)
            return []

        return [{
            "action":      TradeAction.OPEN,
            "symbol":      self.symbol,
            "side":        signal.action,
            "quantity":    quantity,
            "entry_price": current_price,
            "stop_loss":   signal.stop_loss,
            "take_profit": signal.take_profit,
            "strategy":    strategy.name,
            "regime":      regime.value,
            "atr":         signal.atr,
            "timeframe":   self.timeframe,
        }]
