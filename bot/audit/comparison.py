"""Statistical comparison between two configs (numpy-only, no scipy dependency).

Paired t-test and Cohen's d for the case where the same set of windows is
evaluated under two configs. Returns the practical significance summary
needed by the audit's comparative verdict.
"""
from __future__ import annotations

import math

import numpy as np


def paired_t_test(a: list[float], b: list[float]) -> dict:
    """Two-sided paired t-test on samples a vs b. Returns dict with t and p.

    Implementation: t = mean(d) / (std(d, ddof=1) / sqrt(n)) where d = a - b.
    p value computed from Student's t CDF via incomplete beta function.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")
    if arr_a.size < 2:
        raise ValueError("Need at least 2 paired samples")

    diff = arr_a - arr_b
    n    = diff.size
    mean = float(np.mean(diff))
    sd   = float(np.std(diff, ddof=1))

    if sd == 0.0:
        return {"t": 0.0, "p": 1.0, "df": n - 1}

    t  = mean / (sd / math.sqrt(n))
    df = n - 1
    # Two-sided p value via the regularized incomplete beta function.
    # p = I_{df/(df+t^2)}(df/2, 1/2) — the standard Student's-t two-sided CDF.
    x = df / (df + t * t)
    p = float(_betainc_regularized(df / 2.0, 0.5, x))
    # Numerical guard
    p = max(0.0, min(1.0, p))
    return {"t": float(t), "p": p, "df": df}


def cohens_d_paired(a: list[float], b: list[float]) -> float:
    """Cohen's d for paired samples: mean(a - b) / std(a - b, ddof=1)."""
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")
    diff = arr_a - arr_b
    if diff.size < 2:
        return 0.0
    sd = float(np.std(diff, ddof=1))
    mean_diff = float(np.mean(diff))
    if sd == 0.0:
        # Constant difference: direction is still meaningful.
        # Return ±inf if there is a non-zero mean, 0 if truly identical.
        return math.copysign(float("inf"), mean_diff) if mean_diff != 0.0 else 0.0
    return float(mean_diff / sd)


def compare_configs(a: list[float], b: list[float], metric_name: str = "pf") -> dict:
    """Full comparison summary for two configs over the same windows.

    `delta_mean = mean(b) - mean(a)` so positive means config B is better
    (matches "Δ PF in C2's favor" reading from the spec).
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")

    t_result = paired_t_test(a, b)
    return {
        "metric":     metric_name,
        "n":          int(arr_a.size),
        "mean_a":     float(np.mean(arr_a)),
        "mean_b":     float(np.mean(arr_b)),
        "delta_mean": float(np.mean(arr_b) - np.mean(arr_a)),
        "t":          t_result["t"],
        "p":          t_result["p"],
        "df":         t_result["df"],
        "cohens_d":   cohens_d_paired(a, b),
    }


# ── Internal: regularized incomplete beta function (Numerical Recipes 6.4) ───

def _betainc_regularized(a: float, b: float, x: float) -> float:
    """I_x(a, b) = B(x; a, b) / B(a, b) — used by the Student t-distribution CDF."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Log gamma via lgamma for numerical stability
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, *, max_iter: int = 200, eps: float = 3e-7) -> float:
    """Continued fraction expansion of the regularized incomplete beta."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d  = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c  = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d  = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d  = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c  = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d  = 1.0 / d
        delta = d * c
        h    *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h
