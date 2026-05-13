import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from bot.strategy.base import Signal

if TYPE_CHECKING:
    from bot.database.db import Database

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
    def __init__(
        self,
        config: RiskConfig = RiskConfig(),
        symbol: str | None = None,
        db: "Database | None" = None,
    ) -> None:
        """Construct a RiskManager.

        When `db` and `symbol` are both provided, the circuit-breaker timestamp
        is persisted to `bot_config` and reloaded on construction. This is what
        makes the cooldown survive `init 6` reboots — without it, every restart
        silently clears the breaker.

        When either is missing, the manager falls back to in-memory state
        (backward compatible with tests and ad-hoc instances).
        """
        self.config = config
        self.symbol = symbol
        self.db = db
        self._breaker_triggered_at: Optional[datetime] = self._load_breaker_state()

    @property
    def _tag(self) -> str:
        return f"[{self.symbol}] " if self.symbol else ""

    # ── Circuit breaker state persistence ────────────────────────────────────

    def _state_key(self) -> str | None:
        """Key for the breaker timestamp in `bot_config`. None disables persistence."""
        if self.db is None or not self.symbol:
            return None
        return f"breaker_triggered_at_{self.symbol}"

    def _load_breaker_state(self) -> Optional[datetime]:
        """Restore breaker timestamp from DB, or None if absent/invalid."""
        key = self._state_key()
        if key is None:
            return None
        raw = self.db.get_config(key) if self.db else None
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning(
                "%sIgnoring invalid breaker_triggered_at='%s' in bot_config",
                self._tag, raw,
            )
            return None
        elapsed_h = (datetime.now() - dt).total_seconds() / 3600
        logger.info(
            "%sRestored circuit breaker state: triggered=%s (%.1fh ago, cooldown=%dh)",
            self._tag, raw, elapsed_h, self.config.cooldown_hours,
        )
        return dt

    def _save_breaker_state(self, dt: Optional[datetime]) -> None:
        """Persist breaker timestamp to DB. None clears the key (empty string)."""
        key = self._state_key()
        if key is None or self.db is None:
            return
        self.db.set_config(key, dt.isoformat() if dt is not None else "")

    def compute_position_size(
        self,
        capital: float,
        entry: float,
        stop_loss: float,
        risk_fraction: float | None = None,
    ) -> float:
        # Each trade risks risk_per_trade of capital. Multi-symbol concurrency is
        # enforced by 1 orchestrator per symbol (each with max_concurrent_trades=1).
        fraction = risk_fraction if risk_fraction is not None else self.config.risk_per_trade
        risk_amount = capital * fraction
        risk_per_unit = abs(entry - stop_loss)

        if risk_per_unit <= 0:
            logger.warning(
                "%sInvalid risk_per_unit=%.6f (entry=%.2f sl=%.2f) — returning 0",
                self._tag, risk_per_unit, entry, stop_loss,
            )
            return 0.0

        qty_by_risk    = risk_amount / risk_per_unit
        # Spot has no margin: notional must fit in capital. 99% leaves headroom for fees + slippage.
        qty_by_capital = (capital * 0.99) / entry
        quantity       = min(qty_by_risk, qty_by_capital)

        if quantity < qty_by_risk:
            logger.warning(
                "%sQty capped by capital: risk-based=%.5f → %.5f "
                "(risk %.2f%% × SL_dist %.2f%% would need notional > 100%% of capital)",
                self._tag, qty_by_risk, quantity,
                fraction * 100, (risk_per_unit / entry) * 100,
            )

        quantity = round(quantity, self.config.quantity_precision)

        logger.info(
            "%sPosition size: capital=%.2f fraction=%.4f entry=%.2f sl=%.2f → qty=%.*f",
            self._tag, capital, fraction, entry, stop_loss,
            self.config.quantity_precision, quantity,
        )
        return quantity

    def check_circuit_breaker(self, current_capital: float, peak_capital: float) -> bool:
        """Decide whether to halt trading this cycle.

        BOTH inputs now represent TRADING EQUITY (account_baseline + cumulative
        realized PnL), not raw exchange balance. The caller (orchestrator.step())
        is responsible for computing trading_equity before passing it here.
        Pre-May 2026 this method consumed raw exchange balance — see gotcha #31.

        Returns True if (peak - current) / peak >= max_drawdown (default 15%) AND
        the cooldown window has not yet elapsed. Side-effect: persists the triggered
        timestamp to bot_config['breaker_triggered_at_{symbol}'] via _save_breaker_state.

        See gotcha #4 for reset paths (drawdown recovery, cooldown, /reset_hwm).
        """
        # (values are TRADING EQUITY, not exchange balance — gotcha #31)
        if peak_capital <= 0:
            return False
        drawdown = (peak_capital - current_capital) / peak_capital

        if drawdown < self.config.max_drawdown:
            if self._breaker_triggered_at is not None:
                logger.info(
                    "%sCircuit breaker reset: drawdown recovered to %.2f%% (below %.2f%%)",
                    self._tag, drawdown * 100, self.config.max_drawdown * 100,
                )
                self._breaker_triggered_at = None
                self._save_breaker_state(None)
            return False

        if self._breaker_triggered_at is None:
            self._breaker_triggered_at = datetime.now()
            self._save_breaker_state(self._breaker_triggered_at)
            logger.warning(
                "%sCIRCUIT BREAKER triggered: drawdown=%.2f%% peak=%.2f current=%.2f",
                self._tag, drawdown * 100, peak_capital, current_capital,
            )
            return True

        elapsed_hours = (datetime.now() - self._breaker_triggered_at).total_seconds() / 3600
        if elapsed_hours >= self.config.cooldown_hours:
            logger.info(
                "%sCircuit breaker auto-reset: cooldown of %dh elapsed",
                self._tag, self.config.cooldown_hours,
            )
            self._breaker_triggered_at = None
            self._save_breaker_state(None)
            return False

        logger.debug(
            "%sCircuit breaker active: %.1fh / %dh cooldown elapsed",
            self._tag, elapsed_hours, self.config.cooldown_hours,
        )
        return True

    def validate_signal(self, signal: Signal) -> bool:
        """Validate signal strength and direction.

        Does NOT check open positions — duplicate and max_concurrent guards
        live in the orchestrator, which has full context.
        """
        if signal.action == "HOLD":
            logger.debug("%sSignal skipped: action=HOLD", self._tag)
            return False

        if signal.strength < self.config.min_signal_strength:
            logger.info(
                "%sSignal rejected: strength=%.4f below min=%.2f (action=%s)",
                self._tag, signal.strength, self.config.min_signal_strength, signal.action,
            )
            return False

        logger.debug(
            "%svalidate_signal: action=%s strength=%.4f → valid",
            self._tag, signal.action, signal.strength,
        )
        return True
