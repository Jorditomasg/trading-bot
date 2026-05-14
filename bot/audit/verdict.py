"""GO / WATCH / NO-GO verdict for a single config's aggregated metrics.

Thresholds are predetermined per the audit spec to avoid p-hacking.
WATCH = any threshold fails by ≤ 10%. NO-GO = any threshold fails by > 10%.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerdictThresholds:
    """Predetermined thresholds (spec section 4.1)."""
    pf_mean:       float = 1.20
    pf_hit_rate:   float = 0.65
    calmar_mean:   float = 1.50
    max_dd_pct:    float = 35.0    # worst-window DD ceiling
    sparsity_pct:  float = 0.20    # max share of windows with < 5 trades


def _check(name: str, value: float, threshold: float, *, higher_is_better: bool) -> dict:
    if higher_is_better:
        passed = value >= threshold
        # how far below threshold (negative if passed)
        margin = (threshold - value) / threshold if threshold else 0.0
    else:
        passed = value <= threshold
        margin = (value - threshold) / threshold if threshold else 0.0
    return {
        "name":      name,
        "value":     value,
        "threshold": threshold,
        "passed":    bool(passed),
        "margin":    float(margin),     # positive when failed, negative when passed
    }


def evaluate_verdict(
    aggregate: dict,
    thresholds: VerdictThresholds = VerdictThresholds(),
) -> dict:
    """Return verdict dict with `bucket` (GO/WATCH/NO-GO) and `checks` list."""
    pf_mean      = aggregate["pf"]["mean"]
    pf_hit_rate  = aggregate["pf"]["hit_rate"]
    calmar_mean  = aggregate["calmar"]["mean"]
    worst_dd     = aggregate["max_drawdown_pct"]["worst"]
    sparsity_pct = aggregate["sparsity"]["pct"]

    checks = [
        _check("pf_mean",               pf_mean,      thresholds.pf_mean,      higher_is_better=True),
        _check("pf_hit_rate",           pf_hit_rate,  thresholds.pf_hit_rate,  higher_is_better=True),
        _check("calmar_mean",           calmar_mean,  thresholds.calmar_mean,  higher_is_better=True),
        _check("max_dd_within_limit",   worst_dd,     thresholds.max_dd_pct,   higher_is_better=False),
        _check("sparsity_within_limit", sparsity_pct, thresholds.sparsity_pct, higher_is_better=False),
    ]

    failures = [c for c in checks if not c["passed"]]
    if not failures:
        bucket = "GO"
    elif all(c["margin"] <= 0.10 for c in failures):
        bucket = "WATCH"
    else:
        bucket = "NO-GO"

    return {"bucket": bucket, "checks": checks, "thresholds": thresholds.__dict__}
