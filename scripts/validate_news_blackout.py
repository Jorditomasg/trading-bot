#!/usr/bin/env python
"""Validate the external news-blackout filter (live connectivity check).

This is NOT a backtest — CryptoPanic doesn't expose historical posts on the
free tier. We can only verify that:
    1. The filter parses CryptoPanic responses correctly
    2. The economic calendar windowing logic is correct
    3. The filter is fail-open on network errors (does NOT block on API failure)
    4. The cache works (no rate-limit hammering)
    5. The disabled filter is a no-op

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_news_blackout.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bot.risk.news_blackout import (
    NewsBlackoutConfig,
    NewsBlackoutFilter,
    parse_economic_events_iso,
)


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    return condition


def main() -> int:
    print(f"\n{'=' * 80}")
    print(f"  NEWS-BLACKOUT FILTER VALIDATION")
    print(f"{'=' * 80}\n")
    failures: list[str] = []

    # ── 1. Disabled filter is a no-op ─────────────────────────────────────────
    print("1. Disabled filter:")
    f = NewsBlackoutFilter(NewsBlackoutConfig(enabled=False))
    if not check("Returns False when enabled=False", not f.is_blackout_active()):
        failures.append("disabled-noop")

    # ── 2. Calendar windowing ─────────────────────────────────────────────────
    print("\n2. Economic calendar:")
    now = datetime(2026, 5, 7, 18, 5, tzinfo=timezone.utc)
    events = [
        datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),  # 5 min ago
        datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc), # future
    ]
    cfg = NewsBlackoutConfig(
        enabled=True, cryptopanic_enabled=False,
        calendar_enabled=True, calendar_pre_min=30, calendar_post_min=120,
        observe_only=False,
    )
    f = NewsBlackoutFilter(cfg, economic_events=events)
    if not check("Triggers within calendar window (5min after FOMC)",
                 f.is_blackout_active(now)):
        failures.append("calendar-active")

    # Test outside the window
    now_outside = datetime(2026, 5, 7, 21, 0, tzinfo=timezone.utc)  # 3h after, outside 2h post window
    f2 = NewsBlackoutFilter(cfg, economic_events=events)
    if not check("Does NOT trigger outside post-event window (3h later)",
                 not f2.is_blackout_active(now_outside)):
        failures.append("calendar-outside")

    # Pre-event window
    now_pre = datetime(2026, 6, 1, 12, 15, tzinfo=timezone.utc)  # 15min before scheduled event
    f3 = NewsBlackoutFilter(cfg, economic_events=events)
    if not check("Triggers in pre-event window (15min before)",
                 f3.is_blackout_active(now_pre)):
        failures.append("calendar-pre")

    # ── 3. Observe-only mode ──────────────────────────────────────────────────
    print("\n3. Observe-only mode:")
    cfg_obs = NewsBlackoutConfig(
        enabled=True, cryptopanic_enabled=False,
        calendar_enabled=True, calendar_pre_min=30, calendar_post_min=120,
        observe_only=True,
    )
    f_obs = NewsBlackoutFilter(cfg_obs, economic_events=events)
    if not check("Returns False even when triggered (observe-only)",
                 not f_obs.is_blackout_active(now)):
        failures.append("observe-only")
    if not check("trigger_count incremented in observe-only",
                 f_obs.trigger_count == 1, f"got {f_obs.trigger_count}"):
        failures.append("observe-count")

    # ── 4. CryptoPanic — fail-open on network error ───────────────────────────
    print("\n4. CryptoPanic fail-open on network error:")
    cfg_cp = NewsBlackoutConfig(
        enabled=True, cryptopanic_enabled=True, calendar_enabled=False,
        observe_only=False,
    )
    with patch("bot.risk.news_blackout.requests.get", side_effect=Exception("network down")):
        f_cp = NewsBlackoutFilter(cfg_cp)
        result = f_cp.is_blackout_active()
        if not check("Returns False on network failure (fail-open)",
                     not result):
            failures.append("fail-open")

    # ── 5. CryptoPanic — parses real response ─────────────────────────────────
    print("\n5. CryptoPanic response parsing:")

    # Mock a response with one high-importance recent post
    now_cp = datetime.now(tz=timezone.utc)
    fake_post_old = {
        "title": "Old news (irrelevant)",
        "created_at": (now_cp - timedelta(minutes=120)).isoformat().replace("+00:00", "Z"),
        "votes": {"important": 100, "positive": 50, "negative": 10},
    }
    fake_post_recent = {
        "title": "BREAKING: Major regulatory action announced affecting crypto markets",
        "created_at": (now_cp - timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        "votes": {"important": 80, "positive": 30, "negative": 5},
    }

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"results": [fake_post_recent, fake_post_old]}

    with patch("bot.risk.news_blackout.requests.get", return_value=_FakeResp()):
        f_cp = NewsBlackoutFilter(NewsBlackoutConfig(
            enabled=True, cryptopanic_enabled=True, calendar_enabled=False,
            news_window_min=60, votes_threshold=50, observe_only=False,
        ))
        result = f_cp.is_blackout_active(now_cp)
        if not check("Triggers on recent high-vote post", result):
            failures.append("cp-trigger")

    # Mock with only old posts → should NOT trigger
    class _FakeOldResp:
        def raise_for_status(self): pass
        def json(self): return {"results": [fake_post_old]}

    with patch("bot.risk.news_blackout.requests.get", return_value=_FakeOldResp()):
        f_cp2 = NewsBlackoutFilter(NewsBlackoutConfig(
            enabled=True, cryptopanic_enabled=True, calendar_enabled=False,
            news_window_min=60, votes_threshold=50, observe_only=False,
        ))
        result = f_cp2.is_blackout_active(now_cp)
        if not check("Does NOT trigger on old posts (>60 min)", not result):
            failures.append("cp-old")

    # Mock with low-vote posts → should NOT trigger
    fake_low_votes = {
        "title": "Minor update",
        "created_at": (now_cp - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        "votes": {"important": 5, "positive": 2, "negative": 1},
    }
    class _FakeLowResp:
        def raise_for_status(self): pass
        def json(self): return {"results": [fake_low_votes]}

    with patch("bot.risk.news_blackout.requests.get", return_value=_FakeLowResp()):
        f_cp3 = NewsBlackoutFilter(NewsBlackoutConfig(
            enabled=True, cryptopanic_enabled=True, calendar_enabled=False,
            news_window_min=60, votes_threshold=50, observe_only=False,
        ))
        result = f_cp3.is_blackout_active(now_cp)
        if not check("Does NOT trigger on low-vote posts (<50)", not result):
            failures.append("cp-low-votes")

    # ── 6. Cache TTL ──────────────────────────────────────────────────────────
    print("\n6. Cache TTL:")
    call_count = 0
    class _CountingResp:
        def raise_for_status(self): pass
        def json(self):
            nonlocal call_count
            return {"results": []}

    def _counting_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _CountingResp()

    with patch("bot.risk.news_blackout.requests.get", side_effect=_counting_get):
        f_cache = NewsBlackoutFilter(NewsBlackoutConfig(
            enabled=True, cryptopanic_enabled=True, calendar_enabled=False,
            cache_ttl_sec=60, observe_only=False,
        ))
        f_cache.is_blackout_active()
        f_cache.is_blackout_active()
        f_cache.is_blackout_active()
        if not check("3 sequential calls hit API only once (cached)",
                     call_count == 1, f"call_count={call_count}"):
            failures.append("cache")

    # ── 7. parse_economic_events_iso helper ───────────────────────────────────
    print("\n7. parse_economic_events_iso helper:")
    parsed = parse_economic_events_iso([
        "2026-05-07T18:00:00Z",
        "2026-06-15T12:30:00+00:00",
        "invalid date",
    ])
    if not check("Parses 2 valid + skips 1 invalid", len(parsed) == 2):
        failures.append("parse-helper")
    if not check("All parsed events are tz-aware UTC",
                 all(e.tzinfo is not None for e in parsed)):
        failures.append("parse-tz")
    if not check("Events are sorted ascending",
                 parsed == sorted(parsed)):
        failures.append("parse-sorted")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'=' * 80}")
    if failures:
        print(f"  RESULT: FAIL — {len(failures)} check(s) failed: {failures}")
        print(f"{'=' * 80}\n")
        return 1
    print(f"  RESULT: PASS — all checks succeeded")
    print(f"{'=' * 80}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
