"""Statistical comparison between two configs (numpy-only, no scipy dependency).

Paired t-test and Cohen's d for the case where the same set of windows is
evaluated under two configs. Returns the practical significance summary
needed by the audit's comparative verdict.

Exports:
  paired_t_test        — two-sided paired t-test
  cohens_d_paired      — effect size for paired samples
  compare_configs      — full comparison summary (used by run_walk_forward.py)
  ComparatorVerdict    — frozen dataclass with champion-challenger comparison result
  champion_vs_challenger — high-level champion vs challenger comparison function
"""
from __future__ import annotations

import math
from dataclasses import dataclass

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


# ── Champion-challenger comparator ───────────────────────────────────────────


@dataclass(frozen=True)
class ComparatorVerdict:
    """Result of a paired champion-challenger walk-forward comparison.

    All metrics use challenger minus champion convention for differences:
    positive calmar_cohen_d means the challenger outperformed the champion.

    Attributes
    ----------
    champion_pf_mean:       Mean profit factor of the champion across all windows.
    challenger_pf_mean:     Mean profit factor of the challenger across all windows.
    champion_calmar_mean:   Mean Calmar ratio of the champion.
    challenger_calmar_mean: Mean Calmar ratio of the challenger.
    pf_t_stat:              t-statistic from the paired t-test on PF.
    pf_p_value:             Two-sided p-value for the PF paired t-test.
    calmar_t_stat:          t-statistic from the paired t-test on Calmar.
    calmar_p_value:         Two-sided p-value for the Calmar paired t-test.
    pf_cohen_d:             Cohen's d for PF (positive → challenger higher).
    calmar_cohen_d:         Cohen's d for Calmar (positive → challenger higher).
    wins:                   Windows where challenger Calmar > champion Calmar.
    losses:                 Windows where challenger Calmar < champion Calmar.
    ties:                   Windows where challenger Calmar == champion Calmar.
    verdict:                "ADOPT" | "REJECT" | "INCONCLUSIVE"

    Verdict criteria (design D9):
      ADOPT       : calmar_p_value < 0.05 AND calmar_cohen_d > 0.5 AND wins >= 6/10
      REJECT      : losses >= 6/10 (champion clearly dominates challenger)
      INCONCLUSIVE: all other cases
    """
    champion_pf_mean:       float
    challenger_pf_mean:     float
    champion_calmar_mean:   float
    challenger_calmar_mean: float
    pf_t_stat:              float
    pf_p_value:             float
    calmar_t_stat:          float
    calmar_p_value:         float
    pf_cohen_d:             float
    calmar_cohen_d:         float
    wins:                   int
    losses:                 int
    ties:                   int
    verdict:                str   # "ADOPT" | "REJECT" | "INCONCLUSIVE"


def champion_vs_challenger(
    champion_results:   list,   # list[WindowResult] — duck-typed
    challenger_results: list,   # list[WindowResult] — duck-typed
) -> ComparatorVerdict:
    """Compare two configs over the same walk-forward windows.

    Uses paired t-tests and Cohen's d on both PF and Calmar. Win/loss/tie
    counts are based on per-window Calmar comparisons (primary metric).

    Parameters
    ----------
    champion_results:
        WindowResult list for the baseline / champion config.
    challenger_results:
        WindowResult list for the new / challenger config. Must be the same
        length as ``champion_results`` (same window set).

    Returns
    -------
    ComparatorVerdict
        Frozen dataclass with all statistical results and a textual verdict.

    Raises
    ------
    ValueError
        If the two result lists differ in length, or if each has fewer than 2 entries.

    Notes
    -----
    ADOPT criteria: paired Calmar p < 0.05 AND Cohen's d > 0.5 AND wins >= 60% of windows.
    REJECT criteria: losses >= 60% of windows (champion clearly dominates).
    INCONCLUSIVE: everything else.

    Per-window Calmar values of +inf are treated as finite for mean computation
    (stripped from t-test and Cohen's d via the existing paired_t_test / cohens_d_paired
    helpers that already handle infinite values gracefully by comparison — both lists
    receive the same treatment so paired differences are well-defined when both are inf).
    """
    n_champ = len(champion_results)
    n_chal  = len(challenger_results)
    if n_champ != n_chal:
        raise ValueError(
            f"champion and challenger result lists must have equal length; "
            f"got {n_champ} vs {n_chal}"
        )
    if n_champ < 2:
        # Single-window: cannot compute paired t-test. Return INCONCLUSIVE.
        if n_champ == 0:
            raise ValueError("champion_vs_challenger requires at least 2 windows")
        # 1 window — return raw metrics with INCONCLUSIVE verdict
        pf_champ   = float(getattr(champion_results[0],   "pf",     0.0))
        pf_chal    = float(getattr(challenger_results[0], "pf",     0.0))
        cal_champ  = float(getattr(champion_results[0],   "calmar", 0.0))
        cal_chal   = float(getattr(challenger_results[0], "calmar", 0.0))
        wins   = 1 if cal_chal > cal_champ else 0
        losses = 1 if cal_chal < cal_champ else 0
        ties   = 1 if cal_chal == cal_champ else 0
        return ComparatorVerdict(
            champion_pf_mean=pf_champ, challenger_pf_mean=pf_chal,
            champion_calmar_mean=cal_champ, challenger_calmar_mean=cal_chal,
            pf_t_stat=0.0, pf_p_value=1.0,
            calmar_t_stat=0.0, calmar_p_value=1.0,
            pf_cohen_d=0.0, calmar_cohen_d=0.0,
            wins=wins, losses=losses, ties=ties,
            verdict="INCONCLUSIVE",
        )

    # Extract metric arrays
    pf_champ_vals   = [float(getattr(r, "pf",     0.0)) for r in champion_results]
    pf_chal_vals    = [float(getattr(r, "pf",     0.0)) for r in challenger_results]
    cal_champ_vals  = [float(getattr(r, "calmar", 0.0)) for r in champion_results]
    cal_chal_vals   = [float(getattr(r, "calmar", 0.0)) for r in challenger_results]

    # Replace +inf/-inf with large finite values for paired statistics.
    # Using 1e9 as a cap: practically equivalent to infinity for mean-difference purposes
    # while keeping arithmetic stable.
    _cap = 1e9

    def _finite(vals: list[float]) -> list[float]:
        return [
            _cap if v == float("inf") else (-_cap if v == float("-inf") else v)
            for v in vals
        ]

    pf_champ_f  = _finite(pf_champ_vals)
    pf_chal_f   = _finite(pf_chal_vals)
    cal_champ_f = _finite(cal_champ_vals)
    cal_chal_f  = _finite(cal_chal_vals)

    # Per-window win/loss/tie on Calmar (challenger vs champion)
    wins = losses = ties = 0
    for cc, ch in zip(cal_chal_vals, cal_champ_vals):
        if cc > ch:
            wins += 1
        elif cc < ch:
            losses += 1
        else:
            ties += 1

    # Paired t-tests — convention: (challenger, champion) so that
    # positive t / d means "challenger is better than champion".
    # diff = challenger - champion > 0 when challenger outperforms.
    pf_t     = paired_t_test(pf_chal_f,  pf_champ_f)
    cal_t    = paired_t_test(cal_chal_f, cal_champ_f)
    pf_d     = cohens_d_paired(pf_chal_f,  pf_champ_f)
    cal_d    = cohens_d_paired(cal_chal_f, cal_champ_f)

    # Means (use original values, no cap needed for simple mean)
    arr_pf_ch  = np.asarray(pf_champ_f, dtype=float)
    arr_pf_cl  = np.asarray(pf_chal_f,  dtype=float)
    arr_cal_ch = np.asarray(cal_champ_f, dtype=float)
    arr_cal_cl = np.asarray(cal_chal_f,  dtype=float)

    # Verdict logic (design D9 / spec REQ-CC-1)
    # ADOPT: calmar p < 0.05 AND calmar_cohen_d > 0.5 AND wins >= 60% of windows
    # REJECT: losses >= 60% of windows
    # INCONCLUSIVE: everything else
    n            = n_champ
    win_ratio    = wins   / n
    loss_ratio   = losses / n
    adopt_thresh = 0.6    # >= 60% windows won

    if (
        cal_t["p"] < 0.05
        and cal_d > 0.5
        and win_ratio >= adopt_thresh
    ):
        verdict = "ADOPT"
    elif loss_ratio >= adopt_thresh:
        verdict = "REJECT"
    else:
        verdict = "INCONCLUSIVE"

    return ComparatorVerdict(
        champion_pf_mean       = float(np.mean(arr_pf_ch)),
        challenger_pf_mean     = float(np.mean(arr_pf_cl)),
        champion_calmar_mean   = float(np.mean(arr_cal_ch)),
        challenger_calmar_mean = float(np.mean(arr_cal_cl)),
        pf_t_stat              = pf_t["t"],
        pf_p_value             = pf_t["p"],
        calmar_t_stat          = cal_t["t"],
        calmar_p_value         = cal_t["p"],
        pf_cohen_d             = pf_d,
        calmar_cohen_d         = cal_d,
        wins                   = wins,
        losses                 = losses,
        ties                   = ties,
        verdict                = verdict,
    )


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
