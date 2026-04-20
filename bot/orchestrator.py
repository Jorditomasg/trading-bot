import logging
from typing import Optional

import pandas as pd

from bot.bias.filter import Bias, BiasFilter
from bot.config_presets import get_regime_config, get_strategy_configs
from bot.constants import ExitReason, TradeAction, StrategyName
from bot.database.db import Database
from bot.regime.detector import MarketRegime, RegimeDetector
from bot.risk.manager import RiskConfig, RiskManager
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.breakout import BreakoutConfig, BreakoutStrategy
from bot.strategy.ema_crossover import EMACrossoverConfig, EMACrossoverStrategy
from bot.strategy.mean_reversion import MeanReversionConfig, MeanReversionStrategy
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
        timeframe: str = "1h",
    ) -> None:
        self.db = db
        self.symbol = symbol
        self.timeframe = timeframe
        self.risk_manager = RiskManager(risk_config or RiskConfig())
        self.regime_detector = RegimeDetector(config=get_regime_config(timeframe))
        self.bias_filter = bias_filter

        strategy_cfgs = get_strategy_configs(timeframe)
        self._strategies: dict[StrategyName, BaseStrategy] = {
            StrategyName.EMA_CROSSOVER: EMACrossoverStrategy(
                EMACrossoverConfig(**strategy_cfgs[StrategyName.EMA_CROSSOVER])
            ),
            StrategyName.MEAN_REVERSION: MeanReversionStrategy(
                MeanReversionConfig(**strategy_cfgs[StrategyName.MEAN_REVERSION])
            ),
            StrategyName.BREAKOUT: BreakoutStrategy(
                BreakoutConfig(**strategy_cfgs[StrategyName.BREAKOUT])
            ),
        }
        self._peak_capital: float = db.get_peak_capital() or 0.0

    def step(
        self,
        df: pd.DataFrame,
        current_balance: float,
        df_high: Optional[pd.DataFrame] = None,
    ) -> list[dict]:
        # Update High Water Mark (HWM)
        if current_balance > self._peak_capital:
            self._peak_capital = current_balance
            self.db.set_peak_capital(self._peak_capital)
            logger.info("Orchestrator: New High Water Mark (Peak Capital) = %.2f", self._peak_capital)

        if self.risk_manager.check_circuit_breaker(current_balance, self._peak_capital):
            logger.warning("Orchestrator: circuit breaker active — no trading this cycle")
            return []

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

        open_trades = self.db.get_open_trades()

        # Evaluate all open positions for exits (signal reversal / regime change)
        orders: list[dict] = []
        for trade in open_trades:
            exit_order = self._evaluate_open_position(trade, df, signal, regime)
            if exit_order:
                orders.append(exit_order)

        # If we produced any exit orders, return them — entry logic runs next cycle
        if orders:
            return orders

        # Entry guard: respect max_concurrent_trades
        if len(open_trades) >= self.risk_manager.config.max_concurrent_trades:
            logger.debug(
                "Orchestrator: max concurrent trades reached (%d/%d) — skipping new entry",
                len(open_trades), self.risk_manager.config.max_concurrent_trades,
            )
            return []

        # Duplicate guard: no two open trades with the same side + strategy
        if any(t["side"] == signal.action and t["strategy"] == strategy.name for t in open_trades):
            logger.info(
                "Orchestrator: duplicate %s %s already open — skipping",
                signal.action, strategy.name,
            )
            return []

        # Validate signal strength and direction
        if not self.risk_manager.validate_signal(signal, open_trades):
            logger.debug("Orchestrator: signal not valid for execution — skipping")
            return []

        current_price = float(df["close"].iloc[-1])
        quantity = self.risk_manager.compute_position_size(
            capital=current_balance,
            entry=current_price,
            stop_loss=signal.stop_loss,
            n_open_trades=len(open_trades),
        )
        if quantity <= 0:
            logger.warning("Orchestrator: computed quantity=0 — skipping")
            return []

        return [{
            "action":      TradeAction.OPEN,
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
        self, trade: dict, df: pd.DataFrame, signal: Signal, current_regime: MarketRegime
    ) -> Optional[dict]:
        current_price = float(df["close"].iloc[-1])
        side          = trade["side"]
        trade_id      = trade["id"]
        trailing_sl   = trade.get("trailing_sl")

        # Trailing SL ratcheting is handled by position_manager (every 60s).
        # Here we only handle signal-based and regime-based exits.
        reason: Optional[str] = None

        if self.risk_manager.config.enable_regime_exit:
            trade_regime = trade.get("regime")
            if trade_regime and trade_regime != current_regime.value:
                reason = ExitReason.REGIME_CHANGE
                logger.info(
                    "Regime exit: trade opened in %s, current regime=%s — closing id=%d",
                    trade_regime, current_regime.value, trade_id,
                )

        if reason is None and not self.risk_manager.config.disable_reversal_exits:
            cfg = self.risk_manager.config
            is_in_loss = (
                (side == "BUY"  and current_price < trade["entry_price"]) or
                (side == "SELL" and current_price > trade["entry_price"])
            )
            reversal_allowed = not cfg.reversal_only_if_loss or is_in_loss
            opposite = (
                (
                    (side == "BUY"  and signal.action == "SELL") or
                    (side == "SELL" and signal.action == "BUY")
                )
                and signal.strength >= cfg.reversal_strength_threshold
                and reversal_allowed
            )
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
