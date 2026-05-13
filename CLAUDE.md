# CLAUDE.md — Trading Bot

Developer reference for this codebase. Read this before touching anything.

> **Source of truth**: the **code**, not this document. If a parameter, field,
> or behaviour in `bot/` disagrees with what's written here, the code wins —
> docs lag refactors. **Before changing any sensitive parameter (risk, SL/TP,
> thresholds, validation ranges) back the change with a fresh backtest.**
> Templates: `scripts/risk_scaler_matrix.py`, `scripts/risk_sweep.py`,
> `scripts/find_low_dd_v2.py`.

---

## Validated Baseline

**Production seeded values** — written to the `bot_config` KV table on first run by
`_seed_optimized_defaults()` in `main.py`, then applied to live config via `_apply_runtime_config`.
The dataclass defaults in `bot/config.py` mirror these so test/script paths that bypass
`main()` use the same values.

| Param | Value | Notes |
|---|---|---|
| `symbol` | `BTCUSDT` | Multi-symbol supported via `PortfolioBacktestEngine` (BTC+ETH validated) |
| `timeframe` | `4h` | 1h is unviable (legacy backtests: PF=0.75, Ann=-26%) |
| `risk_per_trade` | `0.015` (1.5%) | Picked over 4% per `scripts/risk_scaler_matrix.py` (May 2026) |
| `ema_stop_mult` | `1.5` | SL = 1.5 × ATR |
| `ema_tp_mult` | `4.5` | TP = 4.5 × ATR |
| `ema_max_dist_atr` | `1.0` | Max distance from EMA9 for trend-continuation entries |
| `long_only` | `true` | Bidirectional destroys PF on BTC |

Hard rules — do not change without re-running `BacktestEngine` or `PortfolioBacktestEngine`:

- **Long-only on BTC**: bidirectional destroys PF (1.55 → 1.09). BTC upward bias.
- **No trailing stop in live**: gotcha #1 (still exists in `BacktestConfig` for research).
- **4h timeframe**: validated. 1h is unviable.
- **R:R ≥ 1.5**: optimizer skips any combo below this floor.
- **Drawdown scaler stays DISABLED**: matrix backtest (`scripts/risk_scaler_matrix.py`, May 2026)
  showed it destroys returns on EMA crossover. Enabling it cuts annual from +32.7% to +10.8% at
  1.5% risk while only saving 1.3pp of DD. Mechanism: scaler quarter-sizes trades during DD,
  but DD on trend-following is a pullback before continuation — you take losses at full size
  and recovery winners at quarter size. Code is wired (`bot/risk/drawdown_scaler.py`,
  `bot/orchestrator.py`, `bot/backtest/{engine,portfolio_engine}.py`) but `enabled=False`.
  Can help mean-reverting strategies; never enable for trend-following.

### Risk × DD trade-off (BTC+ETH, 4h, bias_strict, 3y)

Calmar (Annual/DD) is the primary metric for risk-policy decisions — it captures the
survival-vs-return tradeoff better than Sharpe.

| Risk | Annual | Max DD | PF | Sharpe | Calmar |
|---|---|---|---|---|---|
| 1.5% | +32.7% | -15.2% | 1.52 | 1.33 | 2.16 |
| 2.0% | +44.0% | -19.9% | 1.51 | 1.33 | 2.21 |
| 2.5% | +55.2% | -24.5% | 1.49 | 1.33 | 2.25 |
| 3.0% | +66.4% | -29.0% | 1.47 | 1.34 | 2.29 |
| 4.0% | +88.0% | -37.3% | 1.44 | 1.34 | 2.36 |

Calmar improves marginally (+9% from 1.5% to 4%) while DD scales linearly with risk.
PF actually peaks at 1.5%. The seeded 1.5% trades return for survivability.

---

## Architecture

### Full Data Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                         main.py (scheduler)                         │
│  schedule.every().hour.at(":00")  ──►  run_cycle()                 │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │    BinanceClient         │  bot/exchange/binance_client.py
              │  get_klines(200 bars)    │
              │  get_balance("USDT")     │──► total_balance
              └────────────┬────────────┘
                           │  pd.DataFrame (OHLCV)
              ┌────────────▼────────────┐
              │  Capital allocation      │  main.py:run_cycle
              │  balance = total / N     │  N = len(active symbols)
              └────────────┬────────────┘
                           │  allocated balance
              ┌────────────▼────────────┐
              │  StrategyOrchestrator    │  bot/orchestrator.py
              │    .step(df, balance)    │
              └──┬──────────────────────┘
                 │
      ┌──────────▼──────────┐
      │   RegimeDetector     │  bot/regime/detector.py
      │   .detect(df)        │──► TRENDING | RANGING | VOLATILE
      └──────────┬───────────┘
                 │ MarketRegime
      ┌──────────▼──────────┐
      │  _select_strategy()  │  picks from REGIME_STRATEGY_MAP
      │  + winrate fallback  │  (overrides if win_rate < 40%, min 20 trades)
      └──────────┬───────────┘
                 │ BaseStrategy
      ┌──────────▼──────────┐
      │  strategy.generate_  │  bot/strategy/{ema_crossover,mean_reversion,breakout}.py
      │  signal(df)          │──► Signal(action, strength, stop_loss, take_profit, atr)
      └──────────┬───────────┘
                 │ Signal
      ┌──────────▼──────────┐
      │   RiskManager        │  bot/risk/manager.py
      │  validate_signal()   │  rejects if strength < 0.4 or circuit breaker active
      │  compute_position_   │  qty = min(risk/(entry-SL),  capital*0.99/entry)
      │  size()              │  ↑ risk-based            ↑ spot capital cap
      └──────────┬───────────┘
                 │ order dict
      ┌──────────▼──────────┐
      │   _execute_order()   │  main.py — calls BinanceClient.place_order()
      │   (skipped dry-run)  │  then writes to SQLite via Database
      └──────────┬───────────┘
                 │
      ┌──────────▼──────────┐
      │      SQLite DB       │  bot/database/db.py
      │  trades / equity /   │◄── also receives equity snapshot every cycle
      │  signals tables      │
      └──────────┬───────────┘
                 │
      ┌──────────▼──────────┐
      │  Streamlit Dashboard │  dashboard/app.py
      │  @st.cache_resource  │  reads DB, auto-refreshes every 60s
      └─────────────────────┘
```

### Module Map

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point, CLI flags, scheduler, run_cycle loop; **`run_cycle` divides total USDT balance by `n_symbols` so each symbol gets an equitable allocation** |
| `bot/config.py` | Settings dataclass, reads `.env` via python-dotenv |
| `bot/constants.py` | All enums: ExitReason, TradeAction, OrderSide, StrategyName |
| `bot/orchestrator.py` | Coordinates regime → strategy → bias filter → risk → order dict. HWM lives in DB only — no in-memory `_peak_capital` cache; re-read each `step()` to avoid cross-symbol race (gotcha #30, #31). |
| `bot/bias/filter.py` | `BiasFilter` — EMA9/21 on 4h candles; returns BULLISH/BEARISH/NEUTRAL; injected into orchestrator as hard gate before signal execution |
| `bot/regime/detector.py` | 3-level regime detection: ATR volatility → ADX → Hurst |
| `bot/risk/manager.py` | `RiskManager`: circuit breaker (persisted across restarts), position sizing **with spot capital cap**, `validate_signal`. Kelly fields live in `RiskConfig` (`kelly_max_mult`, `kelly_min_mult`, `kelly_min_trades`, `kelly_half`). Circuit breaker now consumes TRADING-EQUITY values (baseline + cumulative realized PnL), not raw exchange balance (see gotcha #4, #30, #31). |
| `bot/risk/kelly.py` | Pure functions: `compute_kelly_fraction()` (half-Kelly default), `kelly_risk_fraction()` (clamped multiplier). Wired in `orchestrator.step()`. |
| `bot/risk/drawdown_scaler.py` | `DrawdownRiskConfig` + `drawdown_multiplier()` — disabled by default; applied in BacktestEngine + PortfolioBacktestEngine. NOT wired in live orchestrator (intentional — see Validated Baseline). |
| `bot/risk/vol_regime.py` | `VolRegimeConfig` + `VolRegimeFilter` — opt-in volatility-regime gate that can block entries or scale size in LOW/HIGH vol windows. Disabled by default. |
| `bot/risk/news_pause.py` | News-event pause window — gates entries around macro releases. Disabled by default; opt in via `BacktestConfig.news_pause`. |
| `bot/risk/news_blackout.py` | Static news-event calendar lookup helpers (companion to `news_pause`). |
| `bot/strategy/base.py` | Abstract BaseStrategy + Signal dataclass |
| `bot/strategy/ema_crossover.py` | EMA 9/21 crossover strategy — the **only** strategy registered in the live orchestrator. |
| `bot/strategy/donchian_breakout.py` | Donchian breakout strategy — research only, not registered in orchestrator. Used by `scripts/risk_sweep.py` and as a `BacktestEngine` override. |
| `bot/strategy/levels.py` | Pure function: `calculate_levels(side, price, atr, sl_mult, tp_mult)` |
| `bot/strategy/signal_factory.py` | Constructors: `buy_signal()`, `sell_signal()`, `hold_signal()` |
| `bot/indicators/utils.py` | Pure functions: `atr()`, `rsi()`, `wilder_smooth()` |
| `bot/config_presets.py` | Timeframe-aware config factory: `get_regime_config(tf)`, `get_strategy_configs(tf)`, plus `BIAS_TIMEFRAME_MAP` / `bias_timeframe_for(tf)` — single source of truth for primary→bias timeframe mapping. |
| `bot/database/db.py` | SQLite wrapper, DDL, migrations, all queries; `bot_config` KV store; `optimizer_runs`, `entry_quality_runs` tables; `get_kelly_stats()` for Kelly sizing. |
| `bot/metrics.py` | Pure functions: Sharpe, max drawdown, profit factor, max loss streak |
| `bot/exchange/binance_client.py` | Binance API client (testnet-aware). `_retry` decorator narrowed to network/Binance exceptions only — programming bugs propagate immediately (gotcha #28). |
| `bot/telegram_notifier.py` | `TelegramNotifier` — sends trade/circuit-breaker/lifecycle/orphan events; `register_commands()` registers bot menu via `setMyCommands`; lazy DB config reads |
| `bot/telegram_commands.py` | `TelegramCommandHandler` — daemon thread, long-polls Telegram. Handles `/pause` `/resume` `/status` `/report`. Top-level `try/except` keeps the thread alive on transient errors (gotcha #29). |
| `bot/backtest/cache.py` | Parquet cache for OHLCV klines (`data/klines/`): `fetch_and_cache()` (incremental update), `cache_info()`, `download_full_history()` |
| `bot/backtest/engine.py` | `BacktestEngine` (single-symbol). Bar-by-bar simulation: regime → strategy → bias → momentum → vol_regime → drawdown_scaler → sizing. Supports leverage (perps), partial-TP ladder, news pause, vol regime, drawdown scaler. |
| `bot/backtest/portfolio_engine.py` | `PortfolioBacktestEngine` (multi-symbol cash pool). Mirrors live multi-symbol bot — shared USDT pool, per-symbol engines for signals/exits. Applies `drawdown_multiplier` and vol-regime size factor (gotcha #26). |
| `bot/backtest/scenario_runner.py` | 8 predefined profitability scenarios (1h/4h × momentum filter × leverage 1–10×); `ScenarioRunner.run_all()` returns `list[ScenarioResult]`. Imports `BIAS_TIMEFRAME_MAP` from `config_presets`. |
| `bot/optimizer/walk_forward.py` | Grid search over EMA SL/TP ATR multipliers; runs backtest engine on recent data; saves viable configs to `optimizer_runs` for dashboard review |
| `bot/optimizer/auto_optimizer.py` | Daemon thread runs walk-forward weekly, hot-reloads approved EMA config (gotcha #18). |
| `bot/optimizer/entry_quality_optimizer.py` | Grid search over EMA entry-quality filters (volume, bar direction, momentum, ATR). Saves to `entry_quality_runs`. |
| `bot/optimizer/auto_entry_quality_optimizer.py` | Daemon companion to `auto_optimizer.py` for entry-quality params. |
| `scripts/compare_scenarios.py` | CLI entry point for the 8-scenario comparison via `ScenarioRunner`. |
| `scripts/risk_scaler_matrix.py` | Risk × drawdown scaler matrix — drove the May 2026 decision to keep the scaler disabled. |
| `scripts/risk_sweep.py` / `scripts/risk_scaling.py` / `scripts/find_low_dd_v2.py` | Research scripts for risk-policy comparisons. Use as templates when proposing a parameter change. |
| `dashboard/app.py` | Streamlit app; 3 tabs: MONITOR \| CONFIG \| BACKTEST; BACKTEST has subtabs BACKTEST and COMPARE; `_topbar()` fragment (5s refresh); MONITOR renders the unified range selector before equity/drawdown charts |
| `dashboard/range.py` | Unified MONITOR range selector (`1H \| 24H \| 7D \| 30D \| ALL`). `render_selector()` writes to `st.session_state["monitor_range"]`. `filter_curve_by_range()` and `klines_params_for_range()` consumed by chart sections. Available options bounded by equity_curve age — longer ranges hidden until enough history exists. |
| `dashboard/sections/open_position.py` | Regime badge + CSS flex timeline strip + open position; `drawdown_section` as separate `@st.fragment(run_every=10)`; reads `current_range()` and filters equity_curve before computing drawdown |
| `dashboard/sections/optimizer.py` | Optimizer UI: grid search form, progress bar, PF heatmap, top-10 table, pending proposal banner (approve/reject), history table |
| `dashboard/sections/scenario_compare.py` | COMPARE subtab UI: form (symbol/days/risk), progress bar per scenario, results table, equity curve overlay chart (Plotly), best/safest callout metrics |
| `dashboard/themes.py` | NothingOS palette + PLOTLY_LAYOUT (drag-only via `dragmode="pan"`) + PLOTLY_CONFIG (scrollZoom/doubleClick disabled) — chart navigation is pan-only across the whole dashboard |

---

## Regime Detection Hierarchy

The detector applies three tests in strict priority order. The first test that fires wins.

```
Level 1 — ATR Volatility Override (highest priority)
  condition : current_atr > 2.0 × mean_atr (last 50 bars)
  result    : VOLATILE
  rationale : extreme moves override any trend measurement

Level 2 — ADX Trend Strength
  condition : ADX >= 25.0
  result    : TRENDING
  uses      : Wilder smoothing (ewm alpha=1/period), NOT simple rolling mean
  rationale : strong directional movement

Level 3 — Hurst Exponent (R/S analysis on last 100 bars)
  H > 0.55  → TRENDING   (persistent, trending series)
  H < 0.45  → RANGING    (anti-persistent, mean-reverting)
  else      → RANGING    (default when indeterminate)
```

Config class: `RegimeDetectorConfig` in `bot/regime/detector.py`.

> **Timeframe-dependent thresholds**: the values above are 1h defaults. `bot/config_presets.py`
> provides calibrated presets per timeframe (1h, 4h, 15m). The orchestrator and backtest engine
> both call `get_regime_config(timeframe)` — never instantiate `RegimeDetectorConfig` directly
> with hardcoded values.

---

## Strategy Architecture

The orchestrator runs a **single strategy**: `EMA_CROSSOVER` for all regimes. The
multi-strategy `REGIME_STRATEGY_MAP` and win-rate fallback described in older docs
have been **removed**. Only `bot/strategy/ema_crossover.py` is registered in
`bot/orchestrator.py`. Other strategy files in `bot/strategy/` (e.g.
`donchian_breakout.py`) exist for research and `BacktestEngine` overrides only;
they are not wired into the live flow.

The regime detector still classifies bars as TRENDING / RANGING / VOLATILE — this
data is logged and stored on each trade for diagnostics, but it does **not**
switch strategies anymore.

### Sizing pipeline (in order, all in `orchestrator.step()`)

1. **Regime detection** → `bot/regime/detector.py`
2. **Signal generation** → `EMACrossoverStrategy.generate_signal(df)`
3. **Bias filter** → `BiasFilter` on higher-timeframe candles (gotcha #14)
4. **Risk validation** → `RiskManager.validate_signal()` (strength + action)
5. **Kelly sizing** (cabled, gotcha #25) → half-Kelly clamped between
   `kelly_min_mult=0.25` and `kelly_max_mult=2.0` of `RiskConfig.risk_per_trade`,
   active after `kelly_min_trades=15` closed trades for that strategy. Falls back
   to flat `risk_per_trade` when there's not enough history.
6. **Momentum NEUTRAL** scales risk to 50% (live + backtest)
7. **Drawdown scaler**: applied in BACKTEST only when `dd_risk` is enabled. NOT
   wired into live orchestrator (intentional — see Validated Baseline section).
8. **`compute_position_size`** with capital cap (gotcha #23)

---

## Strategy Details — EMA Crossover (live)

- Signal: EMA9/EMA21 crossover (single-bar) OR trend-continuation entry when price
  is within `max_distance_atr` of EMA9
- Crossover strength: `abs(fast_slope) / ATR × 5`, floor 0.6
- Trend strength: `0.5 × (1 - dist_atr / max_distance_atr) + 0.4`, capped 0.4–0.8
- Distance check uses `abs()` — filters overextension in both directions (above AND below EMA9)
- SL: `stop_atr_mult × ATR` below/above entry (default 1.5; overridable via `ema_stop_mult` runtime config)
- TP: `tp_atr_mult × ATR` above/below entry (default 4.5; overridable via `ema_tp_mult` runtime config)
- Optional entry-quality filters (per `EMACrossoverConfig`): `volume_multiplier`,
  `require_bar_direction`, `require_ema_momentum`, `min_atr_pct`. Tuned by the
  Entry Quality auto-optimizer (`bot/optimizer/entry_quality_optimizer.py`).

---

## Key Gotchas

These WILL bite you if you don't know them.

### 1. Trailing stop is OFF in the live bot — but `BacktestConfig` still has the fields

**Live**: trailing stop is removed from the live position manager. The
ratcheting block, `TRAILING_STOP` exit reason, and `trailing_stop_enabled`
config flag have been removed from `RiskConfig`, `position_manager()`, and
`bot/constants.py`. Live positions exit ONLY via SL or TP.

**Backtest**: `BacktestConfig` still has `trail_atr_mult` and
`trail_activation_mult` fields, plus a legacy `simulate_trailing` flag.
These exist so research scripts (`scripts/risk_scaling.py` and similar) can
still measure trailing-stop variants. **All production-relevant backtests
keep `simulate_trailing=False`.**

3-year legacy backtest with trail ON: PF=0.764, Ann=-5%. Only 1 of 131 trades
hit TP — trail cut every winner before target. Hence the live removal.

Legacy artifacts:
- `trades.trailing_sl` column in the SQLite schema — preserved for old rows.
  Never written by the live bot.
- `BacktestConfig.simulate_trailing` defaults to False.

Re-enabling in live would require re-introducing both the config flag AND the
`position_manager` logic, plus a fresh backtest demonstrating it adds value
(unlikely on EMA crossover BTC; the removal was data-driven).

### 2. ADX uses Wilder smoothing, NOT simple rolling mean

`_adx()` in `bot/regime/detector.py` calls `wilder_smooth()` — which is `ewm(alpha=1/period, adjust=False)`.
This matches TA-Lib behaviour. Using `.rolling(period).mean()` instead gives different ADX values.

```python
# bot/indicators/utils.py
def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1 / period, adjust=False).mean()
```

### 3. `atr()` uses SMA rolling, `wilder_smooth()` is separate

`atr()` in `bot/indicators/utils.py` uses `tr.rolling(period).mean()` — simple average.
`_adx()` computes its own True Range internally using `wilder_smooth()`.
These are NOT the same ATR. Strategies use the SMA-based `atr()`.

### 4. Circuit breaker resets THREE ways — and SURVIVES restarts

The circuit breaker is NOT permanent. It resets if ANY condition is met:
- `cooldown_hours` (default 4h) have elapsed since trigger, OR
- drawdown recovers below `max_drawdown` threshold (15% default), OR
- Manual `/reset_hwm` Telegram command (clears all `breaker_triggered_at_*` rows atomically)

```python
# If drawdown recovers before cooldown expires:
if drawdown < self.config.max_drawdown:
    self._breaker_triggered_at = None  # immediate reset
    return False
```

**Breaker input is TRADING EQUITY (post-May 2026)**: `check_circuit_breaker` now receives
`trading_equity = account_baseline + SUM(closed_pnl)`, NOT raw exchange balance. This means
faucet deposits and withdrawals do NOT affect the breaker threshold. See gotcha #31.

**Persistence**: `_breaker_triggered_at` is saved to `bot_config` under the
key `breaker_triggered_at_{symbol}` on every state change. The orchestrator
passes `db=db` when constructing `RiskManager`, which restores the timestamp
on `__init__`. This means the cooldown **survives `init 6` reboots and
`docker compose restart`** — a breaker triggered at 2am with a 4h cooldown
will still be active at 3am after a 02:30 restart, instead of being silently
wiped by the in-memory reset.

**New `bot_config` keys added in May 2026**:
- `account_baseline` — USDT trading-start balance; back-computed once at first Phase-2 start; never auto-mutated
- `peak_capital` — now stores peak TRADING EQUITY (not raw balance); ratcheted by orchestrator each cycle

If `db` or `symbol` are not provided (e.g. ad-hoc test instances), the
manager falls back to in-memory state — backward compatible.

### 5. StrEnum — string comparison with DB works natively

All enums inherit from `(str, Enum)`. This means:
```python
ExitReason.STOP_LOSS == "STOP_LOSS"  # True
```
You can store `.value` or the enum itself and compare either way. DB stores raw strings; loading them back as strings compares correctly against enum instances.

### 6. `_migrate_schema()` runs on every DB init — safe to add columns

`Database.__init__()` always calls `_init_schema()` → `_migrate_schema()`.
The migration uses `PRAGMA table_info()` to check existing columns before `ALTER TABLE`.
To add a new column: add it to the `for col, definition in [...]` list in `_migrate_schema()`.
Do NOT recreate the table.

### 7. `PLOTLY_LAYOUT` lives in `dashboard/themes.py` → `NothingOS.PLOTLY_LAYOUT`

`dashboard/app.py` aliases it at module level:
```python
PLOTLY_LAYOUT = NothingOS.PLOTLY_LAYOUT
```
All charts call `fig.update_layout(**PLOTLY_LAYOUT, ...)`. Add new chart defaults to
`NothingOS.PLOTLY_LAYOUT` in `dashboard/themes.py`, not inline in `app.py`.

### 8. `get_db()` is `@st.cache_resource` — single DB connection per Streamlit session

```python
@st.cache_resource
def get_db() -> Database:
    return Database(DB_PATH)
```
The `Database` class opens and closes a connection per operation (`_conn()` context manager),
but the `Database` instance itself is shared. Do not pass separate Database instances to
dashboard helpers — use `get_db()` everywhere inside Streamlit.

### 9. Opposite-signal exit is REMOVED — positions exit only via SL / TP / regime_change

Older versions of this codebase closed an open position when the strategy
emitted a strong opposite signal (`strength >= 0.75`, configurable via a
`RiskConfig.min_exit_signal_strength` field). Both the `min_exit_signal_strength`
field and the opposite-signal exit branch have been **removed**. Live positions
exit through:

1. **SL / TP** — the natural exit (gotcha #24 covers intra-bar wick detection).
2. **Liquidation** — only relevant when leverage > 1 (spot doesn't liquidate).
3. **Regime change** — opt-in via `RiskConfig.enable_regime_exit=True` (gotcha #15).
4. **End of period** — backtest-only.

If you want to re-introduce opposite-signal closing, it'll need a fresh backtest:
empirically, exiting on opposite signals shortens winning trades on
trend-following EMA strategies — same failure mode as the trailing stop.

### 10. `--dry-run` skips `place_order()` but DOES write to DB

In dry-run mode `_execute_order()` is never called, so no orders go to Binance.
However, `db.insert_equity_snapshot()` runs every cycle regardless.
The equity curve IS recorded in dry-run. Use this to evaluate strategy performance
without touching the exchange.

### 11. `TelegramNotifier` reads config from DB on every send — no restart needed

`TelegramNotifier._post()` calls `db.get_telegram_config()` before every HTTP request.
There is no in-memory cache. This means updating the token, chat ID, or `enabled` flag
in the dashboard takes effect on the very next notification — no bot restart required.
The notifier silently no-ops when unconfigured (`has_telegram_config()` returns False).

### 12. Circuit breaker notification fires only on the triggering cycle

`main.run_cycle()` snapshots `orchestrator.risk_manager._breaker_triggered_at` BEFORE
calling `orchestrator.step()` and compares it AFTER. The Telegram notification is sent
only when the value transitions from `None` to a timestamp — i.e., the first cycle that
triggers the breaker. Subsequent cycles where the breaker is still active do NOT re-notify.

### 14. `BiasFilter` is fail-closed — network errors block signals, not bypass them

`BiasFilter.get_bias()` returns `Bias.NEUTRAL` in three situations: `df_4h is None`,
fewer bars than `slow_period + 1`, or EMA gap below `neutral_threshold_pct` (0.1%).
`NEUTRAL` blocks all directional signals — no BUY, no SELL, only HOLD passes.

If the 4h `get_klines()` call raises an exception in `run_cycle()`, `df_4h` is set to
`None` and passed to the orchestrator. The filter receives `None` → returns `NEUTRAL` →
no trades that cycle. A network error **never silently disables** the bias filter.

To disable the filter intentionally: `BiasFilterConfig(enabled=False)`. With `enabled=False`
`get_bias()` returns `BULLISH` (sentinel) and `allows_signal()` always returns `True`.

### 13. `bot_paused` stops `run_cycle` but NOT `position_manager`

When `db.get_bot_paused()` is True, `run_cycle()` returns immediately (no new signals,
no exchange calls). However, `position_manager()` runs on its own schedule and is NOT
gated by the pause flag — SL/TP checks and trailing stop updates continue uninterrupted
even while the bot is paused. Pausing only prevents new trade entries.

### 15. `enable_regime_exit` is OFF by default — opt-in at the `RiskConfig` level

`RiskConfig.enable_regime_exit = False` by default. When enabled, `_evaluate_open_position()`
compares the current regime against `trade["regime"]` (stored at open time) and closes the
position with `ExitReason.REGIME_CHANGE` if they differ.

Risk: regime can oscillate near ADX/ATR boundaries (e.g. TRENDING↔RANGING on the same ADX=25
threshold), causing whipsaw exits. Enable only if you accept that tradeoff.

```python
# To enable:
risk_config = RiskConfig(risk_per_trade=settings.risk_per_trade, enable_regime_exit=True)
```

### 16. `quantity_precision` is fetched from exchangeInfo at startup

`RiskConfig.quantity_precision` defaults to 5 (BTC). At startup, `_init_quantity_precision()`
calls `BinanceClient.get_quantity_precision(symbol)` which reads the `LOT_SIZE` filter from
`exchangeInfo` (unauthenticated endpoint). On failure it logs a warning and keeps the default.
This means multi-pair operation (SOL, ETH, etc.) gets the correct decimal places automatically.

### 17. `orchestrator.step()` third arg is `df_high`, not `df_4h`

The parameter was renamed from `df_4h` to `df_high` to reflect that it carries the
higher-timeframe candles for `BiasFilter` — which is **not always 4h** depending on
the primary timeframe:

```python
_BIAS_TF = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}
```

`main.py` fetches the correct bias timeframe based on `TIMEFRAME` setting and passes it
as `df_high`. The optimizer also follows this mapping.

### 18. Auto-optimizer hot-reloads EMA config without restart

The **auto-optimizer** (`bot/optimizer/auto_optimizer.py`) runs weekly in a daemon thread.
When it finds a better config it writes `ema_stop_mult` and `ema_tp_mult` to the `bot_config`
KV store AND hot-patches the live `EMACrossoverStrategy` object via `_apply_ema_config()`:

```python
# in main.py — on_applied callback
_apply_ema_config(db, orchestrator)   # hot-patches config.stop_atr_mult / config.tp_atr_mult
```

Changes take effect on the very next `run_cycle()` tick — no restart needed.
Manual approvals from the dashboard OPTIMIZER tab write to the DB but require a restart;
`_apply_ema_config()` is called once at startup to pick those up.

### 19. Optimizer viability constraints — all four must pass

`walk_forward.py` gates results before saving to DB. A config is `viable` only if:
- `total_trades >= 15`
- `max_drawdown_pct <= 20.0`
- `sharpe_ratio >= 0.4`
- `profit_factor >= 1.05`

Skipped R:R combos: any combo where `tp_mult / stop_mult < 1.5` is skipped outright
(minimum 1.5:1 risk-reward enforced). Results are sorted viable-first then by PF DESC.

### 20. Parquet cache lives at `data/klines/` — shared by backtest and optimizer

`bot/backtest/cache.py` stores OHLCV data as `data/klines/{SYMBOL}_{INTERVAL}.parquet`.
The cache is incremental: only missing bars are fetched. The directory is created
automatically on first use. Both `BacktestEngine` (via `fetch_and_cache`) and the optimizer
use this cache — running the backtest runner first populates the cache for the optimizer.
Thread-safe for reads; single-writer per file.

### 21. `BacktestEngine.run()` accepts `df_weekly` for momentum filter and `leverage` for futures simulation

`BacktestConfig` has three new field groups (all default to spot/no-filter behaviour):

```python
# Leverage (1.0 = spot, unchanged)
leverage: float = 1.0
funding_rate_per_8h: float = 0.0001   # BTC perp typical

# Weekly momentum filter (False = off, unchanged)
momentum_filter_enabled: bool = False
momentum_sma_period: int = 20          # 20-week SMA
momentum_neutral_band: float = 0.05   # ±5% around SMA
```

`run()` signature now includes `df_weekly: pd.DataFrame | None = None`. When `momentum_filter_enabled=True` and `df_weekly` is provided:
- Price > SMA × 1.05 → **BULLISH** → full risk
- Price < SMA × 0.95 → **BEARISH** → entry blocked (no new trades)
- Within band → **NEUTRAL** → risk halved

Liquidation price for BUY: `entry × (1 − 0.9 / leverage)`. Trades closed with `EXIT_LIQUIDATED`; loss = full margin. Filter only gates **new entries** — open positions are never force-closed by momentum state.

Use `ScenarioRunner` (not `BacktestEngine` directly) when comparing multiple leverage/momentum combinations — it handles data routing (1h→4h bias, 4h→1d bias) and computes annual return correctly.

### 22. Multi-symbol balance is split equitably across symbols in `run_cycle`

`run_cycle()` accepts `n_symbols: int` and divides the fetched USDT balance evenly:

```python
total_balance = client.get_balance("USDT")
balance = total_balance / max(1, n_symbols)
```

`run_all_cycles()` passes `n_symbols=len(orchestrators)` so each per-symbol cycle sees a
fair share of the pool. Without this, the first symbol in the loop would size positions
against 100% of capital, draining the pool and starving every symbol after it
(`-2010 insufficient balance` for the rest).

The split is **equitable, not weighted**. To customise per-symbol weights you would have
to plumb a weight map through `run_all_cycles()`. Currently each symbol gets `1/N`.

A side effect: `BTCUSDT` and `ETHUSDT` both see `total/2` even when one of them has no
open position. This trades capital efficiency for predictability — no symbol can blow up
the pool. If you only want this behaviour above N>1, the `if n_symbols > 1` log line lets
you spot the allocation in the cycle output.

### 23. `RiskManager.compute_position_size` caps quantity by available capital

Spot trading has no margin: notional cannot exceed cash. When `risk_per_trade / sl_distance_pct`
yields more notional than 100% of capital (e.g. 3% risk × 2.76% SL = 108% of capital), the
risk-based formula computes an impossible position. Binance rejects with `-2010`.

To prevent this, `compute_position_size()` takes the minimum of two formulas:

```python
qty_by_risk    = (capital * risk_fraction) / (entry - stop_loss)
qty_by_capital = (capital * 0.99) / entry          # 99% leaves margin for fees
quantity       = min(qty_by_risk, qty_by_capital)  # cap kicks in only if needed
```

When the cap activates, a WARNING log line fires:

```
[BTCUSDT] Qty capped by capital: risk-based=0.54352 → 0.49507
  (risk 3.00% × SL_dist 2.76% would need notional > 100% of capital)
```

The cap is a safety net — seeing it consistently means your `risk_per_trade` and
`stop_atr_mult` combination is too aggressive for spot. The fix is to widen the SL
(higher `ema_stop_mult`) or lower the risk per trade, not to ignore the warning.

### 24. `position_manager` uses intra-bar high/low (1m kline) — not just live_tick

Live SL/TP detection runs in two stages inside `_manage_single_position`:

1. **Primary** — `_check_intra_bar_exit()` fetches the last 2 1m klines via
   `BinanceClient.get_klines(symbol, "1m", limit=2)` and compares each bar's
   `high`/`low` against the trade's SL/TP. Captures intra-second wicks that
   `live_tick` (sampled from WS trade events) can miss.
2. **Fallback** — if the kline fetch fails or returns empty, falls back to
   `db.get_live_tick().price` and the original spot comparison.

When a wick is detected, the **exit price stored is the bar's `close`**, NOT
the SL/TP level. This is intentional: when we send a market close after the
detection, Binance fills at spot, not at the level. The bar close is the
honest proxy for what we'll actually fill at. Empirically validated — closing
at bar close beats "close at level" on max drawdown by ~24% on a 3-year run
(see `scripts/test_wick_variants.py`).

**Both-wicked tiebreaker**: when the same 1m bar wicks BOTH SL and TP
(rare but real during flash crashes that recover), the exit reason is
chosen by the close direction relative to entry — close on the winning
side → `TAKE_PROFIT`, otherwise → `STOP_LOSS`. The exit price is still
the bar close, so the reason now matches the financial outcome. The
backtest engine in `bot/backtest/engine.py` keeps the legacy "SL wins"
conservative rule — there is no real fill price to anchor to in
simulation, so the pessimistic assumption stays statistically correct.

`position_manager` builds the BinanceClient once per cycle (only when there
are open trades), passes it to all `_manage_single_position` calls. Adds 1
unauthenticated REST call per open trade per 60s — trivial against Binance's
1200-weight/min limit.

### 25. Kelly sizing is wired in `orchestrator.step()` — half-Kelly with clamps

The orchestrator computes a per-strategy Kelly fraction from closed-trade history
before sizing each entry. Lives in `bot/risk/kelly.py` (`compute_kelly_fraction`,
`kelly_risk_fraction`) and is invoked at `bot/orchestrator.py:141-184`.

Mechanism:
1. Pull stats with `db.get_kelly_stats(strategy.name, kelly_min_trades=15)`. Returns
   `None` when fewer than 15 closed trades exist for that strategy — sizing then
   falls back to flat `RiskConfig.risk_per_trade`.
2. `compute_kelly_fraction(win_rate, avg_win_pct, avg_loss_pct, half=True)` —
   half-Kelly by default, floored at 0 for negative-edge strategies.
3. `kelly_risk_fraction(...)` clamps the dynamic risk between `kelly_min_mult=0.25`
   and `kelly_max_mult=2.0` of the base `risk_per_trade`. Signal strength scales
   the Kelly multiplier: `mult = (kelly_f / base_risk) * signal_strength`.

`RiskConfig` fields controlling this: `kelly_max_mult`, `kelly_min_mult`,
`kelly_min_trades`, `kelly_half`. Defaults are conservative (half-Kelly,
0.25×–2× clamp). Don't crank `kelly_max_mult` past 2.0 without a fresh backtest:
full Kelly is brutal under estimation error and tends to blow up on drawdowns
that exceed the historical sample.

### 26. `dd_risk` (drawdown scaler) and `vol_regime` apply in BOTH backtest engines

`BacktestEngine` (single-symbol) and `PortfolioBacktestEngine` (multi-symbol cash
pool) both apply `drawdown_multiplier()` and the `vol_regime` size factor to
`effective_risk` at entry time. The portfolio engine had a bug (May 2026) where
both filters were silently bypassed in its duplicated sizing path — a 15-config
risk × scaler matrix produced identical metrics across OFF/Conservative/Moderate
columns, surfacing the bypass. Fix: tracked `peak_capital` HWM and propagated
both factors into the entry block. Test `test_drawdown_scaler_invoked_in_portfolio_sizing`
in `tests/test_portfolio_engine.py` is a regression guard.

When duplicating a sizing path between engines, ALL filters that affect
`effective_risk` must be re-applied — `momentum_state` halving, `vol_size_factor`,
and `drawdown_multiplier`. Identical-metrics-across-configs is the telltale of a
bypass.

### 27. `_execute_order` retries DB writes after exchange fills — orphan alerts on persistent failure

`main._execute_order` is not transactional across Binance + SQLite. To prevent
orphans (filled on exchange but missing from DB), DB writes after a successful
order go through `_retry_db_write` (3 attempts, exponential backoff starting at
0.5s). On final failure `_alert_orphan_position` logs CRITICAL and sends a
Telegram alert with the exchange `orderId` and trade details — the bot does NOT
attempt to undo the order (compounding risk). Manual reconciliation required.

This means the bot can leave inconsistent state if SQLite is broken for
extended periods. The alert is the safety net, not a fix.

### 28. `_retry` decorator only retries network/Binance exceptions

`bot/exchange/binance_client.py:_retry` previously caught `Exception` (everything),
which meant a programming bug like a `KeyError` would burn ~14s on three retries
before propagating. Now narrowed to `(BinanceAPIException, BinanceRequestException,
requests.exceptions.RequestException, TimeoutError, ConnectionError)`. Programming
bugs surface immediately.

### 29. Telegram poll loop survives transient errors

`TelegramCommandHandler._poll_loop` wraps both the per-update handler and the
top-level loop body in `try/except`. Per-update errors log and skip; loop-level
errors back off 10s and continue. Without this, a malformed update or a brief
DB hiccup would kill the daemon thread silently and `/pause`, `/status`,
`/report` would stop responding with no signal to the user.

### 30. HWM is account-level (cross-symbol) — BTC losses pause ETH trading

There is ONE shared `peak_capital` value for the entire account (all symbols). A single
`trading_equity = account_baseline + db.get_closed_pnl_sum()` aggregates PnL across ALL
symbols with no filter. This means:
- If BTC closes a losing trade that crosses 15% drawdown, ETH's `step()` also sees that
  drawdown and returns `[]` (no trades) — even if ETH itself was profitable.
- If ETH recovers the drawdown, the breaker clears for BTC too on next cycle.

This is **intentional** — capital is fungible. The breaker protects the ACCOUNT, not
individual symbol performance. Per-symbol HWM was rejected as unnecessarily complex.

Implementation: `db.get_closed_pnl_sum()` has no symbol filter by default. Both orchestrators
(BTCUSDT and ETHUSDT) call it and get the same cross-symbol total. See `test_cross_symbol_hwm_shared`
in `tests/test_orchestrator_hwm.py`.

### 31. `peak_capital` key semantic changed in May 2026

**Pre-May 2026**: `peak_capital` in `bot_config` stored the peak of the **raw USDT exchange balance**.
This was deposit-prone — a testnet faucet drop of +30k USDT would ratchet the peak to 39277+,
making the circuit breaker permanently stuck (no real trading loss could ever recover from that HWM).

**Post-May 2026**: `peak_capital` stores the peak of **TRADING EQUITY** = `account_baseline + SUM(closed_pnl)`.
The raw balance is no longer the breaker's input. The KEY NAME was kept (`peak_capital`) for API
stability across `get_peak_capital()`, `set_peak_capital()`, and every test that passes a plain
float — callers don't need to change.

**Migration**: at first Phase-2 startup, `_init_account_baseline(db, client)` in `main.py` detects
`account_baseline IS NULL` and runs a one-shot seed:
1. Fetches current USDT balance from Binance
2. Computes `baseline = balance - SUM(pnl_all_closed_trades)`
3. Sets `account_baseline` and resets `peak_capital` to current trading equity

After that, every `orchestrator.step()` re-computes `trading_equity` from DB (no in-memory cache)
and ratchets `peak_capital` if it's higher. Faucet drops have zero effect.

If the back-computed baseline is wrong (e.g. testnet faucet history polluted), use `/reset_hwm`
to clear the peak, then manually DB-poke `account_baseline` to the correct value.

---

## Walk-Forward Optimizer

`bot/optimizer/walk_forward.py` runs a grid search over EMA `stop_atr_mult` × `tp_atr_mult`
to find the best SL/TP combination for the current market conditions.

### Search space

```python
STOP_GRID = [1.0, 1.25, 1.5, 1.75, 2.0]   # SL ATR multipliers
TP_GRID   = [2.5, 3.0, 3.5, 4.0, 4.5, 5.0] # TP ATR multipliers
```

30 combinations total, minus those with R:R < 1.5 (skipped outright).

### Workflow

1. Dashboard OPTIMIZER tab → user selects symbol, timeframe, lookback (days), risk %, fee %.
2. `run_grid_search()` calls `fetch_and_cache()` for primary + bias timeframe klines.
3. For each (stop, tp) combo, runs `BacktestEngine` with `simulate_trailing=True`.
4. Viable results saved to `optimizer_runs` table (status `pending`).
5. Dashboard shows **pending proposal banner** — user clicks Approve or Reject.
6. Approve → `set_runtime_config(ema_stop_mult=..., ema_tp_mult=...)` → bot restart applies it.

### Database methods

| Method | Description |
|---|---|
| `insert_optimizer_run(...)` | Save one grid result with all metrics and status=pending |
| `get_optimizer_runs(limit)` | List recent runs for history table (sorted by timestamp DESC) |
| `get_best_pending_optimizer_run()` | Best pending run by profit_factor (for banner) |
| `set_optimizer_run_status(id, status)` | Update to `approved` or `rejected` |
| `get_runtime_config()` | Read all bot_config keys as a dict |
| `set_runtime_config(**kwargs)` | Write key=value pairs to bot_config |

---

## Telegram Integration

### Architecture

Two classes handle all Telegram interaction:

| Class | File | Role |
|---|---|---|
| `TelegramNotifier` | `bot/telegram_notifier.py` | Outbound — sends notifications to Telegram |
| `TelegramCommandHandler` | `bot/telegram_commands.py` | Inbound — daemon thread, long-polls `getUpdates` (timeout=30s) |

`main()` constructs both, starts the command handler thread, and wires the notifier into
`run_cycle()`, `_execute_order()`, and `position_manager()`. Neither class is imported
by the orchestrator or strategies — they live at the `main.py` layer only.

### Config storage

Telegram config is stored in the `bot_config` key-value table (same store used for active
mode). Keys:

| Key | Type | Description |
|---|---|---|
| `telegram_token` | str | Bot token from BotFather |
| `telegram_chat_id` | str | Target chat ID |
| `telegram_enabled` | `"true"` / `"false"` | Master on/off switch |
| `bot_paused` | `"true"` / `"false"` | Pause flag checked at `run_cycle()` start |
| `ema_stop_mult` | str (float) | EMA SL ATR multiplier; applied at startup from optimizer approval |
| `ema_tp_mult` | str (float) | EMA TP ATR multiplier; applied at startup from optimizer approval |
| `account_baseline` | str (4-dp float) | USDT trading-start balance; back-computed once at first Phase-2 start (`_init_account_baseline`); never auto-mutated. Constant in `trading_equity = baseline + SUM(pnl)`. |
| `peak_capital` | str (4-dp float) | Peak TRADING EQUITY (not raw balance — see gotcha #31). Ratcheted by `orchestrator.step()` each cycle; cleared/overridden by `/reset_hwm`. |
| `breaker_triggered_at_{symbol}` | ISO datetime or `""` | Per-symbol circuit breaker timestamp; cleared by `/reset_hwm` (all symbols atomically). |

Relevant `Database` methods:
- `save_telegram_config(token, chat_id, enabled)` — writes all three config keys
- `get_telegram_config() -> dict` — returns `{token, chat_id, enabled}`
- `has_telegram_config() -> bool` — True when token + chat_id are present
- `get_bot_paused() -> bool` — reads `bot_paused` key
- `set_bot_paused(paused: bool)` — writes `bot_paused` key
- `get_trade(trade_id: int) -> dict | None` — single trade lookup (used for PnL in `trade_closed`)

### Notifications sent

| Method | When |
|---|---|
| `bot_started(dry_run, mode)` | After setup, before first scheduler tick |
| `bot_stopped()` | Before shutdown (SIGTERM/SIGINT handler) |
| `paused()` / `resumed()` | When `/pause` or `/resume` command received |
| `trade_opened(trade, mode)` | After `_execute_order()` writes an OPEN trade to DB |
| `trade_closed(trade, pnl, exit_reason, mode)` | After `_execute_order()` writes a CLOSE trade to DB |
| `circuit_breaker(drawdown, mode)` | On the cycle the breaker first triggers |
| `hwm_reset(old_peak, new_peak, mode)` | In response to `/reset_hwm` command; shows before/after HWM values and confirms breaker timers cleared |
| `status(balance, open_trade, mode, paused)` | In response to `/status` command; includes bot state (Running/Paused) |
| `report(closed_trades, equity_curve, perf_by_strategy, balance, mode, initial_capital)` | In response to `/report` command; sends full performance summary (win rate, PnL, Sharpe, drawdown, profit factor, best strategy) |
| `register_commands()` | Called once on bot startup; registers the 4 commands in the Telegram chat menu via `setMyCommands` |

### Mode tags

All notifications that accept a `mode` parameter include a tag: `🧪 DEMO` for testnet/dry-run
and `🔴 MAINNET` for live trading. Mode is read from `db.get_active_mode()`.

### Supported commands

| Command | Effect |
|---|---|
| `/pause` | Sets `bot_paused=True` in DB; sends `paused()` notification |
| `/resume` | Sets `bot_paused=False` in DB; sends `resumed()` notification |
| `/status` | Sends current balance, bot state (Running/Paused), and open position summary |
| `/report` | Sends full historical performance: win rate, total PnL, profit factor, Sharpe, max drawdown, max loss streak, best strategy |
| `/reset_hwm [value]` | Resets `peak_capital` to current trading equity (or explicit USDT value) and clears ALL `breaker_triggered_at_*` timestamps atomically. Confirms with `hwm_reset()` notification showing old/new peak. Destructive — accessible via the `/help` inline keyboard "Reset HWM" button. |

The command handler reads token and chat_id from DB on every poll cycle — config changes
take effect without restarting the bot.

---

## Conventions

These are non-negotiable. Follow them or the codebase becomes inconsistent.

### Configuration
All tunable parameters go in `*Config` dataclasses, not hardcoded constants.

| Config class | File | Controls |
|---|---|---|
| `RiskConfig` | `bot/risk/manager.py` | drawdown threshold, risk %, cooldown, trail mult, `quantity_precision` (overridden at startup via exchangeInfo), `enable_regime_exit` (default False) |
| `RegimeDetectorConfig` | `bot/regime/detector.py` | ATR/ADX/Hurst periods and thresholds |
| `EMACrossoverConfig` | `bot/strategy/ema_crossover.py` | fast/slow EMA periods, ATR period |
| `MeanReversionConfig` | `bot/strategy/mean_reversion.py` | BB period/std, RSI period/levels, ATR period |
| `BreakoutConfig` | `bot/strategy/breakout.py` | channel period, volume multiplier, ATR period |

### Where things live

- **New enums** → `bot/constants.py`, inherit from `(str, Enum)`
- **New indicators** → `bot/indicators/utils.py`, pure functions, no side effects, return `pd.Series`
- **New metrics/analytics** → `bot/metrics.py`, pure functions operating on `list[dict]` rows
- **Dashboard colors/layout** → `dashboard/themes.py` NothingOS class
- **Strategy exit levels** → `bot/strategy/levels.py` `calculate_levels()`
- **Signal construction** → `bot/strategy/signal_factory.py` (`buy_signal()`, `sell_signal()`, `hold_signal()`)

---

## Adding a New Strategy

Follow these steps exactly. Do not skip any.

### Step 1 — Create the strategy file

Create `bot/strategy/my_strategy.py`:

```python
import logging
from dataclasses import dataclass

import pandas as pd

from bot.indicators import atr as compute_atr
from bot.strategy.base import BaseStrategy, Signal
from bot.strategy.signal_factory import hold_signal, buy_signal, sell_signal
from bot.strategy.levels import calculate_levels

logger = logging.getLogger(__name__)

STOP_ATR_MULT = 1.5  # module-level constants for ATR multiples
TP_ATR_MULT   = 2.0


@dataclass
class MyStrategyConfig:
    some_period: int = 20
    atr_period: int = 14


class MyStrategy(BaseStrategy):
    def __init__(self, config: MyStrategyConfig = MyStrategyConfig()) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return "MY_STRATEGY"  # must match StrategyName enum value

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        required = self.config.some_period + self.config.atr_period + 2
        if len(df) < required:
            logger.warning("MyStrategy: insufficient data (%d rows)", len(df))
            return hold_signal(atr=0.0)

        atr = compute_atr(df, self.config.atr_period)
        current_atr = atr.iloc[-1]
        current_price = float(df["close"].iloc[-1])

        # ... your signal logic ...

        if buy_condition:
            sl, tp = calculate_levels("BUY", current_price, current_atr, STOP_ATR_MULT, TP_ATR_MULT)
            return buy_signal(strength=0.7, stop_loss=sl, take_profit=tp, atr=current_atr)

        return hold_signal(atr=current_atr)
```

### Step 2 — Add the enum value

In `bot/constants.py`:

```python
class StrategyName(str, Enum):
    EMA_CROSSOVER  = "EMA_CROSSOVER"
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT       = "BREAKOUT"
    MY_STRATEGY    = "MY_STRATEGY"  # add here
```

### Step 3 — Register in the orchestrator

In `bot/orchestrator.py`:

```python
from bot.strategy.my_strategy import MyStrategy

# Add to _strategies dict in __init__:
self._strategies: dict[StrategyName, BaseStrategy] = {
    StrategyName.EMA_CROSSOVER:  EMACrossoverStrategy(),
    StrategyName.MEAN_REVERSION: MeanReversionStrategy(),
    StrategyName.BREAKOUT:       BreakoutStrategy(),
    StrategyName.MY_STRATEGY:    MyStrategy(),   # add here
}

# Map to a regime (or reuse an existing entry):
REGIME_STRATEGY_MAP: dict[MarketRegime, StrategyName] = {
    MarketRegime.TRENDING:  StrategyName.EMA_CROSSOVER,
    MarketRegime.RANGING:   StrategyName.MEAN_REVERSION,
    MarketRegime.VOLATILE:  StrategyName.MY_STRATEGY,   # example
}
```

### Step 4 — Write tests

Tests live in `tests/`. One test file per strategy: `tests/test_my_strategy.py`. Use synthetic
OHLCV `pd.DataFrame` and cover BUY / SELL / HOLD / insufficient-data branches.

The dashboard reads strategy names from the DB as strings — no changes needed as long as
`name` matches the `StrategyName` enum value exactly.

---

## Environment Variables Reference

| Variable | Default | Valid Range | Description |
|---|---|---|---|
| `BINANCE_API_KEY` | — | required in live mode | Binance Testnet HMAC API key |
| `BINANCE_API_SECRET` | — | required in live mode | Binance Testnet API secret |
| `BINANCE_TESTNET` | `true` | `true` / `false` | Route to testnet endpoint |
| `SYMBOL` | `BTCUSDT` | any valid Binance pair | Trading pair |
| `TIMEFRAME` | `4h` | Binance kline intervals | Candle interval |
| `INITIAL_CAPITAL` | `10000` | > 0 | Fallback balance when Binance API is unreachable |
| `RISK_PER_TRADE` | `0.015` | (0, 0.10] per `settings.validate()` | Fraction of capital risked per trade. Production normally reads from DB seed (`_seed_optimized_defaults`); this env value is the dataclass fallback used by tests/scripts that bypass `main()`. |
| `DB_PATH` | `trading_bot.db` | any writable path | SQLite database file location |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR | Python logging level |
| `TZ` | `UTC` | any IANA timezone | Timezone for log timestamps (e.g. `Europe/Madrid`) |
| `DECIMAL_SEPARATOR` | `dot` | `dot` / `comma` | Dashboard number format — `dot`: 1,234.56 · `comma`: 1.234,56 |

`RISK_PER_TRADE` validation: `settings.validate()` raises `ValueError` if outside (0, 0.10].
Anything above 4% pushes max drawdown above 37% on the validated 3-year matrix — see
`scripts/risk_scaler_matrix.py`. Don't crank it without re-running the matrix.
Validation is skipped in `--dry-run` mode — the bot starts even with missing API keys.

---

## Running Locally vs Docker

### Local (venv, dry-run)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # edit with your keys
python main.py --dry-run          # no orders placed, DB still written
```

Dashboard (separate terminal):
```bash
streamlit run dashboard/app.py
# open http://localhost:8501
```

Log file written to `logs/bot.log`.

### Docker (production / full stack)

```bash
cp .env.example .env
# edit .env with BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET=true

docker compose up -d
# bot container:       python main.py
# dashboard container: streamlit run dashboard/app.py --server.port=8501
# open http://localhost:8501
```

Docker mounts two named volumes:
- `data` → `/app/data` (SQLite DB)
- `logs` → `/app/logs` (bot.log)

Both containers share the same image (`ghcr.io/jorditomasg/trading-bot:latest`).

Log rotation: bot container caps at 10 MB × 5 files, dashboard at 5 MB × 3 files.

### Graceful shutdown

The bot handles `SIGTERM` and `SIGINT`. In Docker: `docker compose stop` sends SIGTERM.
The main loop checks `_shutdown` flag between scheduler ticks (10s polling interval).
