"""4h risk × momentum filter matrix (3yr backtest)."""
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.fetcher import fetch_historical_klines

logging.disable(logging.CRITICAL)
START = datetime(2022, 4, 1, tzinfo=timezone.utc)
END   = datetime(2025, 4, 1, tzinfo=timezone.utc)

print("Loading klines…", flush=True)
df    = fetch_historical_klines("BTCUSDT", "4h", START, END)
df1d  = fetch_historical_klines("BTCUSDT", "1d", START, END)
df1w  = fetch_historical_klines("BTCUSDT", "1w", START, END)

print(f"\n{'Risk %':>6} {'Mom8%':>6} {'CAGR':>7} {'PF':>6} {'DD':>7} {'Sharpe':>7} {'Trades':>7}")
print("-" * 60)

for risk in (0.02, 0.03, 0.04):
    for mom in (False, True):
        cfg = BacktestConfig(
            initial_capital=10000, risk_per_trade=risk,
            timeframe="4h",
            momentum_filter_enabled=mom,
            momentum_sma_period=20,
            momentum_neutral_band=0.08,
        )
        engine = BacktestEngine(cfg)
        res    = engine.run(df, df_4h=df1d, df_weekly=df1w, symbol="BTCUSDT")
        s      = engine.summary(res)
        net    = (res.final_capital - res.initial_capital) / res.initial_capital
        cagr   = (1 + net) ** (1 / 3) - 1
        pf     = s["profit_factor"]
        pf_str = f"{pf:.3f}" if pf != float("inf") else "  inf"
        label  = "on" if mom else "off"
        print(
            f"{risk*100:>5.1f}% {label:>6} "
            f"{cagr*100:>6.1f}% {pf_str:>6} {s['max_drawdown_pct']:>6.1f}% "
            f"{s['sharpe_ratio']:>7.2f} {s['total_trades']:>7}",
            flush=True,
        )
