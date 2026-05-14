"""Tests for bot.audit.walk_forward.split_windows()."""
from datetime import datetime

import pytest

from bot.audit.walk_forward import WalkForwardConfig, Window, split_windows


def _cfg(**overrides) -> WalkForwardConfig:
    base = dict(
        start_date=datetime(2022, 4, 1),
        end_date=datetime(2026, 4, 1),       # 48 months total
        train_months=18,
        test_months=3,
        step_months=3,
        symbols=("BTCUSDT",),
        timeframe="4h",
    )
    base.update(overrides)
    return WalkForwardConfig(**base)


def test_happy_path_window_count() -> None:
    """48 months total - 18 train = 30 months of testable space. 30 / 3 step = 10 windows."""
    windows = split_windows(_cfg())
    assert len(windows) == 10


def test_first_window_boundaries() -> None:
    windows = split_windows(_cfg())
    w0 = windows[0]
    assert w0.index == 0
    assert w0.train_start == datetime(2022, 4, 1)
    assert w0.train_end   == datetime(2023, 10, 1)
    assert w0.test_start  == datetime(2023, 10, 1)
    assert w0.test_end    == datetime(2024, 1, 1)


def test_last_window_does_not_exceed_end_date() -> None:
    windows = split_windows(_cfg())
    assert windows[-1].test_end <= datetime(2026, 4, 1)


def test_test_windows_do_not_overlap_when_step_equals_test_months() -> None:
    """With step == test_months, test windows must be back-to-back, never overlapping."""
    windows = split_windows(_cfg())
    for prev, curr in zip(windows, windows[1:]):
        assert prev.test_end <= curr.test_start, (
            f"Overlap: window {prev.index}.test_end={prev.test_end} > "
            f"window {curr.index}.test_start={curr.test_start}"
        )


def test_train_always_precedes_test() -> None:
    for w in split_windows(_cfg()):
        assert w.train_start < w.train_end
        assert w.train_end == w.test_start  # back-to-back
        assert w.test_start < w.test_end


def test_data_shorter_than_one_window_returns_empty() -> None:
    """Range = 12 months, train_months=18 → no window fits."""
    short = _cfg(end_date=datetime(2023, 4, 1))
    assert split_windows(short) == []


def test_window_indices_are_sequential_from_zero() -> None:
    windows = split_windows(_cfg())
    assert [w.index for w in windows] == list(range(len(windows)))


def test_custom_step_creates_overlapping_test_windows() -> None:
    """step=1 with test=3 means test windows DO overlap — allowed for non-statistical runs."""
    cfg = _cfg(step_months=1)
    windows = split_windows(cfg)
    # First two windows should share 2/3 of test period
    assert windows[1].test_start < windows[0].test_end


def test_train_months_zero_is_allowed() -> None:
    """No warm-up — full date range used as test. Useful for sanity-check runs."""
    cfg = _cfg(train_months=0)
    windows = split_windows(cfg)
    assert windows[0].train_start == windows[0].train_end == datetime(2022, 4, 1)
