"""Live side of the chandelier exit (gotcha #50) — parity with the backtest ratchet.

Live ratchets the stop once per CLOSED primary bar (run_cycle), persists it in
`trades.stop_loss` / `trades.peak_price`, and position_manager keeps checking
1m wicks against that stop every 60s — the same rule the engines apply.
"""
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from bot.constants import ExitReason
from bot.indicators import atr as compute_atr
from bot.database.db import Database
from main import _check_intra_bar_exit, _seed_optimized_defaults, _update_trailing_stop


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "t.db"))


def _open(db: Database, entry=100.0, sl=90.0, tp=0.0, entry_time=datetime(2026, 10, 1, 8, 0, 30)) -> int:
    return db.insert_trade(
        symbol="BTCUSDT", side="BUY", strategy="EMA_CROSSOVER", regime="TRENDING",
        entry_price=entry, quantity=1.0, stop_loss=sl, take_profit=tp,
        atr=5.0, timeframe="4h", entry_time=entry_time,
    )


def _closed_4h(start="2026-10-01 00:00", highs=(101, 102, 130, 125), atr_flat=5.0) -> pd.DataFrame:
    """Closed 4h bars; with TR == atr_flat on every bar the SMA-ATR(14) is atr_flat."""
    pre = 20
    t = pd.date_range(pd.Timestamp(start, tz="UTC") - pd.Timedelta(hours=4 * pre), periods=pre + len(highs), freq="4h")
    hs = [100.0] * pre + [float(h) for h in highs]
    return pd.DataFrame({"open_time": t, "open": [h - 1 for h in hs], "high": hs,
                         "low": [h - atr_flat for h in hs], "close": [h - 1 for h in hs], "volume": [1.0] * len(hs)})


def test_update_trade_stop_persists_stop_and_peak(db):
    tid = _open(db)
    db.update_trade_stop(tid, 101.5, 130.0)
    t = db.get_trade(tid)
    assert t["stop_loss"] == 101.5 and t["peak_price"] == 130.0


def test_ratchet_uses_only_bars_after_entry(db):
    tid = _open(db, entry_time=datetime(2026, 10, 1, 8, 0, 30))
    df = _closed_4h(highs=(140, 101, 130, 125))   # the 140 bar (00:00) is BEFORE the entry bar (08:00 boundary)
    _update_trailing_stop(db, "BTCUSDT", df, trail_mult=2.0)
    t = db.get_trade(tid)
    assert t["peak_price"] == 130.0            # not 140: that bar closed before the entry
    atr_now = float(compute_atr(df[["open", "high", "low", "close", "volume"]], 14).iloc[-1])
    assert t["stop_loss"] == pytest.approx(max(90.0, 130.0 - 2.0 * atr_now))
    assert t["stop_loss"] > 90.0


def test_ratchet_never_lowers_the_stop(db):
    tid = _open(db, sl=120.0)
    _update_trailing_stop(db, "BTCUSDT", _closed_4h(), trail_mult=4.0)
    assert db.get_trade(tid)["stop_loss"] == 120.0


def test_ratchet_disabled(db):
    tid = _open(db)
    _update_trailing_stop(db, "BTCUSDT", _closed_4h(), trail_mult=0.0)
    assert db.get_trade(tid)["stop_loss"] == 90.0


def test_ratchet_is_idempotent_on_the_same_bar(db):
    tid = _open(db)
    df = _closed_4h()
    _update_trailing_stop(db, "BTCUSDT", df, trail_mult=4.0)
    first = db.get_trade(tid)["stop_loss"]
    _update_trailing_stop(db, "BTCUSDT", df, trail_mult=4.0)
    assert db.get_trade(tid)["stop_loss"] == first


def test_ratchet_ignores_other_symbols(db):
    tid = _open(db)
    _update_trailing_stop(db, "ETHUSDT", _closed_4h(), trail_mult=4.0)
    assert db.get_trade(tid)["stop_loss"] == 90.0


def _client_with_1m(high, low, close):
    c = MagicMock()
    c.get_klines.return_value = pd.DataFrame({"open": [close], "high": [high], "low": [low], "close": [close], "volume": [1.0]})
    return c


def test_intra_bar_exit_ignores_disabled_take_profit():
    trade = {"symbol": "BTCUSDT", "side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0}
    assert _check_intra_bar_exit(_client_with_1m(500.0, 95.0, 400.0), trade) is None


def test_intra_bar_exit_still_stops_out_without_take_profit():
    trade = {"symbol": "BTCUSDT", "side": "BUY", "stop_loss": 90.0, "take_profit": 0.0, "entry_price": 100.0}
    assert _check_intra_bar_exit(_client_with_1m(95.0, 85.0, 88.0), trade) == (ExitReason.STOP_LOSS, 88.0)


def test_seed_writes_trailing_exit_config(db):
    _seed_optimized_defaults(db)
    cfg = db.get_runtime_config()
    assert cfg["ema_stop_mult"] == "2.0"
    assert cfg["ema_tp_mult"] == "0.0"
    assert cfg["ema_trail_mult"] == "4.0"
