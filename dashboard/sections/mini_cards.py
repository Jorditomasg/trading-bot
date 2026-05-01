"""Compact per-symbol cards rendered in the persistent strip — refreshes every 5s."""

import streamlit as st

from bot.database.db import Database
from bot.exchange.binance_client import BinanceClient
from dashboard.constants import RefreshRates, CacheTTL
from dashboard.utils import fmt


@st.cache_data(ttl=CacheTTL.LIVE_PRICE)
def _get_rest_price_cached(symbol: str) -> float | None:
    try:
        client = BinanceClient()
        return client.get_ticker_price(symbol)
    except Exception:
        return None


def _resolve_price(db: Database, symbol: str) -> float | None:
    tick = db.get_live_tick(symbol)
    if tick is not None:
        return tick["price"]
    return _get_rest_price_cached(symbol)


def _short_name(symbol: str) -> str:
    return symbol.replace("USDT", "")


def _card_html(symbol: str, price: float | None, pnl_pct: float | None, status: str) -> str:
    """status: 'BUY' (green dot), 'SELL' (red dot), 'IDLE' (grey dot)."""
    dot_color = {"BUY": "#4caf7d", "SELL": "#e05252", "IDLE": "#444"}[status]
    price_str = f"${fmt(price, ',.2f')}" if price is not None else "—"
    pnl_str   = f"{fmt(pnl_pct, '+.2f')}%" if pnl_pct is not None else "—"
    pnl_color = "#4caf7d" if (pnl_pct or 0) >= 0 else "#e05252"
    return (
        f"<div style='border:1px solid #1a1a1a;padding:6px 10px;"
        f"font-family:Space Mono,monospace;background:#0a0a0a;"
        f"min-width:120px;display:inline-block;margin-right:6px'>"
        f"<div style='font-size:0.65rem;letter-spacing:0.12em;color:#888'>"
        f"<span style='color:{dot_color}'>●</span> {_short_name(symbol)}</div>"
        f"<div style='font-size:0.85rem;color:#eee;font-weight:700'>{price_str}</div>"
        f"<div style='font-size:0.7rem;color:{pnl_color}'>{pnl_str}</div>"
        f"</div>"
    )


@st.fragment(run_every=RefreshRates.LIVE_PRICE)
def mini_cards_section(db: Database) -> None:
    symbols = db.get_symbols()
    if not symbols:
        return

    parts: list[str] = []
    for sym in symbols:
        price = _resolve_price(db, sym)
        open_trades = db.get_open_trades(symbol=sym)
        if open_trades and price is not None:
            t       = open_trades[0]
            sign    = 1 if t["side"] == "BUY" else -1
            pnl_pct = sign * (price - t["entry_price"]) / t["entry_price"] * 100
            status  = t["side"]
        else:
            pnl_pct = None
            status  = "IDLE"
        parts.append(_card_html(sym, price, pnl_pct, status))

    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:6px'>" + "".join(parts) + "</div>",
        unsafe_allow_html=True,
    )
