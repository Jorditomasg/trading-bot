"""Shared UI helpers for the dashboard."""

import os

# DECIMAL_SEPARATOR=comma → 1.234,56  (period=thousands, comma=decimal)
# DECIMAL_SEPARATOR=dot   → 1,234.56  (comma=thousands, dot=decimal) — default
_COMMA_DECIMAL = os.getenv("DECIMAL_SEPARATOR", "dot").lower() == "comma"


def fmt(value: float, spec: str = ",.2f") -> str:
    """Format a number respecting the DECIMAL_SEPARATOR env var.

    DECIMAL_SEPARATOR=dot   (default) → 1,234.56
    DECIMAL_SEPARATOR=comma           → 1.234,56
    """
    s = format(value, spec)
    if not _COMMA_DECIMAL:
        return s
    # Swap separators: comma→placeholder, period→comma, placeholder→period
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def parse_fmt(s: str) -> float:
    """Parse a localized number string back to float (strips +, %, $)."""
    clean = s.replace("+", "").replace("%", "").replace("$", "").strip()
    if _COMMA_DECIMAL:
        clean = clean.replace(".", "").replace(",", ".")
    return float(clean)


def _regime_badge(regime: str) -> str:
    r = regime.upper()
    return f'<span class="regime regime-{r}">{r}</span>'


def _pnl_color(val: float) -> str:
    return "#F5F5F5" if val >= 0 else "#FF0000"
