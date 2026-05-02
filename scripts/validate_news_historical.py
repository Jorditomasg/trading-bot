#!/usr/bin/env python
"""Historical news-blackout simulation using REAL public data.

Two-part validation that doesn't need CryptoPanic historical access:

PART A — Economic calendar (deterministic, public):
    Hardcoded FOMC + CPI + NFP dates 2022-2026. Pause = 30 min before, 2h after.
    Tests if pausing entries around scheduled macro events would have helped.

PART B — Major crypto events (proxy for "what CryptoPanic would have flagged"):
    Hardcoded list of 15 known high-impact crypto news from 2022-2025
    (Luna collapse, FTX, SVB, ETF approval, halving, Trump etc.).
    Pause = 12h after each event.

Both parts run as COUNTERFACTUAL on the existing portfolio backtest:
    1. Run BTC+ETH portfolio normally over 3 years.
    2. Mark each trade as "in_blackout" if entry_time falls in any blackout window.
    3. Compute approximate metrics excluding those trades:
       - PnL: sum of trades that would survive the filter
       - Win rate: filtered subset
    4. Compare to baseline.

Caveat: this is approximate (doesn't recompute capital trajectory exactly).
PnL of skipped trades is removed from total but compounding is ignored. Useful
as first-pass check: if filtered trades have systematically WORSE WR/PnL than
unfiltered, the filter would help. If similar, filter is a no-op.

Run:
    PYTHONPATH=. venv/bin/python scripts/validate_news_historical.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine

# ── PART A: Economic calendar (FOMC, CPI, NFP) — UTC times ────────────────────
# FOMC decisions release at 18:00 UTC (2pm ET) on day-2 of meeting.
# CPI releases at 12:30 UTC (8:30am ET).
# NFP releases at 12:30 UTC first Friday of month.
FOMC_DATES = [
    "2022-03-16T18:00:00Z", "2022-05-04T18:00:00Z", "2022-06-15T18:00:00Z",
    "2022-07-27T18:00:00Z", "2022-09-21T18:00:00Z", "2022-11-02T18:00:00Z",
    "2022-12-14T18:00:00Z",
    "2023-02-01T18:00:00Z", "2023-03-22T18:00:00Z", "2023-05-03T18:00:00Z",
    "2023-06-14T18:00:00Z", "2023-07-26T18:00:00Z", "2023-09-20T18:00:00Z",
    "2023-11-01T18:00:00Z", "2023-12-13T18:00:00Z",
    "2024-01-31T18:00:00Z", "2024-03-20T18:00:00Z", "2024-05-01T18:00:00Z",
    "2024-06-12T18:00:00Z", "2024-07-31T18:00:00Z", "2024-09-18T18:00:00Z",
    "2024-11-07T18:00:00Z", "2024-12-18T18:00:00Z",
    "2025-01-29T19:00:00Z", "2025-03-19T18:00:00Z", "2025-05-07T18:00:00Z",
    "2025-06-18T18:00:00Z", "2025-07-30T18:00:00Z", "2025-09-17T18:00:00Z",
    "2025-11-04T18:00:00Z", "2025-12-09T18:00:00Z",
    "2026-01-28T19:00:00Z", "2026-03-18T18:00:00Z",
]

# CPI: monthly, ~10th-15th of month at 12:30 UTC (approximate)
CPI_DATES = [
    f"{year}-{month:02d}-{day:02d}T12:30:00Z"
    for year in [2022, 2023, 2024, 2025, 2026]
    for month, day in [(1, 12), (2, 10), (3, 10), (4, 12), (5, 11), (6, 10),
                       (7, 13), (8, 10), (9, 13), (10, 12), (11, 10), (12, 13)]
]


# ── PART B: Major crypto news events (proxy for CryptoPanic would-flag) ───────
# These are real high-impact events with known dates. Pause window = 12h after.
CRYPTO_EVENTS = [
    ("2022-05-09T00:00:00Z", "Luna/UST collapse begins"),
    ("2022-06-13T00:00:00Z", "Celsius freezes withdrawals"),
    ("2022-11-08T00:00:00Z", "FTX collapse / Alameda exposure"),
    ("2023-03-10T00:00:00Z", "SVB bank run / USDC depeg"),
    ("2023-06-05T00:00:00Z", "SEC lawsuit vs Binance"),
    ("2023-08-29T00:00:00Z", "Grayscale ETF win vs SEC"),
    ("2024-01-10T22:00:00Z", "BTC spot ETF approval announcement"),
    ("2024-03-13T00:00:00Z", "BTC ATH ~$73k"),
    ("2024-04-19T00:00:00Z", "BTC halving"),
    ("2024-08-05T00:00:00Z", "Yen carry trade unwind / market crash"),
    ("2024-11-05T22:00:00Z", "Trump elected"),
    ("2025-01-20T17:00:00Z", "Trump inauguration / crypto orders"),
    ("2025-02-26T00:00:00Z", "Bybit hack — $1.5B stolen"),
    ("2025-04-02T20:00:00Z", "Trump tariffs announcement"),
    ("2025-04-09T00:00:00Z", "Tariff pause / market rally"),
]


def _to_utc(s: str) -> pd.Timestamp:
    return pd.Timestamp(s.replace("Z", "+00:00"))


def _build_blackout_windows(
    events: list[pd.Timestamp],
    pre_min:  int = 30,
    post_min: int = 120,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return [(start, end), ...] windows around each event."""
    return [
        (e - pd.Timedelta(minutes=pre_min), e + pd.Timedelta(minutes=post_min))
        for e in events
    ]


def _is_blocked(entry_time: pd.Timestamp, windows: list[tuple]) -> bool:
    if entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")
    for start, end in windows:
        if start <= entry_time <= end:
            return True
    return False


def _summarize(trades: list[dict], label: str, baseline_pnl: float | None = None) -> dict:
    closed = [t for t in trades if t.get("exit_reason") is not None]
    if not closed:
        return {"label": label, "n": 0, "pnl": 0, "wr": 0, "avg": 0}
    pnl_total = sum((t.get("pnl") or 0.0) for t in closed)
    wins = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    wr   = 100.0 * wins / len(closed)
    avg  = pnl_total / len(closed)
    delta = ""
    if baseline_pnl is not None:
        d = pnl_total - baseline_pnl
        delta = f"  Δ={d:+.0f}"
    print(
        f"  {label:<40} n={len(closed):>3}  PnL=${pnl_total:>+8.0f}  "
        f"WR={wr:>5.1f}%  avg=${avg:>+6.0f}{delta}"
    )
    return {"label": label, "n": len(closed), "pnl": pnl_total, "wr": wr, "avg": avg}


def main() -> None:
    risk = 0.04
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=1095 + 30)

    print(f"\n{'=' * 100}")
    print(f"  HISTORICAL NEWS-BLACKOUT SIMULATION  —  BTC+ETH @ {risk*100:.0f}%, 3y, 4h")
    print(f"{'=' * 100}\n")

    print("Fetching data…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)

    dfs      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    print("Running BTC+ETH portfolio over full 3y…")
    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=risk, timeframe="4h", long_only=True,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    all_trades: list[dict] = []
    for sym, ts in pr.per_symbol_trades.items():
        for t in ts:
            t["_symbol"] = sym
            all_trades.append(t)

    closed = [t for t in all_trades if t.get("exit_reason") is not None]
    print(f"\nTotal closed trades over 3y: {len(closed)}")
    base_pnl = sum((t.get("pnl") or 0.0) for t in closed)
    base_wins = sum(1 for t in closed if (t.get("pnl") or 0.0) > 0)
    print(f"Baseline:  PnL=${base_pnl:+.0f}  WR={100*base_wins/len(closed):.1f}%  trades={len(closed)}\n")

    # ── PART A: Economic calendar ────────────────────────────────────────────
    fomc_events = [_to_utc(d) for d in FOMC_DATES]
    cpi_events  = [_to_utc(d) for d in CPI_DATES]
    cal_events  = sorted(fomc_events + cpi_events)
    cal_windows = _build_blackout_windows(cal_events, pre_min=30, post_min=120)

    print("─" * 100)
    print("PART A — Economic calendar (FOMC + CPI), pause -30min/+2h")
    print(f"  Total events: {len(cal_events)} ({len(fomc_events)} FOMC + {len(cpi_events)} CPI)")
    print("─" * 100)

    blocked_a = [t for t in closed if _is_blocked(pd.Timestamp(t["entry_time"]), cal_windows)]
    kept_a    = [t for t in closed if not _is_blocked(pd.Timestamp(t["entry_time"]), cal_windows)]

    _summarize(closed,    "Baseline (all trades)")
    _summarize(blocked_a, "WOULD-BE-BLOCKED by calendar", baseline_pnl=None)
    _summarize(kept_a,    "WOULD-SURVIVE calendar filter", baseline_pnl=base_pnl)

    # ── PART B: Major crypto events ──────────────────────────────────────────
    print()
    print("─" * 100)
    print("PART B — Major crypto news events (15 events), pause +12h after")
    print("─" * 100)

    crypto_events = [_to_utc(t[0]) for t in CRYPTO_EVENTS]
    crypto_windows = _build_blackout_windows(crypto_events, pre_min=0, post_min=720)  # 12h post

    blocked_b = [t for t in closed if _is_blocked(pd.Timestamp(t["entry_time"]), crypto_windows)]
    kept_b    = [t for t in closed if not _is_blocked(pd.Timestamp(t["entry_time"]), crypto_windows)]

    _summarize(closed,    "Baseline (all trades)")
    _summarize(blocked_b, "WOULD-BE-BLOCKED by crypto-news", baseline_pnl=None)
    _summarize(kept_b,    "WOULD-SURVIVE crypto-news filter", baseline_pnl=base_pnl)

    # ── PART C: Combined ──────────────────────────────────────────────────────
    print()
    print("─" * 100)
    print("PART C — Combined (calendar + crypto-news)")
    print("─" * 100)

    combined_events  = sorted(cal_events + crypto_events)
    combined_windows = (
        _build_blackout_windows(cal_events, pre_min=30, post_min=120)
        + _build_blackout_windows(crypto_events, pre_min=0, post_min=720)
    )

    blocked_c = [t for t in closed if _is_blocked(pd.Timestamp(t["entry_time"]), combined_windows)]
    kept_c    = [t for t in closed if not _is_blocked(pd.Timestamp(t["entry_time"]), combined_windows)]

    _summarize(closed,    "Baseline (all trades)")
    _summarize(blocked_c, "WOULD-BE-BLOCKED (combined)", baseline_pnl=None)
    _summarize(kept_c,    "WOULD-SURVIVE combined filter", baseline_pnl=base_pnl)

    # ── Final verdict ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("VERDICT")
    print(f"{'=' * 100}\n")

    def _verdict(name: str, blocked: list[dict], kept: list[dict]) -> str:
        if not blocked:
            return f"  {name:<10}: 0 trades blocked — filter is a no-op (no overlap with our trades)"
        bp = sum((t.get("pnl") or 0.0) for t in blocked)
        bw = sum(1 for t in blocked if (t.get("pnl") or 0.0) > 0)
        bw_pct = 100*bw/len(blocked) if blocked else 0
        kp = sum((t.get("pnl") or 0.0) for t in kept)
        kw = sum(1 for t in kept if (t.get("pnl") or 0.0) > 0)
        kw_pct = 100*kw/len(kept) if kept else 0

        if bp < 0 and bw_pct < kw_pct - 5:
            return (f"  {name:<10}: GO — blocked trades had PnL=${bp:+.0f} (WR={bw_pct:.0f}%) "
                    f"vs kept WR={kw_pct:.0f}%. Filter would have improved performance.")
        if bp > 0 and bw_pct > kw_pct + 5:
            return (f"  {name:<10}: NO-GO — blocked trades were WINNERS (PnL=${bp:+.0f} WR={bw_pct:.0f}%); "
                    f"filter would HURT performance.")
        return (f"  {name:<10}: NEUTRAL — blocked PnL=${bp:+.0f} WR={bw_pct:.0f}% "
                f"vs kept WR={kw_pct:.0f}%. Filter ≈ no-op statistically.")

    print(_verdict("Calendar",     blocked_a, kept_a))
    print(_verdict("Crypto-news",  blocked_b, kept_b))
    print(_verdict("Combined",     blocked_c, kept_c))
    print()


if __name__ == "__main__":
    main()
