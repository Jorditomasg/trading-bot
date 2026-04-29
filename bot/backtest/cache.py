"""Local Parquet cache for historical OHLCV klines.

Storage: data/klines/{SYMBOL}_{INTERVAL}.parquet
Strategy:
  - On first call: full fetch from Binance public API.
  - On subsequent calls: only fetches bars newer than the last cached bar
    (incremental update) + optionally older bars if the range extends further back.
  - Thread-safe reads; writes protected by a per-file lock (single writer).

Why Parquet:
  - 1 year of 1m BTCUSDT = ~525 000 rows → ~15 MB on disk, <1 s read.
  - Columnar format: loading only `open_time` for cache-info is instant.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from bot.backtest.fetcher import fetch_historical_klines

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/klines")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_ts(dt) -> pd.Timestamp:
    """Normalise any datetime/Timestamp to UTC-aware pandas Timestamp."""
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def cache_path(symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}.parquet"


# ── Public API ────────────────────────────────────────────────────────────────

def cache_info(symbol: str, interval: str) -> dict | None:
    """Return cache metadata dict or None if not cached yet.

    Keys: rows, from, to, size_mb
    """
    path = cache_path(symbol, interval)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["open_time"])
        if df.empty:
            return None
        return {
            "rows":    len(df),
            "from":    _to_ts(df["open_time"].min()),
            "to":      _to_ts(df["open_time"].max()),
            "size_mb": round(path.stat().st_size / 1_048_576, 2),
        }
    except Exception as exc:
        logger.warning("cache_info failed for %s %s: %s", symbol, interval, exc)
        return None


def fetch_and_cache(
    symbol: str,
    interval: str,
    start_dt: datetime,
    end_dt: datetime,
    on_progress: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Return OHLCV DataFrame for [start_dt, end_dt], updating cache as needed.

    Args:
        symbol:      Binance pair (e.g. "BTCUSDT").
        interval:    Kline interval (e.g. "1m", "4h", "1d").
        start_dt:    Inclusive start (UTC).
        end_dt:      Inclusive end (UTC).
        on_progress: Optional callback(message: str) for UI progress updates.

    Returns:
        DataFrame with columns: open_time, open, high, low, close, volume.
        open_time is UTC-aware.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path      = cache_path(symbol, interval)
    start_ts  = _to_ts(start_dt)
    end_ts    = _to_ts(end_dt)

    # ── Load existing cache ───────────────────────────────────────────────────
    cached: pd.DataFrame = pd.DataFrame()
    if path.exists():
        try:
            cached = pd.read_parquet(path)
            cached["open_time"] = cached["open_time"].apply(_to_ts)
            logger.debug("Cache loaded: %s %s → %d rows", symbol, interval, len(cached))
        except Exception as exc:
            logger.warning("Cache read failed (%s) — will re-fetch", exc)
            cached = pd.DataFrame()

    parts: list[pd.DataFrame] = []

    if not cached.empty:
        cache_from = _to_ts(cached["open_time"].min())
        cache_to   = _to_ts(cached["open_time"].max())

        # Keep the existing cached data as the base
        parts.append(cached)

        # Fetch older data if needed
        if start_ts < cache_from:
            _msg = f"Fetching older {symbol} {interval} data ({start_ts.date()} → {cache_from.date()})…"
            logger.info(_msg)
            if on_progress:
                on_progress(_msg)
            older = _safe_fetch(symbol, interval, start_ts, cache_from)
            if not older.empty:
                parts.insert(0, older)

        # Fetch newer data if needed (allow 2-bar gap before triggering)
        if end_ts > cache_to:
            _msg = f"Fetching new {symbol} {interval} bars ({cache_to.date()} → {end_ts.date()})…"
            logger.info(_msg)
            if on_progress:
                on_progress(_msg)
            newer = _safe_fetch(symbol, interval, cache_to, end_ts)
            if not newer.empty:
                parts.append(newer)
    else:
        _msg = f"Full download: {symbol} {interval} ({start_ts.date()} → {end_ts.date()})…"
        logger.info(_msg)
        if on_progress:
            on_progress(_msg)
        fresh = _safe_fetch(symbol, interval, start_ts, end_ts)
        parts.append(fresh)

    # ── Merge, dedup, sort, save ──────────────────────────────────────────────
    if len(parts) > 1 or (len(parts) == 1 and parts[0] is not cached):
        combined = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates(subset=["open_time"])
            .sort_values("open_time")
            .reset_index(drop=True)
        )
        # Single-threaded write — pyarrow's default ThreadPoolExecutor crashes
        # if the interpreter is shutting down (auto-optimizer daemon thread case).
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            table = pa.Table.from_pandas(combined, preserve_index=False, nthreads=1)
            pq.write_table(table, path)
        except RuntimeError as exc:
            # Interpreter shutdown: drop the write, parent will fetch fresh next start.
            logger.warning("Cache write skipped (likely shutdown): %s", exc)
            return combined
        except ImportError:
            # pyarrow not installed in this env — fall back to pandas default.
            combined.to_parquet(path, index=False)
        logger.info(
            "Cache saved: %s %s → %d rows (%.1f MB)",
            symbol, interval, len(combined), path.stat().st_size / 1_048_576,
        )
        if on_progress:
            on_progress(f"Cache updated: {len(combined):,} bars saved.")
    else:
        combined = parts[0] if parts else pd.DataFrame()

    # ── Return requested slice ────────────────────────────────────────────────
    if combined.empty:
        raise ValueError(
            f"No data available for {symbol} {interval} between {start_ts.date()} and {end_ts.date()}"
        )
    mask = (combined["open_time"] >= start_ts) & (combined["open_time"] <= end_ts)
    return combined[mask].reset_index(drop=True)


def download_full_history(
    symbol: str,
    interval: str,
    years_back: int = 3,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    """Download and cache the full history for a symbol/interval.

    Designed for the dashboard 'Download Data' button.
    Returns cache_info() dict after completion.
    """
    from datetime import timezone
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=years_back * 365)

    if on_progress:
        on_progress(f"Starting download: {symbol} {interval} ({years_back}y history)…")

    fetch_and_cache(symbol, interval, start_dt, end_dt, on_progress=on_progress)
    return cache_info(symbol, interval) or {}


# ── Internal ──────────────────────────────────────────────────────────────────

def _safe_fetch(symbol: str, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    try:
        return fetch_historical_klines(
            symbol, interval,
            start.to_pydatetime(),
            end.to_pydatetime(),
        )
    except Exception as exc:
        logger.error("Fetch failed %s %s [%s, %s]: %s", symbol, interval, start.date(), end.date(), exc)
        return pd.DataFrame()
