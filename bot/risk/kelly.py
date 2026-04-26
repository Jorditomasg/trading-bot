"""Kelly Criterion functions for dynamic position sizing."""


def compute_kelly_fraction(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    half: bool = True,
) -> float:
    """
    Compute the optimal Kelly fraction for a trading strategy.

    Kelly Criterion formula:
        f* = win_rate - (1 - win_rate) / b
    where:
        b = avg_win_pct / avg_loss_pct (reward-to-risk ratio)
        win_rate = fraction of winning trades (0.0 to 1.0)
        avg_win_pct = average profit per winning trade (decimal, e.g. 0.025 = 2.5%)
        avg_loss_pct = average loss per losing trade (decimal, e.g. 0.01 = 1%)

    Args:
        win_rate: Fraction of winning trades (0.0 to 1.0)
        avg_win_pct: Average profit percentage (decimal)
        avg_loss_pct: Average loss percentage (decimal)
        half: If True, return half-Kelly (conservative); else return full Kelly

    Returns:
        Optimal fraction of capital to risk (floored at 0.0 for negative edge).
        If half=True, returns f* × 0.5. Otherwise returns f*.
    """
    if avg_loss_pct <= 0 or avg_win_pct <= 0:
        return 0.0

    b = avg_win_pct / avg_loss_pct
    q = 1.0 - win_rate
    f = win_rate - q / b

    if half:
        f *= 0.5

    return max(0.0, f)


def kelly_risk_fraction(
    kelly_f: float,
    signal_strength: float,
    base_risk: float,
    max_mult: float = 2.0,
    min_mult: float = 0.25,
) -> float:
    """
    Map Kelly fraction to a dynamic risk per trade value.

    Scales the base_risk by a multiplier derived from Kelly fraction
    and signal strength, clamped to safe bounds.

    Formula:
        mult = (kelly_f / base_risk) * signal_strength
        result = base_risk * clamp(mult, min_mult, max_mult)

    Args:
        kelly_f: Kelly fraction (output of compute_kelly_fraction)
        signal_strength: Signal strength scalar (0.0 to 1.0)
        base_risk: Base risk per trade (e.g. 0.01 = 1%)
        max_mult: Maximum multiplier cap (default 2.0 = 2x base risk)
        min_mult: Minimum multiplier floor (default 0.25 = 0.25x base risk)

    Returns:
        Dynamic risk per trade value, clamped between min_mult*base_risk
        and max_mult*base_risk.
    """
    if base_risk <= 0:
        return 0.0

    mult = (kelly_f / base_risk) * signal_strength
    mult = max(min_mult, min(mult, max_mult))
    return base_risk * mult
