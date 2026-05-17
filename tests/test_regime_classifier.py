"""T17 [TEST] — Failing tests for bot/audit/regime_classifier.py.

TDD: these tests MUST be RED before T18 implements the module.

REQ-REGIME-1 coverage:
  - classify_window_bull: 30-day BTC return >= +5% → BULL
  - classify_window_bear: 30-day BTC return <= -5% → BEAR
  - classify_window_flat: 30-day return within threshold → FLAT
REQ-REGIME-1 edge cases:
  - insufficient_data_returns_flat: < 180 bars before test_start → FLAT (fail-flat)
  - exact_threshold_is_bull: exactly +5% return → BULL (boundary inclusive)
  - exact_negative_threshold_is_bear: exactly -5% return → BEAR (boundary inclusive)
  - custom_threshold_respected
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest


def _make_df(n_bars: int, base_price: float, final_price: float) -> pd.DataFrame:
    """Create a synthetic 4h OHLCV DataFrame with a linear price ramp.

    First bar opens at base_price; last bar closes at final_price.
    Prices are distributed linearly across all bars.
    """
    import numpy as np
    prices = np.linspace(base_price, final_price, n_bars)
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n_bars, freq="4h", tz="UTC"),
        "open":  prices,
        "high":  prices * 1.002,
        "low":   prices * 0.998,
        "close": prices,
        "volume": [1000.0] * n_bars,
    })


def _make_window(test_start: datetime) -> object:
    """Create a minimal duck-type Window with only test_start required."""
    from bot.audit.walk_forward import Window

    train_start = datetime(test_start.year, test_start.month, 1, tzinfo=timezone.utc)
    return Window(
        index=0,
        train_start=train_start,
        train_end=test_start,
        test_start=test_start,
        test_end=datetime(test_start.year, test_start.month + 3, 1, tzinfo=timezone.utc),
    )


class TestRegimeLabelEnum:
    def test_regime_label_is_str_enum(self) -> None:
        """RegimeLabel must inherit from (str, Enum) per project convention (gotcha #5)."""
        from bot.audit.regime_classifier import RegimeLabel

        assert isinstance(RegimeLabel.BULL, str)
        assert RegimeLabel.BULL == "BULL"
        assert RegimeLabel.BEAR == "BEAR"
        assert RegimeLabel.FLAT == "FLAT"

    def test_regime_label_values(self) -> None:
        """All three labels must exist."""
        from bot.audit.regime_classifier import RegimeLabel

        labels = {r.value for r in RegimeLabel}
        assert labels == {"BULL", "BEAR", "FLAT"}


class TestClassifyWindow:
    def _test_start(self) -> datetime:
        """A test_start that is 180+ bars into a larger df."""
        # 180 4h bars = 30 days. We'll build a 400-bar df and use bar 200 as test_start.
        return datetime(2024-1-1 + 0, 8, 20, tzinfo=timezone.utc)  # doesn't matter which date

    def test_classify_window_bull(self) -> None:
        """30-day return >= +5% → BULL."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        # Build 400 bars of 4h data. The first 180 bars (30 days) go from 40000 to 44000 (+10%)
        # test_start is at bar 200 (bar index 199)
        n = 400
        # Linear: start 40000, end at 50000 => 30-day window covering bars 170..200
        # will have a significant upward move
        import numpy as np
        prices = np.linspace(40_000.0, 50_000.0, n)  # 25% total
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        df = pd.DataFrame({
            "open_time": dates,
            "close": prices,
        })
        # test_start at exactly the 200th bar's open_time
        test_start = dates[200]
        window = _make_window(test_start)

        label = classify_window(window, df)
        assert label == RegimeLabel.BULL, f"Expected BULL, got {label}"

    def test_classify_window_bear(self) -> None:
        """30-day return <= -5% → BEAR."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 400
        prices = np.linspace(50_000.0, 35_000.0, n)  # declining
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        df = pd.DataFrame({"open_time": dates, "close": prices})

        test_start = dates[250]
        window = _make_window(test_start)

        label = classify_window(window, df)
        assert label == RegimeLabel.BEAR, f"Expected BEAR, got {label}"

    def test_classify_window_flat(self) -> None:
        """Return within threshold → FLAT."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 400
        # Price barely moves: 1% total over whole range
        prices = np.linspace(45_000.0, 45_450.0, n)  # +1%
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        df = pd.DataFrame({"open_time": dates, "close": prices})

        test_start = dates[250]
        window = _make_window(test_start)

        label = classify_window(window, df)
        assert label == RegimeLabel.FLAT, f"Expected FLAT, got {label}"

    def test_insufficient_data_returns_flat(self) -> None:
        """Fewer than 180 bars before test_start → FLAT (fail-flat per spec)."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 50
        prices = np.linspace(40_000.0, 60_000.0, n)  # clearly bullish if data existed
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        df = pd.DataFrame({"open_time": dates, "close": prices})

        # test_start at bar 40 — only 40 bars prior, not enough
        test_start = dates[40]
        window = _make_window(test_start)

        label = classify_window(window, df)
        assert label == RegimeLabel.FLAT, "Expected FLAT when insufficient bars before test_start"

    def test_exact_positive_threshold_is_bull(self) -> None:
        """Exactly +5% return at the boundary → BULL (inclusive).

        The 30-day window is the last 180 bars BEFORE test_start (bars strictly
        earlier than test_start index). So the anchor bar (bar[idx_start]) must be
        the first bar of the 180-bar window and bar[idx_start+179] (= the last bar
        before test_start) must be +5% higher.
        """
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 400
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        prices = np.full(n, 40_000.0)

        test_start = dates[220]   # test_start is at index 220
        # bars before test_start: indices 0..219  (220 bars)
        # last 180 of those: indices 40..219
        idx_window_start = 220 - 180   # = 40
        idx_window_end   = 219          # last bar before test_start

        # Set the 180-bar window from exactly 40000 to exactly 42000 (+5%)
        prices[idx_window_start:idx_window_end + 1] = np.linspace(
            40_000.0, 42_000.0, idx_window_end - idx_window_start + 1
        )
        # bars before the window and the bar at test_start itself
        prices[:idx_window_start] = 40_000.0
        prices[220:]              = 42_000.0

        df = pd.DataFrame({"open_time": dates, "close": prices})
        window = _make_window(test_start)

        label = classify_window(window, df, threshold_pct=0.05)
        # p_start = prices[40] = 40000, p_end = prices[219] = 42000, ret = +5%
        assert label == RegimeLabel.BULL, f"Exactly +5% should be BULL, got {label}"

    def test_exact_negative_threshold_is_bear(self) -> None:
        """Exactly -5% return at the boundary → BEAR (inclusive)."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 400
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        prices = np.full(n, 40_000.0)

        test_start = dates[220]
        idx_window_start = 220 - 180   # = 40
        idx_window_end   = 219

        # -5%: from 40000 to 38000
        prices[idx_window_start:idx_window_end + 1] = np.linspace(
            40_000.0, 38_000.0, idx_window_end - idx_window_start + 1
        )
        prices[:idx_window_start] = 40_000.0
        prices[220:]              = 38_000.0

        df = pd.DataFrame({"open_time": dates, "close": prices})
        window = _make_window(test_start)

        label = classify_window(window, df, threshold_pct=0.05)
        assert label == RegimeLabel.BEAR, f"Exactly -5% should be BEAR, got {label}"

    def test_custom_threshold_respected(self) -> None:
        """threshold_pct kwarg overrides the default."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        import numpy as np
        n = 400
        dates  = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
        prices = np.linspace(40_000.0, 42_800.0, n)   # ~7% total
        df = pd.DataFrame({"open_time": dates, "close": prices})

        test_start = dates[220]
        window     = _make_window(test_start)

        # With threshold=0.10 (10%), the 30-day return (~3%) should be FLAT
        label = classify_window(window, df, threshold_pct=0.10)
        assert label == RegimeLabel.FLAT, (
            f"With 10% threshold, small return should be FLAT, got {label}"
        )

    def test_no_data_before_test_start_returns_flat(self) -> None:
        """Empty DataFrame → FLAT."""
        from bot.audit.regime_classifier import RegimeLabel, classify_window

        df = pd.DataFrame({"open_time": pd.Series([], dtype="object"), "close": []})
        test_start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        window = _make_window(test_start)

        label = classify_window(window, df)
        assert label == RegimeLabel.FLAT
