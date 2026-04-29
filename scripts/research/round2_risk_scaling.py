"""Round 2: H1 weekly-momentum filter + risk scaling.

H1 alone gave CAGR 19.88% / MaxDD 12.58% (vs baseline 22.57% / 20.51%).
DD headroom suggests we can lift risk without breaching the 25% MaxDD ceiling
while keeping PF ≥ 1.3.

Tests four risk levels on H1 (band 5%) and H1c (band 8%) — picks the sweet spot.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.backtest.engine import BacktestConfig, BacktestEngine
from scripts.research.hypotheses import (
    annualize, base_cfg, load_cached, run_one, years_span,
)

logging.disable(logging.CRITICAL)


def run_all() -> None:
    print("Loading cached klines...", flush=True)
    df_4h = load_cached("BTCUSDT", "4h")
    df_1d = load_cached("BTCUSDT", "1d")
    df_1w = load_cached("BTCUSDT", "1w")

    HYPS = []
    # Risk sweep on H1 (band 5%, default)
    for risk in [0.02, 0.025, 0.03, 0.035, 0.04]:
        HYPS.append((
            f"H1+risk={risk*100:.1f}%",
            {
                "risk_per_trade": risk,
                "momentum_filter_enabled": True,
                "momentum_sma_period": 20,
                "momentum_neutral_band": 0.05,
            },
            True,
        ))
    # Risk sweep on H1c (band 8%, slightly more permissive)
    for risk in [0.02, 0.025, 0.03, 0.035, 0.04]:
        HYPS.append((
            f"H1c+risk={risk*100:.1f}%",
            {
                "risk_per_trade": risk,
                "momentum_filter_enabled": True,
                "momentum_sma_period": 20,
                "momentum_neutral_band": 0.08,
            },
            True,
        ))
    # Reference: baseline at same risk levels (no momentum filter)
    for risk in [0.02, 0.025, 0.03]:
        HYPS.append((
            f"BASE+risk={risk*100:.1f}% (no filter)",
            {"risk_per_trade": risk},
            False,
        ))

    results = []
    for label, overrides, use_weekly in HYPS:
        cfg = base_cfg(**overrides)
        r = run_one(label, df_4h, df_1d, df_1w, cfg, use_weekly=use_weekly)
        results.append(r)
        print(f"  ✓ {label}", flush=True)

    print()
    hdr = (
        f"{'Hypothesis':<36} {'CAGR':>7} {'Sharpe':>7} "
        f"{'PF':>6} {'MaxDD':>7} {'Trades':>7} {'WR%':>6} {'Payoff':>7}"
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
            f"{po_str}"
            f"{flag}"
        )

    out = []
    for r in results:
        out.append({k: v for k, v in r.items() if k != "trades_obj"})
    out_path = Path(__file__).resolve().parent.parent / "data" / "hypotheses_round2.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nResults written to {out_path}")


if __name__ == "__main__":
    run_all()
