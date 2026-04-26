# Multi-Symbol Trading — Design Spec

**Date:** 2026-04-26  
**Status:** Approved

---

## Goal

Allow the bot to trade multiple symbols simultaneously (e.g. BTCUSDT + ETHUSDT + SOLUSDT),
configurable from the dashboard CONFIG tab without touching `.env`. Each symbol runs its own
strategy cycle independently. Max 1 open position per symbol at a time.

---

## Architecture

### Loop structure

Each hourly scheduler tick iterates over the configured symbol list serially:

```
run_all_cycles()
  → run_cycle(orchestrators["BTCUSDT"], db, ...)
  → run_cycle(orchestrators["ETHUSDT"], db, ...)
  → run_cycle(orchestrators["SOLUSDT"], db, ...)

position_manager()  [every 60s]
  → iterates all open trades
  → per trade: get_live_tick(trade["symbol"])
```

One failure in a symbol's cycle is caught and logged; the remaining symbols continue.

### Config storage

New `bot_config` key: `symbols` = `"BTCUSDT,ETHUSDT,SOLUSDT"` (comma-separated string).

- If `symbols` is present in `bot_config` → use that list.
- Fallback: `[settings.symbol]` (backward compatible with single-symbol `.env` setup).
- Symbol list takes effect after bot restart (same policy as EMA manual changes).

---

## File-by-file changes

### 1. `bot/database/db.py`

**`get_open_trades(symbol: str | None = None) -> list[dict]`**

Add optional `symbol` filter:

```python
def get_open_trades(self, symbol: str | None = None) -> list[dict]:
    with self._conn() as conn:
        if symbol is None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL AND symbol = ? ORDER BY entry_time DESC",
                (symbol,),
            ).fetchall()
    return [dict(r) for r in rows]
```

**`get_symbols() -> list[str]`**

```python
def get_symbols(self) -> list[str]:
    cfg = self.get_runtime_config()
    raw = cfg.get("symbols", "")
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [settings.symbol]  # fallback

def set_symbols(self, symbols: list[str]) -> None:
    self.set_runtime_config(symbols=",".join(symbols))
```

`get_open_trade()` shim (backward compat) — no changes needed, calls `get_open_trades()` internally.

---

### 2. `bot/orchestrator.py`

`StrategyOrchestrator` already stores `self.symbol`. The only change: pass it to `get_open_trades()`.

```python
# Line ~114 — was: open_trades = self.db.get_open_trades()
open_trades = self.db.get_open_trades(symbol=self.symbol)
```

No other orchestrator changes needed.

---

### 3. `main.py`

**A — `run_cycle()` uses `orchestrator.symbol`**

Replace all `settings.symbol` inside `run_cycle()` with `orchestrator.symbol`:

```python
# klines fetch
df      = client.get_klines(orchestrator.symbol, settings.timeframe, KLINES_LIMIT)
df_high = client.get_klines(orchestrator.symbol, bias_tf, 60)  # bias_tf from _BIAS_TF map

# equity snapshot — write once per cycle (same balance, idempotent)
db.insert_equity_snapshot(balance=balance, drawdown=drawdown)
```

**B — `position_manager()` — per-trade symbol tick**

```python
def position_manager(db, dry_run, risk_config=None, notifier=None):
    trades = db.get_open_trades()
    if not trades:
        return
    for trade in trades:
        tick = db.get_live_tick(trade["symbol"])
        if tick is None:
            logger.debug("position_manager: no live tick for %s — skipping", trade["symbol"])
            continue
        _manage_single_position(trade, tick["price"], db, dry_run, risk_config, notifier)
```

**C — Startup: orchestrator dict + multi-stream**

```python
# Read symbol list
symbols = db.get_symbols()  # e.g. ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

# One orchestrator per symbol (each with own RiskManager / circuit breaker)
orchestrators: dict[str, StrategyOrchestrator] = {
    sym: StrategyOrchestrator(
        db=db,
        symbol=sym,
        risk_config=RiskConfig(risk_per_trade=settings.risk_per_trade),
        bias_filter=_build_bias_filter(db),
        timeframe=settings.timeframe,
    )
    for sym in symbols
}
# Apply optimizer configs to each orchestrator
for sym, orch in orchestrators.items():
    _apply_ema_config(db, orch)
    _apply_trail_config(db, orch.risk_manager.config)
    _init_quantity_precision(orch, db)

# WebSocket streams — one per symbol
stream_client = _build_client(db)
twms = [
    stream_client.start_price_stream(sym, _make_tick_handler(db, sym))
    for sym in symbols
]

# Scheduler — wrap all symbols in one function
def run_all_cycles():
    for sym, orch in orchestrators.items():
        try:
            run_cycle(orch, db, dry_run=args.dry_run, adaptor=adaptor, notifier=notifier)
        except Exception as exc:
            logger.error("run_cycle failed for %s: %s", sym, exc)

schedule.every().hour.at(":00").do(run_all_cycles)
schedule.every(60).seconds.do(
    position_manager, db, args.dry_run, next(iter(orchestrators.values())).risk_manager.config, notifier
)
```

**D — Shutdown: stop all streams**

```python
for twm in twms:
    twm.stop()
```

**E — Auto-optimizer: keep running on primary symbol only**

```python
primary_sym  = symbols[0]
primary_orch = orchestrators[primary_sym]
schedule.every(7).days.do(_launch_auto_optimizer, db, primary_orch, notifier)
```

**F — Telegram /status and /report: unchanged** — they read from DB directly, not from `settings.symbol`.

---

### 4. `dashboard/sections/config_manager.py`

Replace the single `st.selectbox("Symbol", ...)` with `st.multiselect`:

```python
# Read current symbols list
cur_symbols_raw = cfg.get("symbols", cfg.get("symbol", settings.symbol))
cur_symbols = [s.strip() for s in cur_symbols_raw.split(",") if s.strip()]

symbols = st.multiselect(
    "Active Symbols",
    _SYMBOLS,
    default=[s for s in cur_symbols if s in _SYMBOLS],
    help="Symbols traded simultaneously. One position per symbol max. Restart required.",
)
if not symbols:
    st.warning("Select at least one symbol.")
```

On save, write `symbols` instead of `symbol`:

```python
db.set_runtime_config(
    symbols=",".join(symbols),
    # remove "symbol" key — no longer used when "symbols" is set
    ...
)
```

Banner on save: `"Configuration saved — restart the bot to apply all changes."` (same as today).

---

## What does NOT change

- `settings.symbol` — kept as `.env` fallback, not removed
- Kelly stats — pooled per strategy across all symbols (more data = better estimates)
- Auto-optimizer — runs on primary symbol only
- Backtest / optimizer tabs — per-run symbol selection already works
- DB schema — `trades.symbol` already exists; no migration needed

---

## Risk & constraints

| Risk | Mitigation |
|------|-----------|
| 3 symbols × 1% = 3% max simultaneous exposure | Circuit breaker per orchestrator; `max_drawdown` applies per-symbol |
| Binance API rate limit | ~3 extra REST calls/hour per symbol; well within 1200 req/min limit |
| WebSocket stream limit | Binance allows 1024 streams per connection; 3-5 symbols = no problem |
| position_manager uses wrong price | Fixed: per-trade `get_live_tick(trade["symbol"])` lookup |

---

## Testing

| Test | What to verify |
|------|---------------|
| `test_get_open_trades_symbol_filter` | Two symbols in DB; filter returns only the queried symbol's trades |
| `test_run_cycle_uses_orchestrator_symbol` | Mock klines; assert `get_klines` called with `orchestrator.symbol`, not `settings.symbol` |
| `test_position_manager_multi_symbol` | Two open trades, two ticks; assert each trade managed with its own symbol's price |
| `test_get_symbols_fallback` | Empty bot_config → returns `[settings.symbol]` |
| `test_get_symbols_from_botconfig` | `symbols=BTCUSDT,ETHUSDT` in bot_config → returns two-element list |
