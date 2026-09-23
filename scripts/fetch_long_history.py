#!/usr/bin/env python
"""Extend the kline cache back to 2017 for the live symbol set.

The cache only held data from 2023-05-01 (~3.25 years), which is below the
5-year floor `backtest-expert` requires and — more importantly — excludes the
2021 blow-off top and the 2022 bear market entirely. A long-only trend follower
that has never been tested through a full crypto cycle has no time-robustness
evidence at all.

Run:
    PYTHONPATH=. python scripts/fetch_long_history.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.backtest.cache import cache_info, fetch_and_cache

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
INTERVALS = ["4h", "1d", "1w"]
START = datetime(2017, 8, 1, tzinfo=timezone.utc)


def main() -> None:
    end = datetime.now(timezone.utc)
    for sym in SYMBOLS:
        for interval in INTERVALS:
            before = cache_info(sym, interval)
            df = fetch_and_cache(sym, interval, START, end)
            after = cache_info(sym, interval)
            print(
                f"{sym:9} {interval:3}  rows {before['rows'] if before else 0:>6}"
                f" -> {after['rows'] if after else 0:>6}   "
                f"{after['from'].date() if after else '?'} .. "
                f"{after['to'].date() if after else '?'}  (returned {len(df)})"
            )


if __name__ == "__main__":
    main()
