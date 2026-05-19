"""Shared pytest fixtures and OHLCV factories for the test suite.

Previously every test module defined its own `_make_ohlcv` / `_uptrend` /
`_flat` helpers. Centralising them here:
- Eliminates duplication across 46 test files.
- Gives parity tests a canonical synthetic dataset.
- Lets fixtures be reused across the runtime parity suite.

Existing tests can keep their local helpers (no forced migration); new tests
should pull from this module.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd
import pytest

from bot.database.db import Database

# ─────────────────────────────────────────────────────────────────────────────
# OHLCV synthetic-data factories
# ─────────────────────────────────────────────────────────────────────────────

# Default frequency for synthetic bars. Most tests don't depend on the exact
# timestamps — only on ordering and bar count. 1h is the canonical assumption
# of the test suite's pre-existing helpers.
_DEFAULT_FREQ  = "1h"
_DEFAULT_START = "2024-01-01"


def make_ohlcv(
    closes: Iterable[float],
    *,
    high_mult: float = 1.005,
    low_mult:  float = 0.995,
    volume:    float = 1_000_000.0,
    freq:      str   = _DEFAULT_FREQ,
    start:     str   = _DEFAULT_START,
) -> pd.DataFrame:
    """Construct an OHLCV DataFrame with `open_time` column.

    The first bar's open equals its close (no gap). Highs and lows are derived
    multiplicatively so wick width scales with price — realistic-enough for
    indicator math without modelling actual market microstructure.
    """
    closes_list = list(closes)
    n = len(closes_list)
    opens = [closes_list[0]] + closes_list[:-1]
    highs = [c * high_mult for c in closes_list]
    lows  = [c * low_mult  for c in closes_list]
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open_time": times,
        "open":      opens,
        "high":      highs,
        "low":       lows,
        "close":     closes_list,
        "volume":    [volume] * n,
    })


def uptrend(n: int = 300, start_price: float = 40_000.0, end_price: float = 50_000.0,
            **kwargs) -> pd.DataFrame:
    """Steady linear rise across `n` bars — should generate EMA crossover BUYs."""
    if n < 2:
        return make_ohlcv([start_price] * max(n, 1), **kwargs)
    step = (end_price - start_price) / (n - 1)
    closes = [start_price + i * step for i in range(n)]
    return make_ohlcv(closes, **kwargs)


def downtrend(n: int = 300, start_price: float = 50_000.0, end_price: float = 40_000.0,
              **kwargs) -> pd.DataFrame:
    """Steady linear decline — should generate EMA crossover SELLs (when long_only=False)."""
    return uptrend(n=n, start_price=start_price, end_price=end_price, **kwargs)


def flat(n: int = 300, price: float = 45_000.0, **kwargs) -> pd.DataFrame:
    """Flat price — most signals should be HOLD."""
    return make_ohlcv([price] * n, **kwargs)


def choppy(n: int = 300, mid: float = 45_000.0, swing: float = 500.0,
           period: int = 10, **kwargs) -> pd.DataFrame:
    """Sinusoidal chop around `mid` ± `swing` — exercises regime transitions."""
    import math
    closes = [mid + swing * math.sin(2 * math.pi * i / period) for i in range(n)]
    return make_ohlcv(closes, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Pytest fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path) -> Database:
    """A fresh, empty SQLite Database isolated per test.

    No seeding, no migrations beyond what Database.__init__ runs automatically.
    Use this when you want to control exactly which `bot_config` keys exist.
    """
    return Database(str(tmp_path / "test.db"))


@pytest.fixture
def seeded_db(tmp_path) -> Database:
    """A DB pre-seeded with `_seed_optimized_defaults()` — the live baseline.

    Mirrors what production looks like on first bot startup. Use this for
    parity tests where you need the dashboard / live paths to read the SAME
    set of `bot_config` keys.
    """
    # Imported lazily to avoid pulling main.py side effects on every test.
    from main import _seed_optimized_defaults
    db = Database(str(tmp_path / "test.db"))
    _seed_optimized_defaults(db)
    return db


# ─────────────────────────────────────────────────────────────────────────────
# Re-exports for convenience
# ─────────────────────────────────────────────────────────────────────────────

# Allow `from tests.conftest import make_ohlcv, uptrend` from test modules
# that prefer explicit imports over fixture-style access.
__all__ = [
    "make_ohlcv",
    "uptrend",
    "downtrend",
    "flat",
    "choppy",
    "tmp_db",
    "seeded_db",
]
