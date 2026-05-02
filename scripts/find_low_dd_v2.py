#!/usr/bin/env python
"""Stage 2 DD-reduction: test bias-strict and per-symbol patterns.

Stage 1 (find_low_dd.py) showed that within (risk × SL × TP) space, the
baseline is already near-optimal — DD floor is structural at this profit
level. Stage 2 explores deeper levers:

1. bias_strict: block trades when daily bias is NEUTRAL (only trade with
   confirmed bullish/matching-direction bias).
2. Combinations of bias_strict with risk/SL/TP variants.

Hypothesis: in 2022 H1 (Luna crash, multi-period worst loser at -27.5% PnL,
20 trades 20% WR) the bias was bearish/neutral, but the bot still took 20
trades. bias_strict should kill those losers without affecting bull-market
performance.

Run:
    PYTHONPATH=. venv/bin/python scripts/find_low_dd_v2.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logging.getLogger("bot.bias.filter").setLevel(logging.ERROR)

from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig
from bot.backtest.portfolio_engine import PortfolioBacktestEngine
from bot.backtest.scenario_runner import compute_annual_return


@dataclass
class Config:
    label:     str
    risk:      float
    sl:        float
    tp:        float
    strict:    bool
    note:      str = ""


CONFIGS: list[Config] = [
    # Baseline (current): permissive bias
    Config("BASELINE",            0.04, 1.5,  4.5, False, "current target"),
    # Bias strict — only confirmed bullish trades
    Config("Strict bias",         0.04, 1.5,  4.5, True,  "skip NEUTRAL bias"),
    Config("Strict + R3%",        0.03, 1.5,  4.5, True,  "strict + lower risk"),
    Config("Strict + R2%",        0.02, 1.5,  4.5, True,  "very conservative"),
    Config("Strict + TP4.0",      0.04, 1.5,  4.0, True,  "strict + faster TP"),
    Config("Strict + TP4.0 R3%",  0.03, 1.5,  4.0, True,  "strict moderate"),
    Config("Strict + SL1.25",     0.04, 1.25, 4.5, True,  "strict tighter SL"),
]


@dataclass
class Result:
    config:    Config
    annual:    float
    dd:        float
    pf:        float
    trades:    int
    win_rate:  float

    @property
    def calmar(self) -> float:
        return (self.annual * 100.0) / self.dd if self.dd > 0 else 0.0


def _run(c: Config, dfs, dfs_bias, days: int) -> Result:
    cfg = BacktestConfig(
        initial_capital   = 10_000.0,
        risk_per_trade    = c.risk,
        timeframe         = "4h",
        long_only         = True,
        ema_stop_mult     = c.sl,
        ema_tp_mult       = c.tp,
        bias_strict       = c.strict,
    )
    engine = PortfolioBacktestEngine(cfg)
    pr     = engine.run_portfolio(dfs=dfs, dfs_4h=dfs_bias)

    annual = compute_annual_return(pr.initial_capital, pr.final_capital, days)
    ps     = pr.portfolio_summary
    total  = sum(len(ts) for ts in pr.per_symbol_trades.values())

    all_closed = []
    for ts in pr.per_symbol_trades.values():
        all_closed.extend([t for t in ts if t.get("exit_reason") is not None])
    wins = sum(1 for t in all_closed if (t.get("pnl") or 0) > 0)
    wr = (100.0 * wins / len(all_closed)) if all_closed else 0.0

    return Result(
        config=c, annual=annual,
        dd=ps.get("max_drawdown_pct", 0.0),
        pf=ps.get("profit_factor", 0.0),
        trades=total, win_rate=wr,
    )


def main() -> None:
    days     = 1095
    end_dt   = datetime.now(tz=timezone.utc)
    start_dt = end_dt - timedelta(days=days + 30)

    print(f"\n{'=' * 100}")
    print(f"  STAGE 2 — bias_strict variations on BTC+ETH portfolio (3y, 4h)")
    print(f"{'=' * 100}\n")

    print("Fetching data…")
    df_btc_4h = fetch_and_cache("BTCUSDT", "4h", start_dt, end_dt)
    df_btc_1d = fetch_and_cache("BTCUSDT", "1d", start_dt, end_dt)
    df_eth_4h = fetch_and_cache("ETHUSDT", "4h", start_dt, end_dt)
    df_eth_1d = fetch_and_cache("ETHUSDT", "1d", start_dt, end_dt)
    dfs      = {"BTCUSDT": df_btc_4h, "ETHUSDT": df_eth_4h}
    dfs_bias = {"BTCUSDT": df_btc_1d, "ETHUSDT": df_eth_1d}

    print(f"\nRunning {len(CONFIGS)} configurations…\n")
    results: list[Result] = []
    for i, c in enumerate(CONFIGS, 1):
        print(f"  [{i}/{len(CONFIGS)}] {c.label}…", flush=True)
        results.append(_run(c, dfs, dfs_bias, days))

    baseline = next(r for r in results if r.config.label == "BASELINE")

    print(f"\n{'─' * 100}")
    print("RESULTS — sorted by lowest DD")
    print("─" * 100)
    print(
        f"{'Configuration':<22} {'Risk':>5} {'SL':>5} {'TP':>5} {'Strict':>7} "
        f"{'Annual':>9} {'DD':>9} {'PF':>5} {'WR':>6} {'Trades':>7} {'Calmar':>7}  Δ-vs-base"
    )
    print("─" * 100)
    for r in sorted(results, key=lambda x: x.dd):
        d_ann = (r.annual - baseline.annual) * 100
        d_dd  = r.dd - baseline.dd
        marker = "  ← BASELINE" if r is baseline else ""
        print(
            f"{r.config.label:<22} {r.config.risk*100:>4.0f}% {r.config.sl:>5.2f} {r.config.tp:>5.2f} "
            f"{('YES' if r.config.strict else ' no'):>7} "
            f"{r.annual*100:>+8.1f}% -{abs(r.dd):>6.1f}% {r.pf:>5.2f} {r.win_rate:>5.1f}% {r.trades:>7} "
            f"{r.calmar:>7.2f}  ann{d_ann:>+5.1f}pp dd{d_dd:>+5.1f}pp{marker}"
        )

    # ── Verdicts focusing on "preserve profit" constraint ─────────────────────
    print(f"\n{'=' * 100}")
    print("VERDICTS (preserve-profit constraint: annual loss ≤ 10pp)")
    print(f"{'=' * 100}\n")

    preserving = [
        r for r in results
        if r is not baseline
        and (r.annual - baseline.annual) * 100 >= -10
    ]
    if preserving:
        print(f"  Configs that preserve profit (≤10pp annual loss):")
        for r in sorted(preserving, key=lambda x: x.dd):
            d_ann = (r.annual - baseline.annual) * 100
            d_dd  = r.dd - baseline.dd
            print(
                f"    {r.config.label:<22}  Ann={r.annual*100:>+5.1f}% (Δ{d_ann:+.1f}pp)  "
                f"DD=-{r.dd:>4.1f}% (Δ{d_dd:+.1f}pp)  Calmar={r.calmar:.2f}"
            )

        # Best DD-reducing config that still preserves profit
        best = min(preserving, key=lambda r: r.dd)
        d_ann = (best.annual - baseline.annual) * 100
        d_dd  = best.dd - baseline.dd
        print(f"\n  ★ Best DD reduction within profit constraint:")
        print(f"     {best.config.label}  →  Ann={best.annual*100:+.1f}%  DD=-{best.dd:.1f}%  Calmar={best.calmar:.2f}")
        print(f"     vs baseline: Δann={d_ann:+.1f}pp, ΔDD={d_dd:+.1f}pp")
    else:
        print(f"  No config preserves profit AND reduces DD. Trade-off is structural.")

    print()


if __name__ == "__main__":
    main()
