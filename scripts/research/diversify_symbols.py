"""Symbol diversification — does EMA-prod work on more pairs than just BTC+ETH?

Hypothesis: adding more uncorrelated symbols (SOL, BNB, AVAX) increases trade
frequency and dampens single-symbol drought. Same EMA-prod config, same 4h, same
risk, but pool USDT across symbols.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.ERROR)
for mod in ("bot.bias.filter", "bot.strategy", "bot.orchestrator", "bot.regime"):
    logging.getLogger(mod).setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine


CAPITAL = 10_000.0
RISK    = 0.02
COST    = 0.001
END_DT  = datetime(2026, 5, 17, tzinfo=timezone.utc)


def run(symbols, days, *, momentum=True, use_bias=True):
    start = END_DT - timedelta(days=days)
    weekly_start = start - timedelta(days=154)
    dfs    = {s: fetch_and_cache(s, "4h", start, END_DT) for s in symbols}
    dfs_1d = {s: fetch_and_cache(s, "1d", start, END_DT) for s in symbols} if use_bias else None
    dfs_w  = {s: fetch_and_cache(s, "1w", weekly_start, END_DT) for s in symbols} if momentum else None
    cfg = BacktestConfig(
        initial_capital   = CAPITAL,
        risk_per_trade    = RISK,
        timeframe         = "4h",
        cost_per_side_pct = COST,
        long_only         = True,
        ema_stop_mult     = 1.5,
        ema_tp_mult       = 5.0,
        ema_max_distance_atr = 1.0,
        ema_volume_mult   = 1.5,
        ema_require_bar_dir  = True,
        ema_require_momentum = True,
        ema_min_atr_pct   = 0.005,
        momentum_filter_enabled = momentum,
        momentum_neutral_band   = 0.08,
    )
    engine = PortfolioBacktestEngine(cfg)
    r = engine.run_portfolio(dfs, dfs_4h=dfs_1d, dfs_weekly=dfs_w)
    s = r.portfolio_summary
    annual = (s["total_pnl_pct"] / days) * 365
    dd = abs(s["max_drawdown_pct"])
    calmar = annual/dd if dd else float("nan")
    return s["profit_factor"], annual, s["total_pnl_pct"], dd, s["total_trades"], s["win_rate_pct"], calmar, r.per_symbol_summary


def main():
    print(f"Risk={RISK*100:.0f}% Cost={COST*100:.2f}%/side Capital=${CAPITAL:,.0f}", flush=True)
    cohorts = [
        ("BTC+ETH",            ["BTCUSDT", "ETHUSDT"]),
        ("BTC+ETH+SOL",        ["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
        ("BTC+ETH+SOL+BNB",    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]),
        ("BTC+ETH+SOL+BNB+AVAX", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT"]),
    ]
    for days, tag in [(180, "6 MONTHS"), (1095, "3 YEARS")]:
        print(f"\n=== {tag} ===", flush=True)
        print(f"{'Cohort':<25s} | {'PF':>5s} | {'Annual':>8s} | {'Total':>9s} | {'DD':>5s} | {'Tr':>3s} | {'WR':>6s} | {'Cm':>5s}")
        print("-" * 90)
        for name, syms in cohorts:
            try:
                pf, ann, tot, dd, tr, wr, cm, per = run(syms, days)
                print(f"{name:<25s} | {pf:>5.2f} | {ann:>+7.1f}% | {tot:>+8.1f}% | {dd:>4.1f}% | {tr:>3d} | {wr:>5.1f}% | {cm:>5.2f}", flush=True)
            except Exception as e:
                print(f"{name:<25s} | ERROR: {e}")


if __name__ == "__main__":
    main()
