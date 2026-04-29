"""Backtest runner — fetches real historical data and runs a full simulation.

Usage (from the project root):

    python -m bot.backtest.runner                    # BTC 1h, last 6 months
    python -m bot.backtest.runner --symbol ETHUSDT   # different pair
    python -m bot.backtest.runner --timeframe 15m    # 15-minute bars
    python -m bot.backtest.runner --months 3         # shorter period
    python -m bot.backtest.runner --no-bias          # skip 4h BiasFilter

The script prints a full performance report and exits with:
    0  — backtest completed (results may be good or bad)
    1  — data fetch error or insufficient data
"""

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.fetcher import fetch_historical_klines

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest.runner")

# Silence noisy indicator/strategy loggers (keep backtest-level info only)
for _noisy in ("bot.regime", "bot.strategy", "bot.bias", "bot.indicators"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── Report helpers ────────────────────────────────────────────────────────────

_SEP  = "─" * 62
_SEP2 = "═" * 62

def _fmt(value: float, decimals: int = 2, prefix: str = "", suffix: str = "") -> str:
    return f"{prefix}{value:,.{decimals}f}{suffix}"

def _sign(value: float) -> str:
    return "+" if value >= 0 else ""

def _row(label: str, value: str, width: int = 36) -> str:
    return f"  {label:<{width}}{value}"


def print_report(result, summary: dict, symbol: str, timeframe: str) -> None:
    """Print a human-readable performance report to stdout."""
    total_pnl     = summary["total_pnl"]
    total_pnl_pct = summary["total_pnl_pct"]
    win_rate      = summary["win_rate_pct"]
    sharpe        = summary["sharpe_ratio"]
    drawdown      = summary["max_drawdown_pct"]
    pf            = summary["profit_factor"]
    streak        = summary["max_loss_streak"]
    best          = summary["best_trade_pnl"]
    worst         = summary["worst_trade_pnl"]
    n_closed      = summary["total_trades"]
    n_open        = summary["open_at_period_end"]
    ic            = result.initial_capital
    fc            = result.final_capital

    print()
    print(_SEP2)
    print(f"  BACKTEST REPORT — {symbol}  [{timeframe}]")
    print(_SEP2)
    print(f"  Period      : {result.start_date}  →  {result.end_date}")
    print(f"  Total bars  : {result.total_bars:,}")
    print(_SEP)

    # Capital
    print(f"  {'Initial capital':<36}${_fmt(ic)}")
    print(f"  {'Final capital':<36}${_fmt(fc)}")
    pnl_sign = _sign(total_pnl)
    print(f"  {'Net PnL':<36}{pnl_sign}${_fmt(abs(total_pnl))}  ({pnl_sign}{_fmt(total_pnl_pct)}%)")
    print(_SEP)

    # Trade counts
    print(f"  {'Closed trades':<36}{n_closed}")
    print(f"  {'Open at period end':<36}{n_open}")
    print(_SEP)

    # Performance metrics
    print(f"  {'Win rate':<36}{_fmt(win_rate)}%")
    print(f"  {'Sharpe ratio (annualised)':<36}{_fmt(sharpe, 3)}")
    print(f"  {'Max drawdown':<36}{_fmt(drawdown)}%")
    print(f"  {'Profit factor':<36}{_fmt(pf, 3) if pf != float('inf') else '∞'}")
    print(f"  {'Max consecutive losses':<36}{streak}")
    print(_SEP)

    # Trade extremes
    print(f"  {'Best trade PnL':<36}+${_fmt(best)}")
    print(f"  {'Worst trade PnL':<36}-${_fmt(abs(worst))}")
    print(_SEP)

    # Verdict
    passed, notes = _verdict(summary)
    status = "PASS ✓" if passed else "NEEDS REVIEW !"
    print(f"  Verdict: {status}")
    for note in notes:
        print(f"    • {note}")
    print(_SEP2)
    print()


def _verdict(summary: dict) -> tuple[bool, list[str]]:
    """Return (passed, [notes]) based on minimum viability thresholds."""
    notes: list[str] = []
    passed = True

    win_rate = summary["win_rate_pct"]
    sharpe   = summary["sharpe_ratio"]
    drawdown = summary["max_drawdown_pct"]
    pf       = summary["profit_factor"]
    n        = summary["total_trades"]

    if n < 20:
        notes.append(f"Too few trades ({n}) — results not statistically significant")
        passed = False
    if win_rate < 30.0:
        notes.append(f"Win rate {win_rate:.1f}% is below 30% threshold")
        passed = False
    elif win_rate < 40.0:
        notes.append(f"Win rate {win_rate:.1f}% is below 40% (acceptable if PF and Sharpe compensate)")
    if sharpe < 0.5:
        notes.append(f"Sharpe {sharpe:.2f} is below 0.5 — poor risk-adjusted returns")
        passed = False
    if drawdown > 25.0:
        notes.append(f"Max drawdown {drawdown:.1f}% exceeds 25% risk limit")
        passed = False
    elif drawdown > 20.0:
        notes.append(f"Max drawdown {drawdown:.1f}% exceeds 20% (acceptable if PF > 1.2)")
    if pf != float("inf") and pf < 1.1:
        notes.append(f"Profit factor {pf:.2f} is below 1.1 (marginal edge)")
        passed = False
    elif pf != float("inf") and pf < 1.2:
        notes.append(f"Profit factor {pf:.2f} is below 1.2 (solid but not great)")

    if passed:
        notes.append("All minimum viability thresholds met")

    return passed, notes


# ── Main ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a backtest on real Binance historical data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol",     default="BTCUSDT", help="Binance trading pair")
    p.add_argument("--timeframe",  default="1h",      help="Kline interval (e.g. 1h, 15m)")
    p.add_argument("--months",     type=int, default=6, help="Look-back period in months (ignored if --start given)")
    p.add_argument("--start",      default=None, help="Start date YYYY-MM-DD (UTC)")
    p.add_argument("--end",        default=None, help="End date YYYY-MM-DD (UTC, default: today)")
    p.add_argument("--capital",    type=float, default=10_000.0, help="Starting capital (USDT)")
    p.add_argument("--risk",       type=float, default=0.01, help="Risk per trade (fraction)")
    p.add_argument("--no-bias",       action="store_true", help="Disable BiasFilter")
    p.add_argument("--bias-tf",       default=None,
                   help="Timeframe for BiasFilter data (e.g. 1d, 4h). Default: one step up from primary TF.")
    p.add_argument("--cost",          type=float, default=0.0015,
                   help="Cost per side as a fraction (slippage + commission)")
    p.add_argument("--min-strength",  type=float, default=0.5,
                   help="Minimum signal strength to enter a trade (0.0–1.0)")
    p.add_argument("--force-strategy",  default=None,
                   help="Override regime→strategy map, e.g. EMA_CROSSOVER")
    p.add_argument("--skip-ranging",    action="store_true",
                   help="Skip new entries during RANGING regime (trade only TRENDING)")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    end_dt = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if args.end
        else datetime.now(tz=timezone.utc)
    )
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start_dt = end_dt - timedelta(days=args.months * 30)

    # ── Determine bias timeframe ─────────────────────────────────────────────
    _BIAS_DEFAULTS = {"1h": "4h", "15m": "1h", "4h": "1d", "1d": "1w"}
    bias_tf = args.bias_tf or _BIAS_DEFAULTS.get(args.timeframe, "4h")

    logger.info(
        "Backtest: %s  %s  %s → %s  capital=%.0f  risk=%.2f%%  bias=%s  min_strength=%.2f  strategy=%s",
        args.symbol, args.timeframe,
        start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"),
        args.capital, args.risk * 100,
        "OFF" if args.no_bias else f"{bias_tf} EMA",
        args.min_strength,
        args.force_strategy or "regime-based",
    )

    # ── Fetch primary timeframe data ─────────────────────────────────────────
    logger.info("Fetching %s %s klines…", args.symbol, args.timeframe)
    try:
        df = fetch_historical_klines(args.symbol, args.timeframe, start_dt, end_dt)
    except Exception as exc:
        logger.error("Failed to fetch %s %s data: %s", args.symbol, args.timeframe, exc)
        return 1

    # ── Fetch bias data for BiasFilter (unless disabled) ─────────────────────
    df_4h = None
    if not args.no_bias:
        logger.info("Fetching %s %s klines for BiasFilter…", args.symbol, bias_tf)
        try:
            df_4h = fetch_historical_klines(args.symbol, bias_tf, start_dt, end_dt)
        except Exception as exc:
            logger.warning(
                "Could not fetch %s data (%s) — running without BiasFilter", bias_tf, exc
            )

    # ── Run engine ───────────────────────────────────────────────────────────
    cfg = BacktestConfig(
        initial_capital   = args.capital,
        risk_per_trade    = args.risk,
        timeframe         = args.timeframe,
        cost_per_side_pct = args.cost,
        min_signal_strength = args.min_strength,
    )
    engine = BacktestEngine(cfg)

    logger.info("Running simulation…")
    try:
        result = engine.run(df, df_4h=df_4h, symbol=args.symbol)
    except ValueError as exc:
        logger.error("Simulation error: %s", exc)
        return 1

    summary = engine.summary(result)
    print_report(result, summary, args.symbol, args.timeframe)

    return 0


if __name__ == "__main__":
    sys.exit(main())
