"""One-off diagnostic for the May-21..25 losing streak.

For every closed trade in the DB:
  1. Recompute what BiasFilter would have returned at the entry candle on the
     bias timeframe (1d for primary 4h).
  2. Recompute regime classification on the primary timeframe.
  3. Show whether the entry would have been blocked under the validated
     baseline (neutral_passthrough=False) but allowed under the current
     runtime config (neutral_passthrough=True).

Reads:
  /app/data/trading_bot.db            (trades, bot_config)
  /app/data/klines/{SYMBOL}_{TF}.parquet  (OHLCV cache)
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, "/app")

from bot.bias.filter import Bias, BiasFilter, BiasFilterConfig
from bot.config_presets import bias_timeframe_for, get_regime_config
from bot.regime.detector import RegimeDetector

DB = Path("/app/data/trading_bot.db")
KLINES = Path("/app/data/klines")


def load_klines(symbol: str, tf: str) -> pd.DataFrame:
    df = pd.read_parquet(KLINES / f"{symbol}_{tf}.parquet")
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def slice_until(df: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    """Return rows with index strictly < entry timestamp (avoid look-ahead)."""
    ts = pd.Timestamp(ts, tz="UTC") if ts.tzinfo is None else ts
    return df.loc[df.index < ts]


def main() -> None:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute(
        """SELECT id, symbol, side, entry_price, exit_price, pnl, pnl_pct,
                  entry_time, exit_time, exit_reason, stop_loss, take_profit, atr, timeframe
           FROM trades
           ORDER BY id"""
    )
    trades = [dict(r) for r in cur.fetchall()]
    print(f"Loaded {len(trades)} trades from DB")

    bias_strict = BiasFilter(BiasFilterConfig(neutral_passthrough=False))
    bias_passthrough = BiasFilter(BiasFilterConfig(neutral_passthrough=True))

    for t in trades:
        tf_primary = t["timeframe"] or "4h"
        tf_bias = bias_timeframe_for(tf_primary)
        entry_ts = pd.Timestamp(t["entry_time"], tz="UTC")

        print()
        print("=" * 100)
        print(
            f"Trade #{t['id']} {t['symbol']} {t['side']} entry={t['entry_price']} "
            f"@ {entry_ts}  exit={t['exit_price']} pnl={t['pnl']} reason={t['exit_reason']}"
        )

        # ── Bias filter ────────────────────────────────────────────────────
        df_bias = load_klines(t["symbol"], tf_bias)
        df_bias_at_entry = slice_until(df_bias, entry_ts)
        if len(df_bias_at_entry) < 22:
            print(f"  ⚠ not enough {tf_bias} bias bars at entry: {len(df_bias_at_entry)}")
            continue

        b_strict = bias_strict.get_bias(df_bias_at_entry.tail(50))
        b_pass = bias_passthrough.get_bias(df_bias_at_entry.tail(50))
        last = df_bias_at_entry.iloc[-1]
        ema9 = df_bias_at_entry["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema21 = df_bias_at_entry["close"].ewm(span=21, adjust=False).mean().iloc[-1]
        gap_pct = (ema9 - ema21) / last["close"] * 100

        print(
            f"  Bias ({tf_bias}): last_close={last['close']:.2f}  "
            f"ema9={ema9:.2f}  ema21={ema21:.2f}  gap={gap_pct:+.3f}%"
        )
        print(f"    strict (passthrough=False):       {b_strict.value}")
        print(f"    current (passthrough=True):       {b_pass.value}")

        # Would strict have blocked the BUY?
        from bot.strategy.base import Signal
        fake_signal = Signal(
            action=t["side"], strength=0.6,
            stop_loss=t["stop_loss"], take_profit=t["take_profit"], atr=t["atr"],
        )
        strict_allow = bias_strict.allows_signal(fake_signal, b_strict)
        pass_allow = bias_passthrough.allows_signal(fake_signal, b_pass)
        verdict = (
            "✅ identical" if strict_allow == pass_allow
            else ("🚨 PASSTHROUGH ALLOWED, STRICT BLOCKED" if pass_allow else "??")
        )
        print(f"    strict allows {t['side']}?           {strict_allow}")
        print(f"    current allows {t['side']}?          {pass_allow}    {verdict}")

        # ── Regime ─────────────────────────────────────────────────────────
        try:
            df_primary = load_klines(t["symbol"], tf_primary)
            df_primary_at_entry = slice_until(df_primary, entry_ts)
            regime_cfg = get_regime_config(tf_primary)
            detector = RegimeDetector(regime_cfg)
            regime = detector.detect(df_primary_at_entry.tail(150))
            print(f"  Regime ({tf_primary}): {regime.value}")
        except Exception as e:
            print(f"  Regime: error {e}")


if __name__ == "__main__":
    main()
