#!/usr/bin/env python
"""Do the shipped-but-disabled bear gates actually help? — hypothesis test.

Motivated by two independent observations on 2026-08-15:

* `crypto-regime-analyzer` scores the market RISK_OFF 37.4/100: BTC in a bear
  stack (price < 50DMA < 200DMA, 200DMA falling), 49.5% off the 1y high, only
  35% of the top-20 positive over 30d.
* Live has taken 12 closed trades since July for 11 stop-outs (PF 0.22). The bot
  keeps opening longs into that bear stack.

The strategy already ships two gates that would bite here, and BOTH are OFF in
the live `bot_config`:

    ema_require_ema200  = "false"   → EMACrossoverConfig.require_ema200_alignment
    ema_min_entry_adx   = "0.0"     → EMACrossoverConfig.min_entry_adx

This does NOT tune them — tuning on the current bear is exactly the curve-fit
CLAUDE.md warns about ("do not 'fix' the negative 12m by re-optimising on it").
It asks the falsifiable question: does either gate improve Calmar over the FULL
multi-regime history, or does it only rescue the recent bear at the cost of the
2020-21 and 2023-24 bulls, where a long-only trend follower earns its keep?

**A gate that only helps in the bear is regime-fitting and must be rejected.**

Note when reading results: `min_entry_adx` gates only *continuation* entries in
`EMACrossoverStrategy.generate_signal`, not crossover entries, so it is the
weaker lever by construction.

Run:
    PYTHONPATH=. python scripts/test_bear_filters.py
"""

from __future__ import annotations

import json
from multiprocessing import Pool

from scripts.stress_test_2026 import (
    WORKERS, _execute, _init_worker, head, hdr, row, to_run,
)

VARIANTS = {
    "baseline (both off)": {},
    "EMA200 alignment ON": {"ema_require_ema200": "true"},
    "min entry ADX 20":    {"ema_min_entry_adx": "20"},
    "min entry ADX 25":    {"ema_min_entry_adx": "25"},
    "EMA200 + ADX 20":     {"ema_require_ema200": "true", "ema_min_entry_adx": "20"},
}

# Each window is a distinct regime. A gate must not be judged on the bear alone.
WINDOW_LABELS = {
    "full3":    "2020-08→now   full multi-regime",
    "bull2021": "2020-08→2021  bull",
    "bear2022": "2022          bear",
    "rec2324":  "2023-2024     recovery+bull",
    "chop2526": "2025-2026     chop/bear",
}


def main() -> None:
    jobs = [
        {"label": vlabel, "window": w, "overrides": ov, "meta": {"window": w}}
        for w in WINDOW_LABELS
        for vlabel, ov in VARIANTS.items()
    ]

    print(f"Dispatching {len(jobs)} backtests across {WORKERS} workers…")
    with Pool(WORKERS, initializer=_init_worker) as pool:
        raw = pool.map(_execute, jobs)

    results: dict = {}
    for spec, res in zip(jobs, raw):
        if res.get("skipped"):
            continue
        results.setdefault(spec["meta"]["window"], []).append(res)

    for w, wlabel in WINDOW_LABELS.items():
        rows = results.get(w)
        if not rows:
            continue
        hdr(f"WINDOW: {wlabel}")
        print(head(24))
        base_calmar = None
        for d in rows:
            r = to_run(d)
            if base_calmar is None:
                base_calmar = r.calmar
            delta = "" if d is rows[0] else f"   Calmar {r.calmar - base_calmar:+.2f} vs base"
            print(row(r, 24) + delta)

    hdr("VERDICT INPUT — Calmar by variant across regimes")
    variants = list(VARIANTS)
    print(f"{'variant':<24}" + "".join(f"{w:>16}" for w in WINDOW_LABELS))
    for i, v in enumerate(variants):
        cells = ""
        for w in WINDOW_LABELS:
            rows = results.get(w) or []
            d = rows[i] if i < len(rows) else None
            cells += f"{(to_run(d).calmar if d else 0):>16.2f}"
        print(f"{v:<24}{cells}")

    out = "docs/audits/bear_filters_2026-08-15.json"
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"\nSaved → {out}")


if __name__ == "__main__":
    main()
