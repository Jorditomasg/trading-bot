import logging
from dataclasses import dataclass
from typing import Optional

from bot.strategy.base import Signal

logger = logging.getLogger(__name__)

QUANTITY_PRECISION = 5  # BTC decimal places on Binance


@dataclass
class RiskConfig:
    max_drawdown: float = 0.15
    risk_per_trade: float = 0.01
    max_concurrent_trades: int = 1
    min_signal_strength: float = 0.4


class RiskManager:
    def __init__(self, config: RiskConfig = RiskConfig()) -> None:
        self.config = config

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
        quantity = round(quantity, QUANTITY_PRECISION)

        logger.info(
            "Position size: capital=%.2f risk=%.2f entry=%.2f sl=%.2f → qty=%.5f BTC",
            capital, risk_amount, entry, stop_loss, quantity,
        )
        return quantity

    def check_circuit_breaker(self, current_capital: float, peak_capital: float) -> bool:
        if peak_capital <= 0:
            return False
        drawdown = (peak_capital - current_capital) / peak_capital
        triggered = drawdown >= self.config.max_drawdown
        if triggered:
            logger.warning(
                "CIRCUIT BREAKER triggered: drawdown=%.2f%% (peak=%.2f current=%.2f)",
                drawdown * 100, peak_capital, current_capital,
            )
        return triggered

    def validate_signal(self, signal: Signal, open_position: Optional[dict]) -> bool:
        if signal.action == "HOLD":
            return False

        if signal.strength < self.config.min_signal_strength:
            logger.info(
                "Signal rejected: strength %.2f < min %.2f",
                signal.strength, self.config.min_signal_strength,
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
