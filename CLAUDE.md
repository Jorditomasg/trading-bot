# CLAUDE.md — Trading Bot

Developer reference for this codebase. Read this before touching anything.

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
              │  get_balance("USDT")     │
              └────────────┬────────────┘
                           │  pd.DataFrame (OHLCV)
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
      │  compute_position_   │  risk_amount / (entry - stop_loss)
      │  size()              │
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
| `main.py` | Entry point, CLI flags, scheduler, run_cycle loop |
| `bot/config.py` | Settings dataclass, reads `.env` via python-dotenv |
| `bot/constants.py` | All enums: ExitReason, TradeAction, OrderSide, StrategyName |
| `bot/orchestrator.py` | Coordinates regime → strategy → bias filter → risk → order dict |
| `bot/bias/filter.py` | `BiasFilter` — EMA9/21 on 4h candles; returns BULLISH/BEARISH/NEUTRAL; injected into orchestrator as hard gate before signal execution |
| `bot/regime/detector.py` | 3-level regime detection: ATR volatility → ADX → Hurst |
| `bot/risk/manager.py` | Circuit breaker, position sizing, signal validation |
| `bot/strategy/base.py` | Abstract BaseStrategy + Signal dataclass |
| `bot/strategy/ema_crossover.py` | EMA 9/21 crossover strategy (TRENDING) |
| `bot/strategy/mean_reversion.py` | Bollinger Bands + RSI strategy (RANGING) |
| `bot/strategy/breakout.py` | Donchian channel + volume filter (VOLATILE) |
| `bot/strategy/levels.py` | Pure function: `calculate_levels(side, price, atr, sl_mult, tp_mult)` |
| `bot/strategy/signal_factory.py` | Constructors: `buy_signal()`, `sell_signal()`, `hold_signal()` |
| `bot/indicators/utils.py` | Pure functions: `atr()`, `rsi()`, `wilder_smooth()` |
| `bot/database/db.py` | SQLite wrapper, DDL, migrations, all queries; `bot_config` KV store for Telegram config and pause state |
| `bot/metrics.py` | Pure functions: Sharpe, max drawdown, profit factor, max loss streak |
| `bot/exchange/binance_client.py` | Binance API client (testnet-aware) |
| `bot/telegram_notifier.py` | `TelegramNotifier` — sends trade/circuit-breaker/lifecycle events; `register_commands()` registers bot menu via `setMyCommands`; lazy DB config reads |
| `bot/telegram_commands.py` | `TelegramCommandHandler` — daemon thread, long-polls Telegram, handles `/pause` `/resume` `/status` `/report` |
| `dashboard/app.py` | Streamlit app; `_topbar()` fragment (5s refresh) with live mode pill and settings popover; section order: KPIs → Live → [Equity | Drawdown + State] → Signals → Performance |
| `dashboard/sections/open_position.py` | Regime badge + CSS flex timeline strip + open position; `drawdown_section` as separate `@st.fragment(run_every=10)` |
| `dashboard/themes.py` | NothingOS palette + PLOTLY_LAYOUT definition |

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

---

## Strategy → Regime Mapping

Default mapping (in `bot/orchestrator.py`):

```python
REGIME_STRATEGY_MAP = {
    MarketRegime.TRENDING:  StrategyName.EMA_CROSSOVER,
    MarketRegime.RANGING:   StrategyName.MEAN_REVERSION,
    MarketRegime.VOLATILE:  StrategyName.BREAKOUT,
}
```

### Fallback (win-rate override)

`_select_strategy()` checks live performance before returning the default:

1. Query `get_performance_by_strategy()` for all strategies with >= 20 closed trades.
2. If the mapped strategy's win rate is below **40%**, search all tracked strategies for the highest win rate.
3. If a better candidate exists, swap to it and log the switch.
4. If fewer than 20 trades exist for any strategy, that strategy is ignored in the comparison — not enough data.

The fallback can switch across regime boundaries (e.g., use EMA_CROSSOVER in a RANGING regime if mean reversion has been underperforming). This is intentional.

---

## Strategy Details

### EMA Crossover (TRENDING)
- Signal: EMA9/EMA21 crossover (single-bar) OR trend-continuation entry when price is within `max_distance_atr` (default 1.5) of EMA9
- Crossover strength: `abs(fast_slope) / ATR × 5`, floor 0.6
- Trend strength: `0.5 × (1 - dist_atr / max_distance_atr) + 0.4`, capped 0.4–0.8
- Distance check uses `abs()` — filters overextension in both directions (above AND below EMA9)
- SL: `1.5 × ATR` below/above entry
- TP: `2.5 × ATR` above/below entry

### Mean Reversion (RANGING)
- Signal: price touches Bollinger Band (20, 2σ) AND RSI confirms oversold/overbought
- BUY: price <= lower band AND RSI < 30
- SELL: price >= upper band AND RSI > 70
- Strength: combination of band penetration depth + RSI extremity
- SL: `1.5 × ATR` from entry
- TP: Bollinger midline (SMA20)

### Breakout (VOLATILE)
- Signal: close breaks Donchian channel (20-period) with volume > 1.5× average
- Channel uses `.shift(1)` to avoid look-ahead bias
- Strength: `(vol_ratio - 1.5) / 2 + 0.5`, capped at 1.0
- SL: `2.0 × ATR` from entry
- TP: `3.0 × ATR` from entry

---

## Key Gotchas

These WILL bite you if you don't know them.

### 1. Trailing SL is NULL until activation — ratcheting happens in `position_manager`

`trades.trailing_sl` is NULL in the DB until price moves `trail_activation_mult × ATR`
(default: 1.0 × ATR) away from entry. The column exists from trade open but holds NULL.
Do not assume a non-NULL trailing_sl on any open trade.

The **ratcheting** (moving the stop up/down as price moves) runs in `position_manager()`
every 60 seconds using the live WebSocket price — NOT in `orchestrator._evaluate_open_position()`
which only runs hourly. `_evaluate_open_position()` handles signal-reversal exits only.

```python
# in main.py position_manager() — runs every 60s
if side == "BUY" and price >= entry_price + activation:
    new_trail = price - trail_dist
    if trailing_sl is None or new_trail > trailing_sl:
        db.update_trailing_sl(trade_id, new_trail)
```

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

### 4. Circuit breaker resets two ways

The circuit breaker is NOT permanent. It resets if EITHER condition is met:
- `cooldown_hours` (default 4h) have elapsed since trigger, OR
- drawdown recovers below `max_drawdown` threshold (15% default)

```python
# If drawdown recovers before cooldown expires:
if drawdown < self.config.max_drawdown:
    self._breaker_triggered_at = None  # immediate reset
    return False
```

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

### 9. Opposite signal closes position only if `strength >= 0.5`

```python
opposite = (
    (side == "BUY"  and signal.action == "SELL") or
    (side == "SELL" and signal.action == "BUY")
) and signal.strength >= 0.5
```
A weak opposite signal (strength < 0.5) is ignored. The position stays open.
This threshold is hardcoded in `_evaluate_open_position()`, not in `RiskConfig`.

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

The command handler reads token and chat_id from DB on every poll cycle — config changes
take effect without restarting the bot.

### No new dependencies

`TelegramNotifier` uses `requests`, which is already in `requirements.txt`. No new packages
are needed.

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

Tests live in `tests/`. Follow the existing pattern:
- One test file per strategy: `tests/test_my_strategy.py`
- Use `pd.DataFrame` with synthetic OHLCV data
- Test: BUY signal, SELL signal, HOLD when no condition met, insufficient data fallback

### Step 5 — Verify dashboard compatibility

The dashboard reads strategy names directly from the DB as strings. No changes needed as long
as the `name` property matches the `StrategyName` enum value exactly.

---

## Environment Variables Reference

| Variable | Default | Valid Range | Description |
|---|---|---|---|
| `BINANCE_API_KEY` | — | required in live mode | Binance Testnet HMAC API key |
| `BINANCE_API_SECRET` | — | required in live mode | Binance Testnet API secret |
| `BINANCE_TESTNET` | `true` | `true` / `false` | Route to testnet endpoint |
| `SYMBOL` | `BTCUSDT` | any valid Binance pair | Trading pair |
| `TIMEFRAME` | `1h` | Binance kline intervals | Candle interval |
| `INITIAL_CAPITAL` | `10000` | > 0 | Fallback balance when Binance API is unreachable |
| `RISK_PER_TRADE` | `0.01` | 0.001 – 0.05 | Fraction of capital risked per trade (validated on startup) |
| `DB_PATH` | `trading_bot.db` | any writable path | SQLite database file location |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR | Python logging level |
| `TZ` | `UTC` | any IANA timezone | Timezone for log timestamps (e.g. `Europe/Madrid`) |
| `DECIMAL_SEPARATOR` | `dot` | `dot` / `comma` | Dashboard number format — `dot`: 1,234.56 · `comma`: 1.234,56 |

`RISK_PER_TRADE` validation: `settings.validate()` raises `ValueError` if outside (0, 0.05].
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

Both containers share the same image (`ghcr.io/${GITHUB_REPO}:latest`). The `GITHUB_REPO`
env var must be set to your `user/repo` slug, or override the image name directly.

Log rotation: bot container caps at 10 MB × 5 files, dashboard at 5 MB × 3 files.

### Graceful shutdown

The bot handles `SIGTERM` and `SIGINT`. In Docker: `docker compose stop` sends SIGTERM.
The main loop checks `_shutdown` flag between scheduler ticks (10s polling interval).
