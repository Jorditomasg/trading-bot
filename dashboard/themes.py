"""Nothing OS design language — colors, fonts, Plotly layout."""

# ── Palette ───────────────────────────────────────────────────────────────────
class NothingOS:
    BG      = "#0A0A0A"
    SURFACE = "#111111"
    BORDER  = "#1A1A1A"
    ACCENT  = "#FF0000"
    TEXT    = "#F5F5F5"
    MUTED   = "#555555"
    GRAY    = "#888888"
    FONT    = "'Space Mono', 'Courier New', monospace"

    NOTHING_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap');

/* ── Root ──────────────────────────────────────────────── */
html, body, [class*="css"] {
    font-family: 'Space Mono', 'Courier New', monospace !important;
    background-color: #0A0A0A;
    color: #F5F5F5;
}

/* ── Hide Streamlit chrome ──────────────────────────────── */
#MainMenu, footer, header { visibility: hidden; }
.block-container { padding: 1.5rem 2rem 1rem; }

/* ── Divider ────────────────────────────────────────────── */
hr { border-color: #1A1A1A !important; }

/* ── Metric cards ───────────────────────────────────────── */
[data-testid="metric-container"] {
    background: #111111;
    border: 1px solid #1A1A1A;
    padding: 1rem 1.2rem;
    border-radius: 0 !important;
}
[data-testid="metric-container"] label {
    font-size: 0.65rem !important;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: #555 !important;
}
[data-testid="metric-container"] [data-testid="stMetricValue"] {
    font-size: 1.55rem !important;
    font-weight: 700;
    color: #F5F5F5;
}
[data-testid="stMetricDelta"] svg { display: none; }
[data-testid="stMetricDelta"] > div {
    font-size: 0.75rem !important;
    letter-spacing: 0.05em;
}

/* ── Dataframes ─────────────────────────────────────────── */
[data-testid="stDataFrame"] {
    border: 1px solid #1A1A1A;
    border-radius: 0 !important;
}
thead tr th {
    background: #111 !important;
    color: #555 !important;
    font-size: 0.65rem !important;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    border-bottom: 1px solid #1A1A1A !important;
}
tbody tr:nth-child(even) { background: #0D0D0D !important; }

/* ── Section headers ────────────────────────────────────── */
h1, h2, h3 {
    font-family: 'Space Mono', monospace !important;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: #F5F5F5 !important;
}
h2 { font-size: 0.85rem !important; letter-spacing: 0.18em; text-transform: uppercase; color: #555 !important; }

/* ── Status pills ───────────────────────────────────────── */
.pill {
    display: inline-block;
    padding: 2px 10px;
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
}
.pill-running  { border: 1px solid #FF0000; color: #FF0000; }
.pill-stopped  { border: 1px solid #333;    color: #555; }
.pill-testnet  { border: 1px solid #555;    color: #888; }
.pill-mainnet  { background: #FF0000; border: 1px solid #FF0000; color: #FFFFFF; font-weight: 900; letter-spacing: 0.2em; }
.pill-live     { border: 1px solid #FF0000; color: #FF0000; }

/* ── Regime badges ──────────────────────────────────────── */
.regime {
    display: inline-block;
    padding: 2px 10px;
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
    border-radius: 0 !important;
}
.regime-TRENDING  { border: 1px solid #F5F5F5; color: #F5F5F5; }
.regime-RANGING   { border: 1px solid #555;    color: #888; }
.regime-VOLATILE  { border: 1px solid #FF0000; color: #FF0000; }

/* ── Bias badges ─────────────────────────────────────────── */
.bias {
    display: inline-block;
    padding: 2px 8px;
    font-size: 0.6rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 700;
    font-family: 'Space Mono', monospace;
}
.bias-BULLISH { border: 1px solid #00C853; color: #00C853; }
.bias-BEARISH { border: 1px solid #FF0000; color: #FF0000; }
.bias-NEUTRAL { border: 1px solid #333;    color: #555; }

/* ── PnL colours ────────────────────────────────────────── */
.pos { color: #F5F5F5; font-weight: 700; }
.neg { color: #FF0000; font-weight: 700; }
.neu { color: #555; }

/* ── Topbar ─────────────────────────────────────────────── */
.topbar {
    display: flex;
    align-items: baseline;
    gap: 1.5rem;
    margin-bottom: 0.25rem;
}
.bot-name {
    font-size: 1.2rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    color: #F5F5F5;
}
.glyph {
    color: #FF0000;
    font-size: 1.1rem;
    margin-right: 0.3rem;
}

/* ── Topbar action columns (⬇ ⚙ popovers) ──────────────── */
[data-testid="column"]:has([data-testid="stPopover"]) {
    max-width: 52px !important;
    min-width: 40px !important;
    flex: 0 0 52px !important;
    padding-left: 0 !important;
    padding-right: 2px !important;
}
[data-testid="column"]:has([data-testid="stPopover"]) button[kind="secondary"] {
    width: 40px !important;
    min-width: 40px !important;
    max-width: 40px !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
    justify-content: center !important;
}

/* ── Info box ────────────────────────────────────────────── */
[data-testid="stInfo"], [data-testid="stSuccess"], [data-testid="stWarning"] {
    background: #111 !important;
    border-left: 2px solid #1A1A1A !important;
    border-radius: 0 !important;
    font-size: 0.75rem;
}

/* ── Captions ───────────────────────────────────────────── */
[data-testid="stCaptionContainer"] {
    color: #333 !important;
    font-size: 0.6rem !important;
    letter-spacing: 0.08em;
}
</style>
"""

    # Plotly base layout — applied to all charts
    PLOTLY_LAYOUT = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#0A0A0A",
        font=dict(family="Space Mono, Courier New, monospace", color="#555", size=10),
        margin=dict(l=0, r=0, t=4, b=0),
        xaxis=dict(gridcolor="#111", showline=False, zeroline=False),
        yaxis=dict(gridcolor="#111", showline=False, zeroline=False),
    )
