import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from bot.strategy.base import Signal

logger = logging.getLogger(__name__)

@dataclass
class RiskConfig:
    max_drawdown: float = 0.15
    risk_per_trade: float = 0.01
    max_concurrent_trades: int = 1
    min_signal_strength: float = 0.5
    cooldown_hours: int = 4
    quantity_precision: int = 5
    kelly_max_mult: float = 2.0
    kelly_min_mult: float = 0.25
    kelly_min_trades: int = 15
    kelly_half: bool = True


class RiskManager:
    def __init__(self, config: RiskConfig = RiskConfig()) -> None:
        self.config = config
        self._breaker_triggered_at: Optional[datetime] = None

    def compute_position_size(
        self,
        capital: float,
        entry: float,
        stop_loss: float,
        risk_fraction: float | None = None,
    ) -> float:
        # Portfolio-level risk: divide total risk budget evenly across max concurrent slots.
        # With max_concurrent_trades=1 (default) each trade risks risk_per_trade of capital.
        fraction = risk_fraction if risk_fraction is not None else self.config.risk_per_trade
        risk_amount = capital * fraction / self.config.max_concurrent_trades
        risk_per_unit = abs(entry - stop_loss)

        if risk_per_unit <= 0:
            logger.warning(
                "Invalid risk_per_unit=%.6f (entry=%.2f sl=%.2f) — returning 0",
                risk_per_unit, entry, stop_loss,
            )
            return 0.0

        quantity = risk_amount / risk_per_unit
        quantity = round(quantity, self.config.quantity_precision)

        logger.info(
            "Position size: capital=%.2f fraction=%.4f max_concurrent=%d entry=%.2f sl=%.2f → qty=%.*f",
            capital, fraction, self.config.max_concurrent_trades,
            entry, stop_loss, self.config.quantity_precision, quantity,
        )
        return quantity

    def check_circuit_breaker(self, current_capital: float, peak_capital: float) -> bool:
        if peak_capital <= 0:
            return False
        drawdown = (peak_capital - current_capital) / peak_capital

        if drawdown < self.config.max_drawdown:
            if self._breaker_triggered_at is not None:
                logger.info(
                    "Circuit breaker reset: drawdown recovered to %.2f%% (below %.2f%%)",
                    drawdown * 100, self.config.max_drawdown * 100,
                )
                self._breaker_triggered_at = None
            return False

        if self._breaker_triggered_at is None:
            self._breaker_triggered_at = datetime.now()
            logger.warning(
                "CIRCUIT BREAKER triggered: drawdown=%.2f%% peak=%.2f current=%.2f",
                drawdown * 100, peak_capital, current_capital,
            )
            return True

        elapsed_hours = (datetime.now() - self._breaker_triggered_at).total_seconds() / 3600
        if elapsed_hours >= self.config.cooldown_hours:
            logger.info(
                "Circuit breaker auto-reset: cooldown of %dh elapsed",
                self.config.cooldown_hours,
            )
            self._breaker_triggered_at = None
            return False

        logger.debug(
            "Circuit breaker active: %.1fh / %dh cooldown elapsed",
            elapsed_hours, self.config.cooldown_hours,
        )
        return True

    def validate_signal(self, signal: Signal) -> bool:
        """Validate signal strength and direction.

        Does NOT check open positions — duplicate and max_concurrent guards
        live in the orchestrator, which has full context.
        """
        if signal.action == "HOLD":
            logger.debug("Signal skipped: action=HOLD")
            return False

        if signal.strength < self.config.min_signal_strength:
            logger.info(
                "Signal rejected: strength=%.4f below min=%.2f (action=%s)",
                signal.strength, self.config.min_signal_strength, signal.action,
            )
            return False

        logger.debug(
            "validate_signal: action=%s strength=%.4f → valid",
            signal.action, signal.strength,
        )
        return True
