# Multi-Symbol Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow the bot to trade multiple symbols simultaneously, configurable from the dashboard without touching `.env`, with one independent orchestrator (and circuit breaker) per symbol.

**Architecture:** Four surgical changes — DB adds a `symbols` list helper and a symbol filter on `get_open_trades()`; the orchestrator passes its own symbol when querying open trades; `main.py` creates one orchestrator per symbol and loops them in the scheduler; the dashboard CONFIG tab replaces the single symbol selectbox with a multi-select.

**Tech Stack:** Python stdlib, SQLite (existing `bot_config` KV store), Streamlit, `schedule` library.

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `bot/database/db.py` | Modify | `get_open_trades(symbol=None)` filter + `get_symbols()` / `set_symbols()` |
| `bot/orchestrator.py` | Modify | Pass `self.symbol` to `get_open_trades()` |
| `main.py` | Modify | `run_cycle()` uses `orchestrator.symbol`; `position_manager()` per-trade tick; `main()` builds orchestrator dict + multi-stream |
| `dashboard/sections/config_manager.py` | Modify | Replace symbol selectbox with multiselect |
| `tests/test_multi_symbol.py` | Create | All new tests for this feature |

---

## Task 1: DB — symbol filter + symbol list helpers

**Files:**
- Modify: `bot/database/db.py:303-314`
- Test: `tests/test_multi_symbol.py`

The DB already stores `symbol` per trade. We need two things:
1. `get_open_trades(symbol=None)` — optional WHERE filter
2. `get_symbols() / set_symbols()` — read/write the comma-separated list from `bot_config` (`rt_symbols` key, returned as `symbols` by `get_runtime_config()`)

- [ ] **Step 1: Write failing tests**

Create `tests/test_multi_symbol.py`:

```python
import pytest
from bot.database.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _insert_open_trade(db: Database, symbol: str, side: str = "BUY"):
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, side, strategy, regime, entry_price, quantity,
                entry_time, stop_loss, take_profit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, side, "EMA_CROSSOVER", "TRENDING",
             50000.0, 0.001, "2025-01-01T00:00:00", 49000.0, 52000.0),
        )


class TestGetOpenTradesSymbolFilter:
    def test_no_filter_returns_all(self, db):
        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")
        assert len(db.get_open_trades()) == 2

    def test_filter_returns_only_matching_symbol(self, db):
        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")
        result = db.get_open_trades(symbol="BTCUSDT")
        assert len(result) == 1
        assert result[0]["symbol"] == "BTCUSDT"

    def test_filter_returns_empty_when_no_match(self, db):
        _insert_open_trade(db, "BTCUSDT")
        assert db.get_open_trades(symbol="SOLUSDT") == []

    def test_closed_trades_excluded(self, db):
        _insert_open_trade(db, "BTCUSDT")
        with db._conn() as conn:
            conn.execute("UPDATE trades SET exit_price = 51000.0")
        assert db.get_open_trades(symbol="BTCUSDT") == []


class TestGetSetSymbols:
    def test_get_symbols_returns_list_from_db(self, db):
        db.set_symbols(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        assert db.get_symbols() == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_get_symbols_empty_when_not_set(self, db):
        assert db.get_symbols() == []

    def test_set_symbols_overwrites_previous(self, db):
        db.set_symbols(["BTCUSDT", "ETHUSDT"])
        db.set_symbols(["SOLUSDT"])
        assert db.get_symbols() == ["SOLUSDT"]

    def test_get_symbols_strips_whitespace(self, db):
        db.set_runtime_config(symbols=" BTCUSDT , ETHUSDT ")
        assert db.get_symbols() == ["BTCUSDT", "ETHUSDT"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_multi_symbol.py -v 2>&1 | tail -15
```

Expected: multiple FAILs — `get_open_trades` doesn't accept `symbol`, `get_symbols` and `set_symbols` don't exist.

- [ ] **Step 3: Implement `get_open_trades(symbol=None)` in `bot/database/db.py`**

Find line 303 (the existing `get_open_trades` definition) and replace it:

```python
def get_open_trades(self, symbol: str | None = None) -> list[dict]:
    """Return open trades, optionally filtered by symbol, ordered by entry_time descending."""
    with self._conn() as conn:
        if symbol is None:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL ORDER BY entry_time DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE exit_price IS NULL AND symbol = ?"
                " ORDER BY entry_time DESC",
                (symbol,),
            ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Add `get_symbols()` and `set_symbols()` after `set_runtime_config()` (~line 541)**

```python
def get_symbols(self) -> list[str]:
    """Return the configured symbol list from bot_config. Empty list if not set."""
    cfg = self.get_runtime_config()
    raw = cfg.get("symbols", "")
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]

def set_symbols(self, symbols: list[str]) -> None:
    """Persist the symbol list to bot_config."""
    self.set_runtime_config(symbols=",".join(symbols))
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_multi_symbol.py -v 2>&1 | tail -15
```

Expected: all 8 PASS.

- [ ] **Step 6: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 1 pre-existing failure (`test_telegram`), rest PASS.

- [ ] **Step 7: Commit**

```bash
git add bot/database/db.py tests/test_multi_symbol.py
git commit -m "feat(db): add symbol filter to get_open_trades and get/set_symbols helpers"
```

---

## Task 2: Orchestrator — filter open trades by own symbol

**Files:**
- Modify: `bot/orchestrator.py:114`
- Test: `tests/test_multi_symbol.py` (add to existing file)

The orchestrator already has `self.symbol` (set in `__init__`). The only change is passing it to `get_open_trades()`.

- [ ] **Step 1: Add test to `tests/test_multi_symbol.py`**

Append this class to the file:

```python
class TestOrchestratorSymbolIsolation:
    def test_orchestrator_only_sees_own_symbol_trades(self, db, tmp_path):
        from unittest.mock import MagicMock, patch
        from bot.orchestrator import StrategyOrchestrator
        import pandas as pd

        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")

        orch = StrategyOrchestrator(db=db, symbol="ETHUSDT")

        # Verify get_open_trades called with symbol="ETHUSDT"
        with patch.object(db, "get_open_trades", wraps=db.get_open_trades) as mock_got:
            # Provide a minimal DataFrame so step() can run at least the open_trades check
            # (it will return [] due to HOLD signal — that's fine)
            n = 100
            df = pd.DataFrame({
                "open": [50000.0] * n, "high": [50100.0] * n,
                "low": [49900.0] * n, "close": [50050.0] * n,
                "volume": [100.0] * n,
            })
            try:
                orch.step(df, 10000.0, None)
            except Exception:
                pass  # signal errors are fine — we only care about the DB call
            mock_got.assert_called_with(symbol="ETHUSDT")
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestOrchestratorSymbolIsolation -v 2>&1 | tail -10
```

Expected: FAIL — `get_open_trades` called without `symbol` argument.

- [ ] **Step 3: Change the one line in `bot/orchestrator.py`**

Find line 114:
```python
        open_trades = self.db.get_open_trades()
```

Replace with:
```python
        open_trades = self.db.get_open_trades(symbol=self.symbol)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestOrchestratorSymbolIsolation -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: same as before — 1 pre-existing failure, rest PASS.

- [ ] **Step 6: Commit**

```bash
git add bot/orchestrator.py tests/test_multi_symbol.py
git commit -m "feat(orchestrator): filter open trades by own symbol"
```

---

## Task 3: `run_cycle()` — use `orchestrator.symbol` instead of `settings.symbol`

**Files:**
- Modify: `main.py:235,244`
- Test: `tests/test_multi_symbol.py` (add test)

`run_cycle()` currently fetches klines using `settings.symbol` (the global from `.env`). With multiple orchestrators, each must fetch klines for its own symbol.

- [ ] **Step 1: Add test to `tests/test_multi_symbol.py`**

```python
class TestRunCycleUsesOrchestratorSymbol:
    def test_klines_fetched_for_orchestrator_symbol(self, db):
        from unittest.mock import MagicMock, patch, call
        from bot.orchestrator import StrategyOrchestrator
        import main as main_module

        orch = StrategyOrchestrator(db=db, symbol="ETHUSDT")

        mock_client = MagicMock()
        mock_client.get_klines.return_value = None  # will cause early return — that's OK
        mock_client.get_balance.return_value = 10000.0

        with patch.object(main_module, "_build_client", return_value=mock_client):
            main_module.run_cycle(orch, db, dry_run=True)

        calls = [c.args[0] for c in mock_client.get_klines.call_args_list]
        # All klines calls must use ETHUSDT, not whatever settings.symbol is
        for sym in calls:
            assert sym == "ETHUSDT", f"Expected ETHUSDT but got {sym}"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestRunCycleUsesOrchestratorSymbol -v 2>&1 | tail -10
```

Expected: FAIL — `get_klines` called with `settings.symbol` not `"ETHUSDT"`.

- [ ] **Step 3: Update `run_cycle()` in `main.py`**

Find lines 234-250 and replace the two `settings.symbol` references:

```python
    try:
        df = client.get_klines(orchestrator.symbol, settings.timeframe, KLINES_LIMIT)
    except Exception as exc:
        logger.error("Failed to fetch klines for %s: %s", orchestrator.symbol, exc)
        return

    # Daily klines for BiasFilter — backtest-proven: daily EMA9/21 gate
    # outperforms 4h EMA gate (PF 1.19-1.30 vs 0.82-0.93 with taker fees)
    df_4h = None
    try:
        df_4h = client.get_klines(orchestrator.symbol, "1d", 60)
    except Exception as exc:
        logger.warning(
            "Failed to fetch daily klines for %s: %s — BiasFilter will use NEUTRAL "
            "(signals pass if neutral_passthrough=True, blocked if block_on_data_failure=True)",
            orchestrator.symbol, exc,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestRunCycleUsesOrchestratorSymbol -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: same 1 pre-existing failure.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_multi_symbol.py
git commit -m "feat(main): run_cycle uses orchestrator.symbol for klines fetch"
```

---

## Task 4: `position_manager()` — per-trade symbol tick lookup

**Files:**
- Modify: `main.py:482-500`
- Test: `tests/test_multi_symbol.py` (add test)

Currently `position_manager()` fetches one tick for `settings.symbol` and applies it to ALL open trades. With multi-symbol, each trade must use its own symbol's live tick.

- [ ] **Step 1: Add test to `tests/test_multi_symbol.py`**

```python
class TestPositionManagerPerTradeTick:
    def test_each_trade_uses_its_own_symbol_tick(self, db):
        from unittest.mock import patch, MagicMock
        import main as main_module

        _insert_open_trade(db, "BTCUSDT")
        _insert_open_trade(db, "ETHUSDT")

        # Inject ticks for both symbols
        import datetime as _dt
        db.upsert_live_tick("BTCUSDT", 50000.0, 50000.0, 50100.0, 49900.0, 100.0,
                            _dt.datetime.utcnow().isoformat())
        db.upsert_live_tick("ETHUSDT", 3000.0, 3000.0, 3010.0, 2990.0, 500.0,
                            _dt.datetime.utcnow().isoformat())

        calls = []
        original_get_tick = db.get_live_tick

        def tracking_get_tick(symbol):
            calls.append(symbol)
            return original_get_tick(symbol)

        with patch.object(db, "get_live_tick", side_effect=tracking_get_tick):
            with patch.object(main_module, "_manage_single_position"):
                main_module.position_manager(db, dry_run=True)

        assert "BTCUSDT" in calls
        assert "ETHUSDT" in calls
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestPositionManagerPerTradeTick -v 2>&1 | tail -10
```

Expected: FAIL — only `settings.symbol` tick is fetched.

- [ ] **Step 3: Rewrite `position_manager()` body in `main.py`**

Find the `position_manager` function (line ~482) and replace the body from `trades = db.get_open_trades()` to `_manage_single_position(...)`:

```python
def position_manager(
    db: Database,
    dry_run: bool,
    risk_config: "RiskConfig | None" = None,
    notifier: "TelegramNotifier | None" = None,
) -> None:
    """Check SL/TP and ratchet trailing stop for all open trades. Runs every 60s."""
    trades = db.get_open_trades()
    if not trades:
        return

    for trade in trades:
        tick = db.get_live_tick(trade["symbol"])
        if tick is None:
            logger.debug("position_manager: no live tick for %s — skipping", trade["symbol"])
            continue
        price = tick["price"]
        _manage_single_position(trade, price, db, dry_run, risk_config, notifier)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
.venv/bin/pytest tests/test_multi_symbol.py::TestPositionManagerPerTradeTick -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 1 pre-existing failure only.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_multi_symbol.py
git commit -m "feat(main): position_manager uses per-trade symbol tick lookup"
```

---

## Task 5: `main()` — orchestrator dict, multi-stream, scheduler

**Files:**
- Modify: `main.py:557-628`

Replace the single-orchestrator startup with a dict of orchestrators (one per symbol), multiple WebSocket streams, and a `run_all_cycles()` wrapper for the scheduler. No new tests needed — this wires together code already tested in Tasks 1-4.

- [ ] **Step 1: Read current startup block**

Read `main.py` lines 557-641 to confirm exact current state before editing.

- [ ] **Step 2: Replace the orchestrator block (lines ~568-577)**

Find:
```python
    risk_config = RiskConfig(risk_per_trade=settings.risk_per_trade)
    _apply_runtime_config(db, risk_config)
    bias_filter = _build_bias_filter(db)
    orchestrator = StrategyOrchestrator(
        db=db,
        symbol=settings.symbol,
        risk_config=risk_config,
        bias_filter=bias_filter,
        timeframe=settings.timeframe,
    )
    adaptor = ParameterAdaptor(
        db=db,
        mean_reversion_strategy=orchestrator.get_strategy(StrategyName.MEAN_REVERSION),
        breakout_strategy=orchestrator.get_strategy(StrategyName.BREAKOUT),
        risk_manager=orchestrator.risk_manager,
    )
    _apply_ema_config(db, orchestrator)
    _apply_trail_config(db, orchestrator.risk_manager.config)
    _init_quantity_precision(orchestrator, db)
    _init_price_precision(db)
```

Replace with:
```python
    # Symbol list: from bot_config if set, else fall back to .env SYMBOL
    symbols = db.get_symbols() or [settings.symbol]
    logger.info("Active symbols: %s", symbols)

    bias_filter = _build_bias_filter(db)

    def _build_orchestrator(sym: str) -> StrategyOrchestrator:
        rc = RiskConfig(risk_per_trade=settings.risk_per_trade)
        _apply_runtime_config(db, rc)
        orch = StrategyOrchestrator(
            db=db,
            symbol=sym,
            risk_config=rc,
            bias_filter=bias_filter,
            timeframe=settings.timeframe,
        )
        _apply_ema_config(db, orch)
        _apply_trail_config(db, orch.risk_manager.config)
        _init_quantity_precision(orch, db)
        return orch

    orchestrators: dict[str, StrategyOrchestrator] = {
        sym: _build_orchestrator(sym) for sym in symbols
    }
    primary_orch = orchestrators[symbols[0]]

    adaptor = ParameterAdaptor(
        db=db,
        mean_reversion_strategy=primary_orch.get_strategy(StrategyName.MEAN_REVERSION),
        breakout_strategy=primary_orch.get_strategy(StrategyName.BREAKOUT),
        risk_manager=primary_orch.risk_manager,
    )
    _init_price_precision(db)
```

- [ ] **Step 3: Replace the WebSocket stream block (lines ~600-604)**

Find:
```python
    # Start the WebSocket price stream on the same client
    twm = stream_client.start_price_stream(
        settings.symbol,
        _make_tick_handler(db, settings.symbol),
    )
```

Replace with:
```python
    # Start one WebSocket price stream per symbol
    twms = [
        stream_client.start_price_stream(sym, _make_tick_handler(db, sym))
        for sym in symbols
    ]
```

- [ ] **Step 4: Replace the log + initial run + scheduler block (lines ~606-628)**

Find:
```python
    logger.info(
        "Bot started — symbol=%s timeframe=%s dry_run=%s",
        settings.symbol, settings.timeframe, args.dry_run,
    )
    notifier.bot_started(args.dry_run, db.get_active_mode())
    notifier.register_commands()

    # Run immediately on startup, then schedule hourly
    run_cycle(orchestrator, db, dry_run=args.dry_run, adaptor=adaptor, notifier=notifier)

    # Auto-optimizer: run at startup if overdue, then weekly
    if should_run(db):
        _launch_auto_optimizer(db, orchestrator, notifier)

    schedule.every().hour.at(":00").do(
        run_cycle, orchestrator, db, args.dry_run, adaptor, notifier
    )
    schedule.every(60).seconds.do(
        position_manager, db, args.dry_run, orchestrator.risk_manager.config, notifier
    )
    schedule.every(7).days.do(
        _launch_auto_optimizer, db, orchestrator, notifier
    )
```

Replace with:
```python
    logger.info(
        "Bot started — symbols=%s timeframe=%s dry_run=%s",
        symbols, settings.timeframe, args.dry_run,
    )
    notifier.bot_started(args.dry_run, db.get_active_mode())
    notifier.register_commands()

    def run_all_cycles() -> None:
        for sym, orch in orchestrators.items():
            try:
                run_cycle(orch, db, dry_run=args.dry_run, adaptor=adaptor, notifier=notifier)
            except Exception as exc:
                logger.error("run_cycle failed for %s: %s", sym, exc)

    # Run immediately on startup, then schedule hourly
    run_all_cycles()

    # Auto-optimizer: runs on primary symbol only
    if should_run(db):
        _launch_auto_optimizer(db, primary_orch, notifier)

    schedule.every().hour.at(":00").do(run_all_cycles)
    schedule.every(60).seconds.do(
        position_manager, db, args.dry_run, primary_orch.risk_manager.config, notifier
    )
    schedule.every(7).days.do(
        _launch_auto_optimizer, db, primary_orch, notifier
    )
```

- [ ] **Step 5: Replace the shutdown WebSocket stop (line ~639)**

Find:
```python
    twm.stop()
    logger.info("WebSocket price stream stopped.")
```

Replace with:
```python
    for twm in twms:
        twm.stop()
    logger.info("WebSocket price streams stopped.")
```

- [ ] **Step 6: Replace `price_fetcher` in `cmd_handler` (line ~596)**

Find:
```python
        price_fetcher=lambda: stream_client.get_ticker_price(settings.symbol),
```

Replace with:
```python
        price_fetcher=lambda: stream_client.get_ticker_price(symbols[0]),
```

- [ ] **Step 7: Verify the bot starts cleanly**

```bash
.venv/bin/python -c "import main; print('OK')"
```

Expected: `OK` with no import errors.

- [ ] **Step 8: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 1 pre-existing failure only.

- [ ] **Step 9: Commit**

```bash
git add main.py
git commit -m "feat(main): orchestrator dict + multi-stream + run_all_cycles scheduler"
```

---

## Task 6: Dashboard — symbol multiselect in CONFIG tab

**Files:**
- Modify: `dashboard/sections/config_manager.py:46,64-65,141-153`

Replace the single `st.selectbox("Symbol", ...)` with `st.multiselect`, read from `db.get_symbols()`, and save with `db.set_symbols()`.

- [ ] **Step 1: Update symbol read at line ~46**

Find:
```python
    cur_symbol           = cfg.get("symbol",                  settings.symbol)
```

Replace with:
```python
    cur_symbols_raw = cfg.get("symbols", cfg.get("symbol", settings.symbol))
    cur_symbols     = [s.strip() for s in cur_symbols_raw.split(",") if s.strip()]
```

- [ ] **Step 2: Replace the selectbox with multiselect (lines ~62-68)**

Find:
```python
    with st.form("bot_config_form"):
        col_sym, col_tf = st.columns(2)
        with col_sym:
            sym_idx = _SYMBOLS.index(cur_symbol) if cur_symbol in _SYMBOLS else 0
            symbol  = st.selectbox("Symbol", _SYMBOLS, index=sym_idx)
        with col_tf:
```

Replace with:
```python
    with st.form("bot_config_form"):
        col_sym, col_tf = st.columns(2)
        with col_sym:
            symbols = st.multiselect(
                "Active Symbols",
                _SYMBOLS,
                default=[s for s in cur_symbols if s in _SYMBOLS] or [_SYMBOLS[0]],
                help="Symbols traded simultaneously. One position per symbol max. Restart required to apply.",
            )
            if not symbols:
                st.warning("Select at least one symbol.")
        with col_tf:
```

- [ ] **Step 3: Update the save block (lines ~139-153)**

Find:
```python
    if saved:
        db.set_runtime_config(
            symbol=symbol,
            timeframe=timeframe,
```

Replace with:
```python
    if saved:
        if not symbols:
            st.error("Cannot save — select at least one symbol.")
        else:
            db.set_runtime_config(
                symbols=",".join(symbols),
                timeframe=timeframe,
```

Then find the closing of the `set_runtime_config(...)` call and the `st.success(...)` line. They need an extra indentation level (inside the `else` block). The full replacement:

Find the block:
```python
    if saved:
        db.set_runtime_config(
            symbol=symbol,
            timeframe=timeframe,
            risk_per_trade=str(round(risk_pct / 100, 4)),
            max_drawdown=str(round(max_dd_pct / 100, 3)),
            max_concurrent=str(max_concurrent),
            cooldown_hours=str(cooldown_hours),
            trail_atr_mult=str(trail_atr),
            trail_act_mult=str(trail_act),
            bias_neutral_passthrough="true" if bias_passthrough else "false",
            bias_neutral_threshold=str(round(bias_threshold_pct / 100, 4)),
            long_only="true" if long_only_mode else "false",
        )
        st.success("Configuration saved — restart the bot to apply all changes.")
```

Replace with:
```python
    if saved:
        if not symbols:
            st.error("Cannot save — select at least one symbol.")
        else:
            db.set_runtime_config(
                symbols=",".join(symbols),
                timeframe=timeframe,
                risk_per_trade=str(round(risk_pct / 100, 4)),
                max_drawdown=str(round(max_dd_pct / 100, 3)),
                max_concurrent=str(max_concurrent),
                cooldown_hours=str(cooldown_hours),
                trail_atr_mult=str(trail_atr),
                trail_act_mult=str(trail_act),
                bias_neutral_passthrough="true" if bias_passthrough else "false",
                bias_neutral_threshold=str(round(bias_threshold_pct / 100, 4)),
                long_only="true" if long_only_mode else "false",
            )
            st.success("Configuration saved — restart the bot to apply all changes.")
```

- [ ] **Step 4: Verify dashboard imports cleanly**

```bash
.venv/bin/python -c "
import ast, pathlib
src = pathlib.Path('dashboard/sections/config_manager.py').read_text()
ast.parse(src)
print('Syntax OK')
"
```

Expected: `Syntax OK`.

- [ ] **Step 5: Run full suite — no regressions**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 1 pre-existing failure only.

- [ ] **Step 6: Commit**

```bash
git add dashboard/sections/config_manager.py
git commit -m "feat(dashboard): replace symbol selectbox with multiselect for multi-symbol config"
```

---

## Final Verification

- [ ] **Run all multi-symbol tests**

```bash
.venv/bin/pytest tests/test_multi_symbol.py -v 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Run full suite**

```bash
.venv/bin/pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: 1 pre-existing failure, rest PASS.

- [ ] **Confirm 6 commits landed**

```bash
git log --oneline -6
```

Expected:
```
feat(dashboard): replace symbol selectbox with multiselect for multi-symbol config
feat(main): orchestrator dict + multi-stream + run_all_cycles scheduler
feat(main): position_manager uses per-trade symbol tick lookup
feat(main): run_cycle uses orchestrator.symbol for klines fetch
feat(orchestrator): filter open trades by own symbol
feat(db): add symbol filter to get_open_trades and get/set_symbols helpers
```
