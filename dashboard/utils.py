"""Shared UI helpers for the dashboard."""


def _regime_badge(regime: str) -> str:
    r = regime.upper()
    return f'<span class="regime regime-{r}">{r}</span>'


def _pnl_color(val: float) -> str:
    return "#F5F5F5" if val >= 0 else "#FF0000"
