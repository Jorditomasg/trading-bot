"""Regression test for the circuit-breaker adaptor path in run_cycle.

Guards against the bug (gotcha #31) where run_cycle re-checked the circuit
breaker using the RAW exchange balance instead of trading equity. On testnet
the raw USDT balance routinely diverges from trading equity, which false-fired
the breaker every cycle and corrupted the safety-net state.

The contract: orchestrator.step() is the single place that evaluates the
breaker (against trading equity). run_cycle must reuse that decision when
informing the adaptor — never re-evaluate from raw balance.
"""

from unittest.mock import MagicMock, patch

import pytest

import main as main_module
from bot.adaptive.adaptor import ParameterAdaptor
from bot.orchestrator import StrategyOrchestrator
from tests.conftest import uptrend


@pytest.fixture
def db(tmp_path):
    from bot.database.db import Database
    return Database(str(tmp_path / "test.db"))


def test_adaptor_not_false_triggered_by_low_raw_balance(db):
    """Healthy trading equity + depressed raw balance must NOT fire the breaker.

    trading_equity = baseline (18000) + closed_pnl (0) = 18000 = peak → 0% DD.
    Raw exchange balance is 5000 → a raw-balance check would see 72% DD and
    falsely trip the breaker. The fixed code reuses step()'s (healthy) state.
    """
    db.set_account_baseline(18000.0)
    db.set_peak_capital(18000.0)

    orch = StrategyOrchestrator(db=db, symbol="BTCUSDT", timeframe="4h")
    adaptor = ParameterAdaptor(db, orch.risk_manager)

    df = uptrend(300, freq="4h")
    mock_client = MagicMock()
    mock_client.get_klines.return_value = df
    mock_client.get_balance.return_value = 5000.0  # raw balance << peak

    captured = {}
    real_maybe_adapt = adaptor.maybe_adapt

    def spy(circuit_breaker_active=False):
        captured["cb"] = circuit_breaker_active
        return real_maybe_adapt(circuit_breaker_active=circuit_breaker_active)

    with patch.object(main_module, "_build_client", return_value=mock_client):
        with patch.object(adaptor, "maybe_adapt", side_effect=spy):
            main_module.run_cycle(orch, db, dry_run=True, adaptor=adaptor, n_symbols=1)

    # The adaptor must see a HEALTHY (not tripped) breaker.
    assert captured.get("cb") is False, (
        "adaptor saw circuit_breaker_active=True — run_cycle is re-checking the "
        "breaker against raw exchange balance instead of reusing step()'s decision"
    )

    # And the breaker timestamp must NOT have been persisted by the cycle.
    cfg = db.get_runtime_config()
    assert not cfg.get("breaker_triggered_at_BTCUSDT"), (
        "breaker_triggered_at_BTCUSDT was written despite healthy trading equity"
    )
