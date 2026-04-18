import logging
from dataclasses import dataclass

from bot.database.db import Database

logger = logging.getLogger(__name__)


@dataclass
class AdaptorConfig:
    window_size: int = 20              # últimas N trades por estrategia
    min_trades: int = 10               # mínimo de trades para adaptar
    tighten_threshold: float = 0.35   # win_rate < 35% → endurecer
    relax_threshold: float = 0.65     # win_rate > 65% → relajar


class ParameterAdaptor:
    def __init__(
        self,
        db: Database,
        mean_reversion_strategy,   # MeanReversionStrategy instance
        breakout_strategy,         # BreakoutStrategy instance
        risk_manager,              # RiskManager instance
        config: AdaptorConfig = AdaptorConfig(),
    ) -> None:
        self.db = db
        self.config = config
        # Targets: (strategy_name, param_name, getter, setter, step, min_val, max_val)
        self._targets = [
            (
                "MEAN_REVERSION", "rsi_oversold",
                lambda: mean_reversion_strategy.config.rsi_oversold,
                lambda v: setattr(mean_reversion_strategy.config, "rsi_oversold", v),
                1.0, 28.0, 40.0,
            ),
            (
                "MEAN_REVERSION", "rsi_overbought",
                lambda: mean_reversion_strategy.config.rsi_overbought,
                lambda v: setattr(mean_reversion_strategy.config, "rsi_overbought", v),
                1.0, 60.0, 72.0,
            ),
            (
                "BREAKOUT", "volume_multiplier",
                lambda: breakout_strategy.config.volume_multiplier,
                lambda v: setattr(breakout_strategy.config, "volume_multiplier", v),
                0.05, 1.0, 1.5,
            ),
            (
                "RISK", "min_signal_strength",
                lambda: risk_manager.config.min_signal_strength,
                lambda v: setattr(risk_manager.config, "min_signal_strength", v),
                0.02, 0.3, 0.55,
            ),
        ]

    def _compute_win_rate(self, strategy: str) -> float | None:
        trades = self.db.get_all_trades()
        recent = [
            t for t in trades
            if t.get("exit_price") is not None and t.get("strategy") == strategy
        ][-self.config.window_size:]
        if len(recent) < self.config.min_trades:
            return None
        wins = sum(1 for t in recent if (t.get("pnl") or 0) > 0)
        return wins / len(recent)

    def _adapt_param(self, strategy, param_name, getter, setter, step, min_val, max_val, direction):
        old = getter()
        if direction == "tighten":
            new = max(min_val, round(old - step, 4))
        else:
            new = min(max_val, round(old + step, 4))
        if new == old:
            logger.debug("Adaptor: %s.%s already at bound %.4f", strategy, param_name, old)
            return
        setter(new)
        reason = f"win_rate {'<' if direction == 'tighten' else '>'} threshold"
        self.db.insert_adaptive_param(strategy, param_name, old, new, reason)
        logger.info("Adapted %s.%s: %.4f → %.4f (%s)", strategy, param_name, old, new, reason)

    def maybe_adapt(self, circuit_breaker_active: bool = False) -> None:
        if circuit_breaker_active:
            logger.debug("Adaptor skipped: circuit breaker active")
            return

        # Group targets by strategy name to compute win_rate once per strategy
        strategies_seen = {}
        for (strategy, param_name, getter, setter, step, min_val, max_val) in self._targets:
            if strategy not in strategies_seen:
                strategies_seen[strategy] = self._compute_win_rate(strategy)

        for (strategy, param_name, getter, setter, step, min_val, max_val) in self._targets:
            wr = strategies_seen.get(strategy)
            if wr is None:
                logger.debug("Adaptor: %s not enough trades, skipping", strategy)
                continue
            if wr < self.config.tighten_threshold:
                self._adapt_param(strategy, param_name, getter, setter, step, min_val, max_val, "tighten")
            elif wr > self.config.relax_threshold:
                self._adapt_param(strategy, param_name, getter, setter, step, min_val, max_val, "relax")
            else:
                logger.debug("Adaptor: %s.%s win_rate=%.2f in neutral zone", strategy, param_name, wr)
