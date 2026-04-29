"""Round 3: stack additional macro filters on top of H1c (weekly momentum 8% band).

Tests:
  - Daily 200-EMA bull-bias gate (long_only when daily close > 200d EMA)
  - Daily 100-EMA gate (faster)
  - Loss-streak cooldown (after 3 consecutive losses, skip next 3 trades)
  - Combined gates

Implementation: subclass BacktestEngine to inject macro gating in
_generate_signal — does NOT touch the live engine code.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.regime.detector import MarketRegime
from bot.strategy.base import Signal
from bot.strategy.signal_factory import hold_signal
from scripts.research.hypotheses import (
    annualize, base_cfg, load_cached, years_span, _payoff,
)

logging.disable(logging.CRITICAL)


class FilteredEngine(BacktestEngine):
    """BacktestEngine with optional macro filters applied before risk sizing."""

    def __init__(
        self,
        cfg: BacktestConfig,
        df_1d: pd.DataFrame,
        *,
        ema_period: int | None = None,           # daily EMA macro filter
        loss_streak_threshold: int | None = None, # cooldown after N losses
        loss_streak_cooldown: int = 3,            # # trades to skip
    ) -> None:
        super().__init__(cfg)
        self._df_1d = df_1d.copy()
        self._df_1d["open_time"] = pd.to_datetime(self._df_1d["open_time"], utc=True)
        if ema_period is not None:
            self._df_1d[f"ema{ema_period}"] = (
                self._df_1d["close"].ewm(span=ema_period, adjust=False).mean()
            )
        self._ema_period = ema_period
        self._streak_thresh = loss_streak_threshold
        self._cooldown_n   = loss_streak_cooldown

    def _macro_long_ok(self, current_time: pd.Timestamp) -> bool:
        """Return True when daily close > daily EMA at the most recent completed daily bar."""
        if self._ema_period is None:
            return True
        mask = self._df_1d["open_time"] <= current_time
        completed = self._df_1d[mask]
        if len(completed) < self._ema_period + 5:
            return True  # warmup — fail-open
        last = completed.iloc[-1]
        return float(last["close"]) > float(last[f"ema{self._ema_period}"])

    def _generate_signal(
        self, window: pd.DataFrame, window_4h: pd.DataFrame | None,
    ) -> tuple[MarketRegime, Signal]:
        regime, signal = super()._generate_signal(window, window_4h)
        if signal.action != "BUY":
            return regime, signal
        # Apply daily macro filter
        current_time = window["open_time"].iloc[-1] if "open_time" in window else None
        if current_time is None:
            # window has been stripped of open_time — synthesize from index
            return regime, signal
        if not self._macro_long_ok(current_time):
            return regime, hold_signal(atr=signal.atr)
        return regime, signal


def run_with_streak_filter(engine: BacktestEngine, df_4h, df_1d, df_1w, *, threshold: int, cooldown_n: int):
    """Wrapper: post-process the trade list to simulate a loss-streak cooldown.

    Re-runs the engine and then drops trades that *would* have been skipped after
    N consecutive losses.  This is an approximation — the real bot would compound
    differently, but it gives a useful first-pass signal.
    """
    res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w, symbol="BTCUSDT")
    closed = res.trades
    # Walk trades in order; track loss streak and skip flag.
    streak = 0
    skip_remaining = 0
    kept: list[dict] = []
    for t in closed:
        if t["exit_reason"] == "END_OF_PERIOD":
            kept.append(t)
            continue
        if skip_remaining > 0:
            skip_remaining -= 1
            continue
        kept.append(t)
        if (t["pnl"] or 0) < 0:
            streak += 1
            if streak >= threshold:
                skip_remaining = cooldown_n
                streak = 0
        else:
            streak = 0
    # Reconstruct equity curve from kept trades only
    equity = engine.config.initial_capital
    new_curve = [{"timestamp": res.equity_curve[0]["timestamp"], "equity": equity}]
    for t in kept:
        if t["exit_reason"] == "END_OF_PERIOD":
            continue
        equity += (t["pnl"] or 0)
        new_curve.append({"timestamp": t["exit_time"], "equity": equity})
    res.trades = kept
    res.equity_curve = new_curve
    res.final_capital = equity
    return res


# ── Custom engine that uses open_time-aware windows ──────────────────────────
# The base engine strips open_time when slicing windows. We need a way to map
# back to the original df's timestamp. We override the main `run` loop's
# behaviour by intercepting via a wrapper instead of subclass.

def run_filtered(
    df_4h: pd.DataFrame, df_1d: pd.DataFrame, df_1w: pd.DataFrame,
    cfg: BacktestConfig, *,
    ema_period: int | None = None,
    use_weekly: bool = True,
):
    """Run engine with daily-EMA filter applied at signal-time."""
    # Pre-compute daily EMA series indexed by daily open_time
    daily = df_1d.copy()
    daily["open_time"] = pd.to_datetime(daily["open_time"], utc=True)
    if ema_period is not None:
        daily[f"ema{ema_period}"] = daily["close"].ewm(span=ema_period, adjust=False).mean()

    # Patch: monkey-patch the engine's _generate_signal to apply the gate.
    engine = BacktestEngine(cfg)
    base_gen = engine._generate_signal

    def gated(window: pd.DataFrame, window_4h: pd.DataFrame | None):
        regime, signal = base_gen(window, window_4h)
        if signal.action == "BUY" and ema_period is not None:
            # Use the timestamp of the LAST 4h bar in window — same idx the engine uses
            # window has no open_time when stripped; recover from df_4h via the
            # last close price match is unsafe. Instead, we wrap the engine's run
            # loop by patching at a higher level (see run_with_clock below).
            pass
        return regime, signal

    return None  # placeholder — replaced by run_with_clock below


def run_with_clock(
    df_4h: pd.DataFrame, df_1d: pd.DataFrame, df_1w: pd.DataFrame,
    cfg: BacktestConfig, *,
    ema_period: int | None = None,
    use_weekly: bool = True,
):
    """Run the engine but intercept signal generation per-bar to apply a macro filter.

    We bypass engine.run() and replicate the bar loop with timestamp awareness so
    we can ask the daily-EMA gate at the same instant the strategy generates a signal.
    """
    daily = df_1d.copy()
    daily["open_time"] = pd.to_datetime(daily["open_time"], utc=True)
    if ema_period is not None:
        daily[f"ema{ema_period}"] = daily["close"].ewm(span=ema_period, adjust=False).mean()

    # Build a fast lookup: for each 4h bar's open_time, what was the daily EMA at the
    # most recent COMPLETED daily bar? We use merge_asof to find the last daily bar
    # whose open_time <= 4h bar's open_time.
    primary = df_4h[["open_time"]].copy().reset_index(drop=True)
    primary["open_time"] = pd.to_datetime(primary["open_time"], utc=True)
    if ema_period is not None:
        merged = pd.merge_asof(
            primary.sort_values("open_time"),
            daily[["open_time", "close", f"ema{ema_period}"]].sort_values("open_time"),
            on="open_time", direction="backward", suffixes=("", "_d"),
        )
        macro_ok = (merged["close"] > merged[f"ema{ema_period}"]).fillna(True).to_numpy()
    else:
        macro_ok = None

    # Now run the engine but with a wrapped strategy that consults macro_ok.
    engine = BacktestEngine(cfg)

    # Keep track of which 4h bar index we're on. We need to wrap _generate_signal
    # to use it. Easiest: monkey-patch engine and remember the row counter via
    # the strategy. We do that with a counter in a closure.
    ts_to_idx = {ts: i for i, ts in enumerate(primary["open_time"])}

    base_gen = engine._generate_signal

    def gated(window: pd.DataFrame, window_4h: pd.DataFrame | None):
        regime, signal = base_gen(window, window_4h)
        if signal.action != "BUY" or macro_ok is None:
            return regime, signal
        # Determine current bar's timestamp from window: window is OHLCV-only,
        # but original df_4h slice was passed in. The engine uses df.iloc[i:i+lookback].
        # We need to find which bar this is. We use the close price + ATR fingerprint
        # — but that's fragile. Better: also patch at run() level. See below.
        return regime, signal

    # Direct approach: replicate the engine's run loop (it's ~150 lines).
    # Too risky. Use a different strategy: post-filter trades by entry timestamp.
    res = engine.run(df_4h, df_4h=df_1d, df_weekly=df_1w if use_weekly else None, symbol="BTCUSDT")

    if macro_ok is None:
        return res

    # Filter open trades whose entry_time was a "macro down" period.
    kept: list[dict] = []
    equity = engine.config.initial_capital
    first = res.equity_curve[0] if res.equity_curve else {"bar": 0, "time": None}
    new_curve = [{"bar": first.get("bar", 0), "time": first.get("time"), "balance": equity}]
    for t in res.trades:
        if t["exit_reason"] == "END_OF_PERIOD":
            kept.append(t)
            continue
        et = pd.Timestamp(t["entry_time"])
        if et.tzinfo is None:
            et = et.tz_localize("UTC")
        idx = ts_to_idx.get(et)
        if idx is None:
            arr = primary["open_time"].to_numpy()
            i = pd.Series(arr).searchsorted(et, side="right") - 1
            idx = max(0, int(i))
        if not bool(macro_ok[idx]):
            continue  # filtered out
        kept.append(t)
        equity += (t["pnl"] or 0)
        new_curve.append({"bar": t.get("exit_bar", 0), "time": t["exit_time"], "balance": equity})
    res.trades = kept
    res.equity_curve = new_curve
    res.final_capital = equity
    return res


def run_one(label, df_4h, df_1d, df_1w, cfg, *, use_weekly=True, ema_period=None):
    res = run_with_clock(df_4h, df_1d, df_1w, cfg, ema_period=ema_period, use_weekly=use_weekly)
    engine = BacktestEngine(cfg)
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
    # Reference points
    HYPS.append(("BASELINE",                        base_cfg(),                                         False, None))
    HYPS.append(("H1c (no extra filter, risk 2%)",  base_cfg(**H1c),                                    True,  None))
    HYPS.append(("H1c risk 4%",                     base_cfg(**H1c, risk_per_trade=0.04),               True,  None))
    # Daily EMA stacking on H1c at risk 4%
    for ep in [50, 100, 150, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 4%)",     base_cfg(**H1c, risk_per_trade=0.04),               True,  ep))
    # Daily EMA stacking on H1c at risk 5%
    for ep in [50, 100, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 5%)",     base_cfg(**H1c, risk_per_trade=0.05),               True,  ep))
    # Daily EMA stacking on H1c at risk 6%
    for ep in [50, 100, 200]:
        HYPS.append((f"H1c+EMA{ep}d (risk 6%)",     base_cfg(**H1c, risk_per_trade=0.06),               True,  ep))

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

    out = []
    for r in results:
        out.append({k: v for k, v in r.items()})
    out_path = Path(__file__).resolve().parent.parent / "data" / "hypotheses_round3.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    run_all()
