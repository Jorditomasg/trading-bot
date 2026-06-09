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
| `symbol` | `BTCUSDT` | Multi-symbol via `PortfolioBacktestEngine`. Live set seeded **`BTCUSDT,ETHUSDT,SOLUSDT`** (SOL added 2026-06-09 for diversification — see below) |
| `timeframe` | `4h` | 1h is unviable (legacy backtests: PF=0.75, Ann=-26%) |
| `risk_per_trade` | `0.015` (1.5%) | Picked over 4% per `scripts/risk_scaler_matrix.py` (May 2026) |
| `ema_stop_mult` | `1.5` | SL = 1.5 × ATR |
| `ema_tp_mult` | `5.0` | TP = 5.0 × ATR (B-pick: walk-forward audit winner, May 2026) |
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
- **SOLUSDT added to the live set (2026-06-09)** for diversification. Under the
  TRUE live ÷N capital allocation, BTC+ETH+SOL cuts 3y max-DD 12.1%→9.4% and
  lifts Calmar 1.61→1.99 vs BTC+ETH, **robust in both sub-period halves** (and in
  the weak 2025-26 chop it improves CAGR *and* DD). SOL is a decorrelation play
  (4h corr to BTC 0.73), not return-chasing — its standalone Calmar (0.80) is
  worse than BTC's. BNB was rejected (toxic, standalone Calmar −0.04). Spec:
  `docs/superpowers/specs/2026-06-09-add-sol-diversification.md`.

> ✅ **Gotcha #40 FIXED (2026-06-10):** `PortfolioBacktestEngine` now sizes each
> symbol off `capital/N`, mirroring live's `total/N` allocation — dashboard
> multi-symbol numbers are **true-live scale** (validated: BTC+ETH 19.3% CAGR /
> 12.1% DD / Calmar 1.60; BTC+ETH+SOL DD 9.4% / Calmar 1.97). Any multi-symbol
> result recorded BEFORE 2026-06-10 — including the Risk×DD table below — is
> still ~N× inflated; re-run before relying on its absolute values.

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
      │  validate_signal()   │  rejects if strength < 0.5 or circuit breaker active
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
| `bot/audit/` | Walk-forward validation framework (sub-project A). Reuses `PortfolioBacktestEngine`. CLI in `scripts/audit/run_walk_forward.py`. Spec: `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md`. Reports under `docs/audits/`. See gotcha #32. |
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
| `bot/backtest/portfolio_runner.py` | Pure runner used by the dashboard. Owns `BIAS_WARMUP` / `MOMENTUM_WARMUP` constants, `build_fetch_plan`, `build_backtest_config`, `run_portfolio_backtest_core`. No Streamlit deps — testable end-to-end. The Streamlit section delegates to it. |
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
| `dashboard/sections/backtest_runner.py` | Thin Streamlit wrapper around `bot/backtest/portfolio_runner.py`. Handles spinner / error / warning / session_state / rerun — no real logic. |
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

## Gotchas — moved to `docs/gotchas.md`

The 37-entry gotchas catalog that used to live here was extracted to keep
this file under ~500 lines (loaded into every Claude session). Entries are
numbered and stable — `gotcha #N` references in source comments remain
valid.

**Read first when**:
- A bug touches anything cross-cutting (DB persistence, exit logic, parity,
  HWM, circuit breaker) — search `docs/gotchas.md` by `### N.`
- Live and backtest produce different numbers — read gotchas **#24, #36, #37**
  before debugging
- Adding a runtime config key — read gotchas **#36, #37** + run
  `tests/test_parity_runtime.py` to verify both paths agree

**Companion docs**:
- `docs/backtest_vs_live.md` — every known live↔backtest fidelity gap, with rationale
- `docs/INDEX.md` — topic → file lookup map for fast navigation

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
- **Shared test helpers** → `tests/conftest.py` (`make_ohlcv`, `uptrend`, `flat`, `choppy`, `tmp_db`, `seeded_db`)
- **Dashboard backtest logic** → `bot/backtest/portfolio_runner.py` (pure); `dashboard/sections/backtest_runner.py` is a thin Streamlit wrapper

---

## Adding a New Strategy — moved to `docs/adding_strategies.md`

The step-by-step strategy guide was moved out. See `docs/adding_strategies.md`
for the four-step walkthrough (create file, add enum, register in orchestrator,
write tests).

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
