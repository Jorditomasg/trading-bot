"""Scheduler-thread jobs reuse one BinanceClient per mode/credentials.

Building one costs two python-binance Client()s, each doing an unretried
ping() in its constructor: 9,273 rebuilds in 7 days (one per 60 s
position_manager tick) — and when a ping timed out, that whole SL/TP tick was
skipped (2026-09-29 08:00 `Read timed out` on api.binance.com).
"""
from unittest.mock import MagicMock, patch

import main as main_module


def _db(mode="TESTNET", creds=None):
    db = MagicMock()
    db.get_active_mode.return_value = mode
    db.get_mainnet_credentials.return_value = creds
    return db


def setup_function(_):
    main_module._client_cache.clear()


def test_same_mode_reuses_the_client():
    db = _db()
    with patch.object(main_module, "_build_client", side_effect=lambda d: object()) as build:
        a = main_module._scheduler_client(db)
        b = main_module._scheduler_client(db)
    assert a is b
    assert build.call_count == 1


def test_mode_change_rebuilds():
    db = _db()
    with patch.object(main_module, "_build_client", side_effect=lambda d: object()) as build:
        a = main_module._scheduler_client(db)
        db.get_active_mode.return_value = "MAINNET"
        db.get_mainnet_credentials.return_value = ("k", "s")
        b = main_module._scheduler_client(db)
    assert a is not b
    assert build.call_count == 2


def test_credential_change_rebuilds():
    db = _db("MAINNET", ("k1", "s1"))
    with patch.object(main_module, "_build_client", side_effect=lambda d: object()) as build:
        main_module._scheduler_client(db)
        db.get_mainnet_credentials.return_value = ("k2", "s2")
        main_module._scheduler_client(db)
    assert build.call_count == 2


def test_failed_build_is_not_cached():
    db = _db()
    calls = {"n": 0}

    def flaky(_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("ping timed out")
        return object()

    with patch.object(main_module, "_build_client", side_effect=flaky):
        try:
            main_module._scheduler_client(db)
        except TimeoutError:
            pass
        assert main_module._scheduler_client(db) is not None
