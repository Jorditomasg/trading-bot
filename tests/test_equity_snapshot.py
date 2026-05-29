"""Equity snapshots are recorded once per full cycle, not once per symbol.

Guards gotcha #39: the account USDT pool is shared across symbols, so a
per-symbol snapshot over-sampled the equity curve in multi-symbol mode
(spurious zero-return points inflating Sharpe + tripled storage).
"""

from unittest.mock import MagicMock, patch

import pytest

import main as main_module
from bot.adaptive.adaptor import ParameterAdaptor
from bot.database.db import Database
from bot.orchestrator import StrategyOrchestrator
from tests.conftest import uptrend


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def test_record_equity_snapshot_inserts_one_point(db):
    db.set_account_baseline(10_000.0)
    db.set_peak_capital(10_000.0)
    client = MagicMock()
    client.get_balance.return_value = 10_500.0

    main_module.record_equity_snapshot(db, client)

    curve = db.get_equity_curve()
    assert len(curve) == 1
    assert curve[0]["balance"] == 10_500.0


def test_balance_fetch_failure_skips_snapshot(db):
    client = MagicMock()
    client.get_balance.side_effect = RuntimeError("network down")

    main_module.record_equity_snapshot(db, client)  # must not raise

    assert db.get_equity_curve() == []


def test_run_cycle_does_not_snapshot(db):
    """run_cycle must no longer write equity points — that moved to run_all_cycles."""
    db.set_account_baseline(18_000.0)
    db.set_peak_capital(18_000.0)
    orch = StrategyOrchestrator(db=db, symbol="BTCUSDT", timeframe="4h")
    adaptor = ParameterAdaptor(db, orch.risk_manager)

    client = MagicMock()
    client.get_klines.return_value = uptrend(300, freq="4h")
    client.get_balance.return_value = 18_000.0

    with patch.object(main_module, "_build_client", return_value=client):
        main_module.run_cycle(orch, db, dry_run=True, adaptor=adaptor, n_symbols=2)

    assert db.get_equity_curve() == [], "run_cycle still inserts a per-symbol snapshot"
