"""Official-engine validation of the chandelier exit vs the old fixed TP (gotcha #50).

    PYTHONPATH=. python scripts/validate_trailing_exit_2026_10.py <full2|full3> <old|new> <out.json>

Runs `PortfolioBacktestEngine` through `build_backtest_config` (live parity,
weekly momentum ON as live) on the look-ahead-free engine (gotcha #49) and
dumps the equity curve + trades. ~10 min per run on this host (the engine
recomputes indicators over the whole history every bar). Results:
docs/audits/trailing_exit_2026-10-12.md.
"""
import sys, json, logging
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
logging.disable(logging.WARNING)
from datetime import datetime, timezone
import stress_test_2026 as st
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.portfolio_runner import BacktestRequest, build_backtest_config
w, label, out = sys.argv[1], sys.argv[2], sys.argv[3]
CFG = {"old": {"ema_stop_mult": "1.5", "ema_tp_mult": "5.0", "ema_trail_mult": "0.0"},
       "new": {"ema_stop_mult": "2.0", "ema_tp_mult": "0.0", "ema_trail_mult": "4.0"}}[label]
st._init_worker()
data = st._window_data(w)
cfg = dict(st.LIVE_RUNTIME_CFG); cfg.update(CFG)
req = BacktestRequest(symbols=tuple(data), timeframe="4h", start_dt=datetime(2017,8,1,tzinfo=timezone.utc),
    end_dt=datetime.now(timezone.utc), capital=10000.0, risk=0.015, cost_per_side=0.001,
    use_bias=True, use_momentum=True, use_1m=False)
pr = PortfolioBacktestEngine(config=build_backtest_config(req, cfg)).run_portfolio(
    dfs={s: d[0] for s, d in data.items()}, dfs_4h={s: d[1] for s, d in data.items()},
    dfs_weekly={s: d[2] for s, d in data.items()})
trades = [dict(t, symbol=s) for s, ts in pr.per_symbol_trades.items() for t in ts]
json.dump({"curve": pr.combined_equity_curve, "trades": trades, "summary": pr.portfolio_summary}, open(out, "w"), default=str)
print("done", w, label, round(pr.final_capital, 2), flush=True)
