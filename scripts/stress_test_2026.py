#!/usr/bin/env python
"""Full stress test of the live strategy per the `backtest-expert` methodology.

Why this exists — two problems with the existing validation:

1. **Wrong config.** `scripts/validate_live_risk_2026.py` (which produced the
   Risk×DD table in CLAUDE.md) builds `BacktestConfig(...)` by hand and leaves
   every entry-quality filter at `None`, `bias_strict=False` and
   `kelly_enabled=True`. Live runs all four filters ON, `bias_strict` ON and
   Kelly OFF. This script routes through `portfolio_runner.build_backtest_config`
   — the parity-guarded path (gotchas #36/#37) — fed with the ACTUAL live
   `bot_config` values, so the numbers describe the deployed strategy.

2. **Wrong window.** Only 3.25 years were cached, so the strategy had never been
   tested through the 2021 blow-off top or the 2022 bear. `backtest-expert`
   requires 5y minimum and explicit multi-regime coverage. Run
   `scripts/fetch_long_history.py` first — history now goes back to 2017.

Sections mirror the skill's workflow:
  §3 baseline, §4 stress (parameter sensitivity / friction / time robustness),
  §6 evaluation inputs.

Runs are CPU-bound and independent, so they are farmed to a process pool. Keep
WORKERS low — this host runs a 7 GiB Minecraft server and has ~3 GiB free.

Run:
    PYTHONPATH=. python scripts/stress_test_2026.py
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Pool

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for _noisy in ("bot.bias.filter", "bot.backtest.cache", "bot.backtest.fetcher",
               "bot.backtest.engine", "bot.backtest.portfolio_engine"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

import pandas as pd

from bot.backtest.cache import cache_path
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.portfolio_runner import BacktestRequest, build_backtest_config
from bot.backtest.scenario_runner import compute_annual_return

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
CAPITAL = 10_000.0
BASE_RISK = 0.015
WORKERS = int(os.getenv("STRESS_WORKERS", "4"))

# The live bot_config, read out of the running container on 2026-08-15. Hardcoded
# rather than read from a local DB because the local trading_bot.db is a stale
# dev copy with an EMPTY bot_config — using it would silently fall back to the
# `build_backtest_config` defaults and defeat the entire point of this script.
LIVE_RUNTIME_CFG = {
    "long_only": "true",
    "ema_stop_mult": "1.5",
    "ema_tp_mult": "5.0",
    "ema_max_dist_atr": "1.0",
    "ema_vol_mult": "1.5",
    "ema_momentum_req": "true",
    "ema_bar_dir": "true",
    "ema_min_atr": "0.005",
    "ema_min_entry_adx": "0.0",
    "ema_require_ema200": "false",
    "bias_neutral_passthrough": "false",   # → bias_strict = True
    "kelly_enabled": "false",
}

# Windows referenced by name so job specs stay picklable and tiny.
WINDOWS: dict[str, tuple[str, str | None, list[str]]] = {
    "full3":     ("2020-08-11", None,         SYMBOLS),
    "full2":     ("2017-08-17", None,         ["BTCUSDT", "ETHUSDT"]),
    "recent3y":  ("2023-08-16", None,         SYMBOLS),
    "bull2021":  ("2020-08-11", "2021-12-31", SYMBOLS),
    "bear2022":  ("2022-01-01", "2022-12-31", ["BTCUSDT", "ETHUSDT"]),
    "rec2324":   ("2023-01-01", "2024-12-31", SYMBOLS),
    "chop2526":  ("2025-01-01", None,         SYMBOLS),
    **{f"y{y}": (f"{y}-01-01", f"{y}-12-31 23:59",
                 SYMBOLS if y >= 2021 else ["BTCUSDT", "ETHUSDT"])
       for y in range(2018, 2027)},
}


@dataclass
class Run:
    label: str
    annual: float
    dd: float
    pf: float
    sharpe: float
    trades: int
    win_rate: float
    avg_win_pct: float
    avg_loss_pct: float
    final: float
    extra: dict = field(default_factory=dict)

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


# ── Worker side ──────────────────────────────────────────────────────────────

_CACHE: dict[str, pd.DataFrame] = {}


def _init_worker() -> None:
    """Load every parquet once per worker process.

    Reads the cache files directly rather than calling `fetch_and_cache`: the
    cache is already complete (see scripts/fetch_long_history.py) and four
    workers racing to update the same parquet would be a write conflict.
    """
    for sym in SYMBOLS:
        for interval in ("4h", "1d", "1w"):
            path = cache_path(sym, interval)
            if path.exists():
                df = pd.read_parquet(path)
                df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
                _CACHE[f"{sym}_{interval}"] = df


def _window_data(window: str) -> dict:
    start_s, end_s, syms = WINDOWS[window]
    start = pd.Timestamp(start_s, tz="UTC")
    end = pd.Timestamp(end_s, tz="UTC") if end_s else pd.Timestamp.now(tz="UTC")
    out = {}
    for sym in syms:
        cut = {}
        for interval in ("4h", "1d", "1w"):
            df = _CACHE.get(f"{sym}_{interval}")
            if df is None:
                continue
            t = df["open_time"]
            cut[interval] = df[(t >= start) & (t <= end)].reset_index(drop=True)
        if "4h" not in cut or len(cut["4h"]) < 250:
            continue   # not enough bars for warmup + a meaningful test
        out[sym] = (cut["4h"], cut.get("1d"), cut.get("1w"))
    return out


def _execute(job: dict) -> dict:
    data = _window_data(job["window"])
    if not data:
        return {"label": job["label"], "skipped": True}

    cfg_dict = dict(LIVE_RUNTIME_CFG)
    cfg_dict.update(job.get("overrides") or {})
    req = BacktestRequest(
        symbols=tuple(data.keys()),
        timeframe="4h",
        start_dt=datetime(2017, 8, 1, tzinfo=timezone.utc),
        end_dt=datetime.now(timezone.utc),
        capital=CAPITAL,
        risk=job.get("risk", BASE_RISK),
        cost_per_side=job.get("cost", 0.001),
        use_bias=True,
        use_momentum=False,
        use_1m=False,
    )
    engine = PortfolioBacktestEngine(config=build_backtest_config(req, cfg_dict))
    pr = engine.run_portfolio(
        dfs={s: d[0] for s, d in data.items()},
        dfs_4h={s: d[1] for s, d in data.items()},
        dfs_weekly={s: d[2] for s, d in data.items()},
    )
    ps = pr.portfolio_summary
    trades = [t for ts in pr.per_symbol_trades.values() for t in ts]
    closed = [t for t in trades if t.get("exit_price") is not None]
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) <= 0]

    def avg_pct(rows):
        vals = [t.get("pnl_pct") for t in rows if t.get("pnl_pct") is not None]
        return sum(vals) / len(vals) if vals else 0.0

    first = list(data.values())[0][0]["open_time"].iloc[0]
    last = list(data.values())[0][0]["open_time"].iloc[-1]
    days = max((last - first).days, 1)

    return {
        "label": job["label"],
        "skipped": False,
        "annual": compute_annual_return(pr.initial_capital, pr.final_capital, days),
        "dd": ps.get("max_drawdown_pct", 0.0),
        "pf": ps.get("profit_factor", 0.0),
        "sharpe": ps.get("sharpe_ratio", 0.0),
        "trades": len(closed),
        "win_rate": (len(wins) / len(closed) * 100.0) if closed else 0.0,
        "avg_win_pct": avg_pct(wins),
        "avg_loss_pct": abs(avg_pct(losses)),
        "final": pr.final_capital,
        "extra": {"days": days, "symbols": list(data.keys()), **job.get("meta", {})},
    }


def to_run(d: dict) -> Run:
    return Run(
        label=d["label"], annual=d["annual"], dd=d["dd"], pf=d["pf"],
        sharpe=d["sharpe"], trades=d["trades"], win_rate=d["win_rate"],
        avg_win_pct=d["avg_win_pct"], avg_loss_pct=d["avg_loss_pct"],
        final=d["final"], extra=d["extra"],
    )


# ── Presentation ─────────────────────────────────────────────────────────────

def hdr(title: str) -> None:
    print("\n" + "=" * 84)
    print(title)
    print("=" * 84)


def head(name_w: int = 30) -> str:
    return (f"{'run':<{name_w}} {'annual':>8} {'maxDD':>8} {'PF':>6} "
            f"{'Calmar':>7} {'n':>6} {'win%':>7}")


def row(r: Run, name_w: int = 30) -> str:
    return (f"{r.label:<{name_w}} {r.annual * 100:>7.1f}% {r.dd:>7.1f}% {r.pf:>6.2f} "
            f"{r.calmar:>7.2f} {r.trades:>6} {r.win_rate:>6.1f}%")


def main() -> None:
    jobs: list[dict] = []

    # §3 baseline + §3b parity gap
    jobs += [
        {"label": "BTC+ETH+SOL 2020-08→now", "window": "full3", "sec": "baseline"},
        {"label": "BTC+ETH 2017-08→now", "window": "full2", "sec": "baseline"},
        {"label": "3y live-parity config", "window": "recent3y", "sec": "parity"},
        {"label": "3y validate_live_risk cfg", "window": "recent3y", "sec": "parity",
         "overrides": {"ema_vol_mult": "0", "ema_momentum_req": "false",
                       "ema_bar_dir": "false", "ema_min_atr": "0",
                       "bias_neutral_passthrough": "true", "kelly_enabled": "true"}},
    ]
    # §4a parameter sensitivity — 50–150% of stop, 80–120% of TP
    for stop in (0.75, 1.125, 1.5, 1.875, 2.25):
        for tp in (4.0, 4.5, 5.0, 5.5, 6.0):
            jobs.append({
                "label": f"s{stop}/t{tp}", "window": "full3", "sec": "sensitivity",
                "overrides": {"ema_stop_mult": str(stop), "ema_tp_mult": str(tp)},
                "meta": {"stop": stop, "tp": tp},
            })
    # §4b friction
    for mult, cost in ((1.0, 0.001), (1.5, 0.0015), (2.0, 0.002), (3.0, 0.003)):
        jobs.append({"label": f"cost {mult:.1f}x ({cost*100:.2f}%/side)",
                     "window": "full3", "sec": "friction", "cost": cost})
    # §4c time robustness
    for y in range(2018, 2027):
        jobs.append({"label": str(y), "window": f"y{y}", "sec": "by_year"})

    print(f"Dispatching {len(jobs)} backtests across {WORKERS} workers…")
    with Pool(WORKERS, initializer=_init_worker) as pool:
        raw = pool.map(_execute, jobs)

    by_sec: dict[str, list[dict]] = {}
    for spec, res in zip(jobs, raw):
        if res.get("skipped"):
            continue
        by_sec.setdefault(spec["sec"], []).append(res)

    hdr("§3  BASELINE — live-parity config, full history")
    print(head())
    for d in by_sec["baseline"]:
        print(row(to_run(d)))

    hdr("§3b PARITY GAP — live config vs the config CLAUDE.md's table used")
    print(head())
    for d in by_sec["parity"]:
        print(row(to_run(d)))

    hdr("§4a PARAMETER SENSITIVITY — stop × TP grid (seek plateaus, not peaks)")
    print("Baseline stop=1.5 tp=5.0. Rows = stop mult, cols = TP mult. Cell = Calmar.")
    sens = {(d["extra"]["stop"], d["extra"]["tp"]): d for d in by_sec["sensitivity"]}
    tps = sorted({k[1] for k in sens})
    print(f"\n{'stop':>6} " + " ".join(f"{t:>8.1f}" for t in tps))
    for stop in sorted({k[0] for k in sens}):
        cells = " ".join(f"{sens[(stop, t)]['annual']*100/sens[(stop, t)]['dd']:>8.2f}"
                         if sens[(stop, t)]["dd"] > 0 else f"{'—':>8}" for t in tps)
        print(f"{stop:>6.3f} {cells}")
    print(f"\n{'stop':>6} {'tp':>6} {'annual':>8} {'maxDD':>8} {'PF':>6} {'Calmar':>7} {'n':>6}")
    for (stop, tp), d in sorted(sens.items()):
        r = to_run(d)
        print(f"{stop:>6.3f} {tp:>6.1f} {r.annual*100:>7.1f}% {r.dd:>7.1f}% "
              f"{r.pf:>6.2f} {r.calmar:>7.2f} {r.trades:>6}")

    hdr("§4b EXECUTION FRICTION — cost per side stressed to 1.5×, 2×, 3×")
    print(head())
    for d in by_sec["friction"]:
        print(row(to_run(d)))

    hdr("§4c TIME ROBUSTNESS — calendar-year breakdown")
    regimes = {2018: "bear", 2019: "recovery", 2020: "covid crash + bull",
               2021: "blow-off top", 2022: "bear", 2023: "recovery",
               2024: "bull", 2025: "chop/decline", 2026: "bear"}
    print(f"{'year':<8} {'annual':>8} {'maxDD':>8} {'PF':>6} {'n':>6} {'win%':>7}  regime")
    yrs = [to_run(d) for d in by_sec["by_year"]]
    for r in yrs:
        print(f"{r.label:<8} {r.annual*100:>7.1f}% {r.dd:>7.1f}% {r.pf:>6.2f} "
              f"{r.trades:>6} {r.win_rate:>6.1f}%  {regimes.get(int(r.label), '')}")
    pos = [r for r in yrs if r.annual > 0]
    print(f"\nPositive years: {len(pos)}/{len(yrs)} "
          f"(skill requires a majority for time robustness)")

    hdr("§6  EVALUATION INPUTS for backtest-expert/scripts/evaluate_backtest.py")
    b = to_run(max(by_sec["baseline"], key=lambda d: d["trades"]))
    print(f"  --total-trades      {b.trades}")
    print(f"  --win-rate          {b.win_rate:.0f}")
    # `pnl_pct` is a DECIMAL fraction of notional (engine.py: 0.02 = +2%), while
    # evaluate_backtest.py wants percent. Convert, or the evaluator reads a 13%
    # average win as 0.13% and scores the expectancy as near-zero.
    print(f"  --avg-win-pct       {b.avg_win_pct * 100:.2f}")
    print(f"  --avg-loss-pct      {b.avg_loss_pct * 100:.2f}")
    print(f"  --max-drawdown-pct  {b.dd:.0f}")
    print(f"  --years-tested      {b.extra['days'] / 365:.0f}")
    print(f"  (from: {b.label})")

    out = "docs/audits/stress_test_2026-08-15.json"
    with open(out, "w") as fh:
        json.dump(by_sec, fh, indent=2, default=str)
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    main()
