"""Drawdown-aware risk scaling.

Reduces position size when the account equity is in drawdown from its
high-water mark. Restores full risk once equity returns to a new peak.

Hypothesis: in trend-following strategies, sustained drawdown is a regime
signal — the edge is degraded. Continuing at full risk compounds the bleed.
Tactical de-risking caps damage; auto-restoration to full risk on recovery
preserves upside in good regimes.

Stateless helper — caller tracks peak_capital and current_capital, helper
computes the scalar multiplier. This keeps the same component usable from
both BacktestEngine (where peak is derived from equity_curve) and the
live orchestrator (where peak is persisted in DB).

Math:
    dd = (peak - current) / peak    (0 if current >= peak)

    multiplier = 1.0                if dd < first_threshold
                 multiplier[i]      where i is the largest threshold dd >= thresholds[i]

Example:
    thresholds = [0.05, 0.10]   multipliers = [0.5, 0.25]
    dd = 0.03  → 1.0   (full risk)
    dd = 0.07  → 0.5   (half risk — first tier)
    dd = 0.12  → 0.25  (quarter risk — second tier)
    dd = 0.20  → 0.25  (still last tier)
    new peak  → 1.0    (auto-restored)

The configured multipliers should be in (0, 1]. Thresholds in (0, 1) and
strictly increasing. The helper enforces these in __post_init__.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DrawdownRiskConfig:
    enabled: bool = False
    thresholds:  list[float] = field(default_factory=lambda: [0.05, 0.10])
    multipliers: list[float] = field(default_factory=lambda: [0.50, 0.25])

    def __post_init__(self) -> None:
        if len(self.thresholds) != len(self.multipliers):
            raise ValueError(
                f"thresholds ({len(self.thresholds)}) and multipliers ({len(self.multipliers)}) "
                "must have the same length"
            )
        for i, t in enumerate(self.thresholds):
            if not (0.0 < t < 1.0):
                raise ValueError(f"threshold[{i}]={t} must be in (0, 1)")
        for i in range(1, len(self.thresholds)):
            if self.thresholds[i] <= self.thresholds[i - 1]:
                raise ValueError(
                    f"thresholds must be strictly increasing — got {self.thresholds}"
                )
        for i, m in enumerate(self.multipliers):
            if not (0.0 < m <= 1.0):
                raise ValueError(f"multiplier[{i}]={m} must be in (0, 1]")


def drawdown_multiplier(
    current_capital: float,
    peak_capital: float,
    config: DrawdownRiskConfig,
) -> float:
    """Return the risk multiplier given current equity and HWM.

    Returns 1.0 (no scaling) when:
      - config.enabled is False
      - peak_capital <= 0 (cold start, no HWM yet)
      - current_capital >= peak_capital (at or above HWM)
      - drawdown below first threshold

    Otherwise returns the multiplier corresponding to the deepest threshold
    breached (last-match wins).
    """
    if not config.enabled:
        return 1.0
    if peak_capital <= 0 or current_capital >= peak_capital:
        return 1.0

    dd = (peak_capital - current_capital) / peak_capital

    # Find the deepest threshold breached. thresholds is strictly increasing.
    multiplier = 1.0
    for t, m in zip(config.thresholds, config.multipliers):
        if dd >= t:
            multiplier = m
        else:
            break
    return multiplier
