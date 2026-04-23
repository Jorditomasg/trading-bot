"""Scenario runner — compares multiple BacktestEngine configurations in one call.

Each Scenario is a named configuration tuple. ScenarioRunner.run_all() executes
all scenarios sequentially and returns a list of ScenarioResult with normalized
metrics for easy side-by-side comparison.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from bot.backtest.engine import EXIT_LIQUIDATED, BacktestConfig, BacktestEngine

logger = logging.getLogger(__name__)

# Bias timeframe mapping — mirrors live bot and backtest_runner.py
_BIAS_TF: dict[str, str] = {
    "1h": "4h",
    "2h": "4h",
    "4h": "1d",
    "8h": "1d",
    "1d": "1w",
}


@dataclass
class Scenario:
    name: str
    timeframe: str
    leverage: float
    momentum_filter: bool


@dataclass
class ScenarioResult:
    scenario: Scenario
    annual_return_pct: float    # e.g. 0.32 = 32%
    sharpe_ratio: float
    max_drawdown_pct: float     # e.g. 12.5 = 12.5%
    profit_factor: float
    total_trades: int
    liquidations: int
    final_capital: float
    equity_curve: list[dict]


SCENARIOS: list[Scenario] = [
    Scenario("Baseline 4h",         "4h", 1.0,  False),
    Scenario("1h spot",             "1h", 1.0,  False),
    Scenario("4h + momentum",       "4h", 1.0,  True),
    Scenario("1h + momentum",       "1h", 1.0,  True),
    Scenario("1h + momentum + 2×",  "1h", 2.0,  True),
    Scenario("1h + momentum + 3×",  "1h", 3.0,  True),
    Scenario("1h + momentum + 5×",  "1h", 5.0,  True),
    Scenario("1h + momentum + 10×", "1h", 10.0, True),
]


def compute_annual_return(
    initial_capital: float,
    final_capital: float,
    lookback_days: int,
) -> float:
    """Compound annual return as a fraction (0.32 = 32%)."""
    if initial_capital <= 0 or lookback_days <= 0:
        return 0.0
    ratio = final_capital / initial_capital
    if ratio <= 0:
        return -1.0
    return ratio ** (365.0 / lookback_days) - 1.0


class ScenarioRunner:
    """Executes a list of Scenario configs against pre-fetched OHLCV DataFrames.

    DataFrames are passed at construction time so the caller controls data
    fetching (cache, network, tests) and the runner stays pure.

    Args:
        df_1h:         Primary 1h OHLCV data (used for 1h scenarios).
        df_4h:         4h data — bias TF for 1h scenarios; primary for 4h scenarios.
        df_1d:         1d data — bias TF for 4h scenarios.
        df_weekly:     Weekly data for momentum filter (may be None to disable).
        lookback_days: Number of calendar days covered — used for annual return.
        risk_per_trade: Fraction of capital risked per trade (e.g. 0.02).
    """

    def __init__(
        self,
        df_1h: pd.DataFrame,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        df_weekly: pd.DataFrame | None,
        lookback_days: int,
        risk_per_trade: float,
    ) -> None:
        self._df_1h         = df_1h
        self._df_4h         = df_4h
        self._df_1d         = df_1d
        self._df_weekly     = df_weekly
        self._lookback_days = lookback_days
        self._risk          = risk_per_trade

    def run_all(
        self,
        scenarios: list[Scenario] | None = None,
        symbol: str = "BTCUSDT",
    ) -> list[ScenarioResult]:
        """Run all scenarios sequentially and return results in the same order."""
        if scenarios is None:
            scenarios = SCENARIOS
        results: list[ScenarioResult] = []
        for scenario in scenarios:
            logger.info("ScenarioRunner: running '%s'", scenario.name)
            result = self._run_one(scenario, symbol)
            results.append(result)
        return results

    # ── Private ───────────────────────────────────────────────────────────────

    def _run_one(self, scenario: Scenario, symbol: str) -> ScenarioResult:
        """Run a single scenario and return its ScenarioResult."""
        tf         = scenario.timeframe
        df_primary = self._df_1h if tf == "1h" else self._df_4h
        df_bias    = self._df_4h if tf == "1h" else self._df_1d

        config = BacktestConfig(
            initial_capital         = 10_000.0,
            risk_per_trade          = self._risk,
            timeframe               = tf,
            cost_per_side_pct       = 0.0015,
            leverage                = scenario.leverage,
            funding_rate_per_8h     = 0.0001,
            momentum_filter_enabled = scenario.momentum_filter,
            momentum_sma_period     = 20,
            momentum_neutral_band   = 0.05,
            simulate_trailing       = True,
            disable_reversal_exits  = True,
            long_only               = False,
        )
        engine = BacktestEngine(config)
        bt     = engine.run(
            df        = df_primary,
            df_4h     = df_bias,
            symbol    = symbol,
            df_weekly = self._df_weekly if scenario.momentum_filter else None,
        )
        summary = engine.summary(bt)

        liquidations = sum(
            1 for t in bt.trades if t.get("exit_reason") == EXIT_LIQUIDATED
        )

        annual_return = compute_annual_return(
            bt.initial_capital, bt.final_capital, self._lookback_days
        )

        return ScenarioResult(
            scenario          = scenario,
            annual_return_pct = annual_return,
            sharpe_ratio      = summary["sharpe_ratio"],
            max_drawdown_pct  = summary["max_drawdown_pct"],
            profit_factor     = summary["profit_factor"],
            total_trades      = summary["total_trades"],
            liquidations      = liquidations,
            final_capital     = bt.final_capital,
            equity_curve      = bt.equity_curve,
        )
