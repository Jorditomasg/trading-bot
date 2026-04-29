"""Round 3b: PROPER engine-level daily-EMA macro filter.

Re-tests Round 3 with the gate plumbed into _generate_signal so position sizing
compounds correctly, then sweeps risk levels to find the genuine optimum.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.regime.detector import MarketRegime
from bot.strategy.base import Signal
from bot.strategy.signal_factory import hold_signal
from scripts.research.hypotheses import (
    annualize, base_cfg, load_cached, years_span, _payoff,
)

logging.disable(logging.CRITICAL)


class MacroFilteredEngine(BacktestEngine):
    """BacktestEngine that gates BUY entries on a daily-EMA macro trend.

    Builds a per-4h-bar boolean array (macro_long_ok) keyed by the timestamp of
    the most recent COMPLETED daily bar. Hooks into `_get_4h_window` (called
    every bar with current_time) to stash the current timestamp, then reads it
    inside an overridden `_generate_signal`.
    """

    def __init__(
        self,
        cfg: BacktestConfig,
        df_4h_primary: pd.DataFrame,
        df_1d: pd.DataFrame,
        ema_period: int,
    ) -> None:
        super().__init__(cfg)
        self._ema_period = ema_period

        daily = df_1d.copy()
        daily["open_time"] = pd.to_datetime(daily["open_time"], utc=True)
        daily[f"ema{ema_period}"] = daily["close"].ewm(span=ema_period, adjust=False).mean()
        primary = df_4h_primary[["open_time"]].copy().reset_index(drop=True)
        primary["open_time"] = pd.to_datetime(primary["open_time"], utc=True)

        merged = pd.merge_asof(
            primary.sort_values("open_time"),
            daily[["open_time", "close", f"ema{ema_period}"]].sort_values("open_time"),
            on="open_time", direction="backward",
        )
        # Fill warmup NaNs as True (fail-open until EMA is defined)
        ok = (merged["close"] > merged[f"ema{ema_period}"])
        ok = ok.fillna(True).astype(bool).to_numpy()
        # Index by open_time for fast lookup
        self._ok_lookup = dict(zip(primary["open_time"].to_list(), ok))
        self._current_time: pd.Timestamp | None = None

    def _get_4h_window(self, df_4h, current_time):
        # Stash for use in _generate_signal
        self._current_time = current_time
        return super()._get_4h_window(df_4h, current_time)

    def _generate_signal(self, window, window_4h):
        regime, signal = super()._generate_signal(window, window_4h)
        if signal.action != "BUY":
            return regime, signal
        ok = self._ok_lookup.get(self._current_time, True)
        if not ok:
            return regime, hold_signal(atr=signal.atr)
        return regime, signal


def run_one(label, df_4h, df_1d, df_1w, cfg, *, use_weekly=True, ema_period=None):
    if ema_period is None:
        engine = BacktestEngine(cfg)
    else:
        engine = MacroFilteredEngine(cfg, df_4h, df_1d, ema_period)
    res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w if use_weekly else None, symbol="BTCUSDT")
    s = engine.summary(res)
    yrs = years_span(df_4h)
    return {
        "label":  label,
        "ann":    annualize(s["total_pnl_pct"], yrs),
        "sharpe": s["sharpe_ratio"],
        "pf":     s["profit_factor"],
        "dd":     s["max_drawdown_pct"],
        "trades": s["total_trades"],
        "wr":     s["win_rate_pct"],
        "payoff": _payoff(res.trades),
        "streak": s["max_loss_streak"],
    }


def run_all() -> None:
    print("Loading cached klines...", flush=True)
    df_4h = load_cached("BTCUSDT", "4h")
    df_1d = load_cached("BTCUSDT", "1d")
    df_1w = load_cached("BTCUSDT", "1w")

    H1c = dict(
        momentum_filter_enabled=True,
        momentum_sma_period=20,
        momentum_neutral_band=0.08,
    )

    HYPS = []
    HYPS.append(("BASELINE",                          base_cfg(),                                  False, None))
    HYPS.append(("H1c risk 2%",                       base_cfg(**H1c),                             True,  None))
    HYPS.append(("H1c risk 4%",                       base_cfg(**H1c, risk_per_trade=0.04),        True,  None))
    # EMA filter sweep at risk 4%
    for ep in [50, 100, 150, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 4%)",       base_cfg(**H1c, risk_per_trade=0.04),        True,  ep))
    # EMA filter sweep at risk 5%
    for ep in [50, 100, 150, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 5%)",       base_cfg(**H1c, risk_per_trade=0.05),        True,  ep))
    # EMA filter at risk 6%
    for ep in [100, 150, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 6%)",       base_cfg(**H1c, risk_per_trade=0.06),        True,  ep))

    results = []
    for label, cfg, uw, ep in HYPS:
        r = run_one(label, df_4h, df_1d, df_1w, cfg, use_weekly=uw, ema_period=ep)
        results.append(r)
        print(f"  ✓ {label}", flush=True)

    print()
    hdr = (
        f"{'Hypothesis':<36} {'CAGR':>7} {'Sharpe':>7} "
        f"{'PF':>6} {'MaxDD':>7} {'Trades':>7} {'WR%':>6} {'Payoff':>7} {'Streak':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        ok = (r["ann"] > 22.57 and r["pf"] >= 1.3 and r["dd"] <= 25.0)
        flag = " ★" if ok else ""
        pf_str = f"{r['pf']:6.3f}" if r['pf'] != float('inf') else "   inf"
        po_str = f"{r['payoff']:6.3f}" if r['payoff'] != float('inf') else "   inf"
        print(
            f"{r['label']:<36} "
            f"{r['ann']:>6.2f}% "
            f"{r['sharpe']:>7.3f} "
            f"{pf_str} "
            f"{r['dd']:>6.2f}% "
            f"{r['trades']:>7d} "
            f"{r['wr']:>5.1f}% "
            f"{po_str} "
            f"{r['streak']:>7d}"
            f"{flag}"
        )

    out_path = Path(__file__).resolve().parent.parent.parent / "data" / "hypotheses_round3b.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    run_all()
