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
    min_signal_strength: float = 0.4
    cooldown_hours: int = 4
    trail_atr_mult: float = 1.5
    trail_activation_mult: float = 1.0
    quantity_precision: int = 5   # overridden at startup via exchangeInfo LOT_SIZE
    enable_regime_exit: bool = False


class RiskManager:
    def __init__(self, config: RiskConfig = RiskConfig()) -> None:
        self.config = config
        self._breaker_triggered_at: Optional[datetime] = None

    def compute_position_size(
        self, capital: float, entry: float, stop_loss: float
    ) -> float:
        risk_amount = capital * self.config.risk_per_trade
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
            "Position size: capital=%.2f risk=%.2f entry=%.2f sl=%.2f → qty=%.5f BTC",
            capital, risk_amount, entry, stop_loss, quantity,
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

    def validate_signal(self, signal: Signal, open_position: Optional[dict]) -> bool:
        if signal.action == "HOLD":
            logger.debug("Signal skipped: action=HOLD")
            return False

        if signal.strength < self.config.min_signal_strength:
            logger.info(
                "Signal rejected: strength=%.4f below min=%.2f (action=%s)",
                signal.strength, self.config.min_signal_strength, signal.action,
            )
            return False

        if open_position is not None:
            # Allow opposite-direction signal (to close), reject same direction
            existing_side = open_position.get("side", "")
            if signal.action == existing_side:
                logger.info(
                    "Signal rejected: already have an open %s position", existing_side
                )
                return False

        return True
