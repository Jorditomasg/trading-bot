"""External news-blackout filter (CryptoPanic + economic calendar).

Unlike `news_pause.py` (endogenous, ATR/volume spikes), this filter pulls REAL
news data and pauses entries when high-impact news is recent or imminent.

Sources:
    1. CryptoPanic API (free tier, 1k req/day, no auth needed for public posts).
       Filters by `important=true` posts within `news_window_min` minutes.
    2. Static economic calendar — hardcoded list of FOMC/CPI/NFP dates.
       The calendar is a YAML/JSON file; if absent, only CryptoPanic runs.

This module is OPT-IN. To enable, instantiate with `enabled=True` and call
`is_blackout_active()` from `main.run_cycle()` BEFORE generating signals.

IMPORTANT — VALIDATION CAVEAT:
CryptoPanic does not provide historical post data on the free tier. This filter
CANNOT be backtested rigorously — only forward-tested. Recommended approach:
    1. Run in observe-only mode (log triggers, do NOT block trades) for 1-2 months.
    2. Compare logged triggers to actual market reactions.
    3. Tune `votes_threshold` and `news_window_min` based on real false positive
       rate before flipping to active mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
HTTP_TIMEOUT_SEC = 5


@dataclass
class NewsBlackoutConfig:
    enabled:           bool   = False
    # CryptoPanic
    cryptopanic_enabled:   bool  = True
    cryptopanic_api_key:   str   = ""    # optional — public posts work without it
    cryptopanic_currencies: str  = "BTC,ETH"
    news_window_min:       int   = 60    # block entries for N min after qualifying news
    votes_threshold:       int   = 50    # only trigger on posts with >= N community votes
    require_important_flag: bool = True  # use the API's `important=true` filter
    # Economic calendar (optional)
    calendar_enabled:      bool  = False
    calendar_pre_min:      int   = 30    # block this many minutes BEFORE event
    calendar_post_min:     int   = 120   # block this many minutes AFTER event
    # Operational
    cache_ttl_sec:         int   = 60    # cache CryptoPanic responses for N seconds
    observe_only:          bool  = True  # if True, log but don't actually block


@dataclass
class _CacheEntry:
    fetched_at: datetime
    triggered:  bool
    reason:     str


class NewsBlackoutFilter:
    """Filter that returns True when blackout is active.

    Cached and rate-limited so it doesn't hammer CryptoPanic on every cycle.
    """

    def __init__(
        self,
        config:           NewsBlackoutConfig,
        economic_events:  list[datetime] | None = None,
    ) -> None:
        self.config = config
        self._economic_events = economic_events or []
        self._cache: _CacheEntry | None = None
        self._trigger_count = 0   # diagnostic

    @property
    def trigger_count(self) -> int:
        return self._trigger_count

    def is_blackout_active(self, now: datetime | None = None) -> bool:
        """Return True if blackout should be active at `now` (default UTC now)."""
        if not self.config.enabled:
            return False

        now = now or datetime.now(tz=timezone.utc)

        # Calendar check is cheap (in-memory); always run first
        if self.config.calendar_enabled and self._calendar_blackout(now):
            self._trigger_count += 1
            if self.config.observe_only:
                logger.info("news_blackout: OBSERVE-ONLY trigger (calendar) at %s", now.isoformat())
                return False
            return True

        # CryptoPanic check (cached)
        if self.config.cryptopanic_enabled:
            if self._cryptopanic_blackout(now):
                self._trigger_count += 1
                if self.config.observe_only:
                    logger.info("news_blackout: OBSERVE-ONLY trigger (cryptopanic) at %s", now.isoformat())
                    return False
                return True

        return False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _calendar_blackout(self, now: datetime) -> bool:
        for event in self._economic_events:
            window_start = event - timedelta(minutes=self.config.calendar_pre_min)
            window_end   = event + timedelta(minutes=self.config.calendar_post_min)
            if window_start <= now <= window_end:
                logger.info("news_blackout: calendar event window active (event=%s)", event.isoformat())
                return True
        return False

    def _cryptopanic_blackout(self, now: datetime) -> bool:
        if self._cache is not None:
            age_sec = (now - self._cache.fetched_at).total_seconds()
            if age_sec < self.config.cache_ttl_sec:
                return self._cache.triggered

        triggered, reason = self._fetch_cryptopanic(now)
        self._cache = _CacheEntry(fetched_at=now, triggered=triggered, reason=reason)
        if triggered:
            logger.info("news_blackout: CryptoPanic trigger — %s", reason)
        return triggered

    def _fetch_cryptopanic(self, now: datetime) -> tuple[bool, str]:
        params: dict = {
            "currencies": self.config.cryptopanic_currencies,
            "public":     "true",
        }
        if self.config.cryptopanic_api_key:
            params["auth_token"] = self.config.cryptopanic_api_key
        if self.config.require_important_flag:
            params["filter"] = "important"

        try:
            r = requests.get(CRYPTOPANIC_URL, params=params, timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.warning("news_blackout: CryptoPanic fetch failed (%s) — fail-open", exc)
            return False, f"fetch failed: {exc}"

        results = data.get("results") or []
        if not results:
            return False, "no posts"

        cutoff = now - timedelta(minutes=self.config.news_window_min)
        for post in results:
            created_str = post.get("created_at") or ""
            try:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if created < cutoff:
                continue

            votes = post.get("votes") or {}
            score = (votes.get("important") or 0) + (votes.get("positive") or 0) + (votes.get("negative") or 0)
            if score < self.config.votes_threshold:
                continue

            title = post.get("title") or ""
            return True, f"{title[:80]}... (votes={score}, age={int((now-created).total_seconds()/60)}min)"

        return False, f"no qualifying post (checked {len(results)})"


def parse_economic_events_iso(date_strings: list[str]) -> list[datetime]:
    """Parse a list of ISO 8601 datetime strings into UTC-aware datetimes.

    Use this to load a static economic calendar from a JSON/YAML file.
    Example: ["2026-05-07T18:00:00Z", "2026-05-15T12:30:00Z"]
    """
    out: list[datetime] = []
    for s in date_strings:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append(dt)
        except ValueError as exc:
            logger.warning("economic calendar: skipping invalid date %s: %s", s, exc)
    return sorted(out)
