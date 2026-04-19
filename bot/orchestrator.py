import logging
from typing import Optional

import pandas as pd

from bot.bias.filter import Bias, BiasFilter
from bot.constants import ExitReason, TradeAction, StrategyName
from bot.database.db import Database
from bot.regime.detector import MarketRegime, RegimeDetector
from bot.risk.manager import RiskConfig, RiskManager
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.breakout import BreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverStrategy
from bot.strategy.mean_reversion import MeanReversionStrategy
from bot.strategy.signal_factory import hold_signal

logger = logging.getLogger(__name__)

REGIME_STRATEGY_MAP: dict[MarketRegime, StrategyName] = {
    MarketRegime.TRENDING: StrategyName.EMA_CROSSOVER,
    MarketRegime.RANGING: StrategyName.MEAN_REVERSION,
    MarketRegime.VOLATILE: StrategyName.BREAKOUT,
}

WINRATE_LOOKBACK = 20
WINRATE_MIN_THRESHOLD = 0.40


class StrategyOrchestrator:
    def __init__(
        self,
        db: Database,
        symbol: str,
        risk_config: Optional[RiskConfig] = None,
        bias_filter: Optional[BiasFilter] = None,
    ) -> None:
        self.db = db
        self.symbol = symbol
        self.risk_manager = RiskManager(risk_config or RiskConfig())
        self.regime_detector = RegimeDetector()
        self.bias_filter = bias_filter

        self._strategies: dict[StrategyName, BaseStrategy] = {
            StrategyName.EMA_CROSSOVER: EMACrossoverStrategy(),
            StrategyName.MEAN_REVERSION: MeanReversionStrategy(),
            StrategyName.BREAKOUT: BreakoutStrategy(),
        }
        self._peak_capital: float = 0.0

    def step(
        self,
        df: pd.DataFrame,
        current_balance: float,
        df_high: Optional[pd.DataFrame] = None,
    ) -> Optional[dict]:
        if self._peak_capital < current_balance:
            self._peak_capital = current_balance

        if self.risk_manager.check_circuit_breaker(current_balance, self._peak_capital):
            logger.warning("Orchestrator: circuit breaker active — no trading this cycle")
            return None

        regime = self.regime_detector.detect(df)
        logger.info("Orchestrator: regime=%s balance=%.2f", regime.value, current_balance)

        strategy = self._select_strategy(regime)
        signal: Signal = strategy.generate_signal(df)

        bias: Optional[Bias] = None
        if self.bias_filter is not None:
            bias = self.bias_filter.get_bias(df_high)
            if not self.bias_filter.allows_signal(signal, bias):
                logger.info(
                    "BiasFilter blocked signal action=%s bias=%s — holding",
                    signal.action, bias.value,
                )
                signal = hold_signal(atr=signal.atr)

        self.db.insert_signal(
            symbol=self.symbol,
            strategy=strategy.name,
            regime=regime.value,
            action=signal.action,
            strength=signal.strength,
            bias=bias.value if bias is not None else None,
        )
        logger.info(
            "Orchestrator: signal action=%s strength=%.2f strategy=%s bias=%s",
            signal.action, signal.strength, strategy.name,
            bias.value if bias is not None else "N/A",
        )

        open_trade = self.db.get_open_trade()

        # Check stop loss / take profit on open position
        if open_trade is not None:
            exit_order = self._evaluate_open_position(open_trade, df, signal)
            if exit_order:
                return exit_order

        # Validate and open a new position
        if not self.risk_manager.validate_signal(signal, open_trade):
            logger.debug("Orchestrator: signal not valid for execution — skipping")
            return None

        if open_trade is not None:
            logger.debug("Orchestrator: position already open — skipping new entry")
            return None

        current_price = float(df["close"].iloc[-1])
        quantity = self.risk_manager.compute_position_size(
            capital=current_balance,
            entry=current_price,
            stop_loss=signal.stop_loss,
        )
        if quantity <= 0:
            logger.warning("Orchestrator: computed quantity=0 — skipping")
            return None

        return {
            "action": TradeAction.OPEN,
            "side": signal.action,
            "quantity": quantity,
            "entry_price": current_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "strategy": strategy.name,
            "regime": regime.value,
            "atr": signal.atr,
        }

    def _select_strategy(self, regime: MarketRegime) -> BaseStrategy:
        default_name = REGIME_STRATEGY_MAP[regime]
        performance = self.db.get_performance_by_strategy()

        regime_strategy_perf = {
            row["strategy"]: row for row in performance
            if row["total_trades"] >= WINRATE_LOOKBACK
        }

        if default_name in regime_strategy_perf:
            wr = regime_strategy_perf[default_name]["win_rate"] / 100
            if wr < WINRATE_MIN_THRESHOLD:
                logger.warning(
                    "Strategy %s win_rate=%.2f%% below threshold — searching for alternative",
                    default_name, wr * 100,
                )
                best_name, best_wr = default_name, wr
                for name, row in regime_strategy_perf.items():
                    candidate_wr = row["win_rate"] / 100
                    if candidate_wr > best_wr:
                        best_wr = candidate_wr
                        best_name = name
                if best_name != default_name:
                    logger.info(
                        "Switching from %s to %s (winrate %.2f%% vs %.2f%%)",
                        default_name, best_name, wr * 100, best_wr * 100,
                    )
                    return self._strategies[best_name]

        return self._strategies[default_name]

    def _evaluate_open_position(
        self, trade: dict, df: pd.DataFrame, signal: Signal
    ) -> Optional[dict]:
        current_price = float(df["close"].iloc[-1])
        side          = trade["side"]
        trade_id      = trade["id"]
        trailing_sl   = trade.get("trailing_sl")

        # Trailing SL ratcheting is handled by position_manager (every 60s).
        # Here we only handle signal-based exits.
        reason: Optional[str] = None
        opposite = (
            (side == "BUY"  and signal.action == "SELL") or
            (side == "SELL" and signal.action == "BUY")
        ) and signal.strength >= 0.5
        if opposite:
            reason = ExitReason.SIGNAL_REVERSAL

        if reason is None:
            return None

        close_side = "SELL" if side == "BUY" else "BUY"
        logger.info(
            "Orchestrator: closing trade id=%d reason=%s price=%.2f trailing_sl=%s",
            trade_id, reason, current_price,
            f"{trailing_sl:.2f}" if trailing_sl else "N/A",
        )
        return {
            "action":      TradeAction.CLOSE,
            "side":        close_side,
            "trade_id":    trade_id,
            "quantity":    trade["quantity"],
            "exit_price":  current_price,
            "exit_reason": reason,
        }
