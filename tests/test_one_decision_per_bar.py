"""`run_cycle` makes at most one entry decision per closed bar.

The scheduler ticks hourly while the traded timeframe is 4h. With
`closed_only=True` klines, four consecutive cycles see byte-identical data, so
without this guard a stop-out at 21:30 could be re-entered at 22:00 into the
same setup at an arbitrary intrabar price — churn `BacktestEngine`, which
decides once per bar, never simulates.

Exits are unaffected: `position_manager()` runs on its own 60s loop.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import main as main_module
from bot.orchestrator import StrategyOrchestrator


def _df(last_open_time: str, n: int = 100) -> pd.DataFrame:
    times = pd.date_range(end=pd.Timestamp(last_open_time), periods=n, freq="4h")
    return pd.DataFrame({
        "open_time": times,
        "open": [50000.0] * n, "high": [50100.0] * n,
        "low": [49900.0] * n, "close": [50050.0] * n,
        "volume": [100.0] * n,
    })


@pytest.fixture(autouse=True)
def _clear_bar_cache():
    main_module._last_entry_bar.clear()
    yield
    main_module._last_entry_bar.clear()


def _client_for(df: pd.DataFrame) -> MagicMock:
    client = MagicMock()
    weekly = pd.DataFrame({"close": [50000.0] * 60})
    client.get_klines.side_effect = lambda *a, **k: (
        weekly if a[1] == "1w" else df
    )
    client.get_balance.return_value = 10000.0
    return client


def test_second_cycle_on_the_same_bar_is_skipped(tmp_db):
    orch = StrategyOrchestrator(db=tmp_db, symbol="ETHUSDT")
    client = _client_for(_df("2026-07-30 20:00"))

    with patch.object(main_module, "_build_client", return_value=client):
        main_module.run_cycle(orch, tmp_db, dry_run=True)
        first_pass_calls = client.get_klines.call_count
        main_module.run_cycle(orch, tmp_db, dry_run=True)

    # The skipped cycle costs exactly one kline fetch (the primary), then bails
    # before the bias and weekly fetches.
    assert client.get_klines.call_count == first_pass_calls + 1


def test_a_new_closed_bar_re_enables_the_entry_pass(tmp_db):
    orch = StrategyOrchestrator(db=tmp_db, symbol="ETHUSDT")

    with patch.object(main_module, "_build_client",
                      return_value=_client_for(_df("2026-07-30 20:00"))):
        main_module.run_cycle(orch, tmp_db, dry_run=True)

    client = _client_for(_df("2026-07-31 00:00"))
    with patch.object(main_module, "_build_client", return_value=client):
        main_module.run_cycle(orch, tmp_db, dry_run=True)

    assert client.get_klines.call_count > 1  # ran the full pass again


def test_the_guard_is_per_symbol(tmp_db):
    """One symbol's bar must not suppress another's cycle."""
    eth = StrategyOrchestrator(db=tmp_db, symbol="ETHUSDT")
    btc = StrategyOrchestrator(db=tmp_db, symbol="BTCUSDT")
    df = _df("2026-07-30 20:00")

    with patch.object(main_module, "_build_client", return_value=_client_for(df)):
        main_module.run_cycle(eth, tmp_db, dry_run=True)

    client = _client_for(df)
    with patch.object(main_module, "_build_client", return_value=client):
        main_module.run_cycle(btc, tmp_db, dry_run=True)

    assert client.get_klines.call_count > 1
    assert set(main_module._last_entry_bar) == {"ETHUSDT", "BTCUSDT"}


def test_frame_without_open_time_does_not_break_the_cycle(tmp_db):
    """Synthetic frames opt out of the guard instead of raising."""
    orch = StrategyOrchestrator(db=tmp_db, symbol="ETHUSDT")
    df = _df("2026-07-30 20:00").drop(columns=["open_time"])
    client = _client_for(df)

    with patch.object(main_module, "_build_client", return_value=client):
        main_module.run_cycle(orch, tmp_db, dry_run=True)

    assert "ETHUSDT" not in main_module._last_entry_bar
    intervals = [c.args[1] for c in client.get_klines.call_args_list]
    assert "1w" in intervals  # full pass still ran
