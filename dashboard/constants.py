"""Dashboard UI constants — single source of truth for all hardcoded values."""

# ─── Colors ───────────────────────────────────────────────────────────────────
RED     = "#FF0000"   # losses, VOLATILE, SELL, negative
WHITE   = "#F5F5F5"   # gains, TRENDING, BUY, positive, primary text
MUTED   = "#555555"   # labels, RANGING, neutral
BG      = "#0A0A0A"   # page background
SURFACE = "#111111"   # card / chart surface
BORDER  = "#1A1A1A"   # borders, dividers
GRAY    = "#888888"   # secondary accent (hline annotations)
CAPTION = "#333333"   # captions, subtle text
GREEN   = "#00C853"   # BUY signal markers

# ─── Regime color map ─────────────────────────────────────────────────────────
REGIME_COLORS: dict[str, str] = {
    "TRENDING": WHITE,
    "RANGING":  MUTED,
    "VOLATILE": RED,
}

# ─── Chart config ─────────────────────────────────────────────────────────────
class ChartConfig:
    HEIGHT_EQUITY      = 220
    HEIGHT_DRAWDOWN    = 220
    HEIGHT_LIVE        = 200
    HEIGHT_PERFORMANCE = 200
    HEIGHT_REGIME      = 180
    MARKER_SIZE        = 10
    MARKER_OPACITY     = 0.9
    LINE_WIDTH         = 1.5
    LINE_WIDTH_THIN    = 1

# ─── Thresholds ───────────────────────────────────────────────────────────────
class Thresholds:
    CIRCUIT_BREAKER_PCT = 15.0   # drawdown % that triggers the circuit breaker
    WIN_RATE_MID        = 50.0   # 50% reference line on win-rate charts

# ─── Fragment refresh rates (seconds) ─────────────────────────────────────────
class RefreshRates:
    TOPBAR      = 5
    KPI         = 10
    POSITION    = 10
    CHARTS      = 30
    PERFORMANCE = 30

# ─── Cache TTLs (seconds) ─────────────────────────────────────────────────────
class CacheTTL:
    KLINES      = 300
    LIVE_PRICE  = 5
