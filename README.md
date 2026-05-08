# Trading Bot — BTC/USDT

Regime-adaptive algorithmic trading bot for Binance (Testnet and Mainnet). Automatically selects between three strategies based on real-time market regime detection.

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?style=flat-square)
![Binance](https://img.shields.io/badge/Binance-Testnet%20%7C%20Mainnet-F0B90B?style=flat-square)

---

## Overview

The bot runs on a configurable candle interval (recommended: **4h** — the validated baseline).
Each cycle it:

1. Fetches the last 200 OHLCV candles (primary TF) and the corresponding higher-TF candles from Binance
2. Classifies the market as TRENDING, RANGING, or VOLATILE using a 3-level detection cascade
3. Selects the best-fit strategy for the current regime (with live win-rate fallback logic)
4. Generates a signal and filters it through the multi-timeframe BiasFilter (higher-TF EMA9/21 alignment)
5. Validates the signal through the risk manager
6. Opens or closes a position on Binance, writes results to SQLite
7. Records an equity snapshot for the dashboard

**Key features:**

- 3-level regime detection: ATR volatility spike → ADX → Hurst exponent (R/S analysis)
- 3 strategies, each tuned to a different market condition; calibrated per timeframe via presets (1h, 4h, 15m)
- **Multi-timeframe BiasFilter**: EMA9/21 on the higher-TF candles gates primary-TF signals — only trades in the direction of the higher-timeframe trend; fail-closed (network errors block signals, not bypass them)
- Dynamic position sizing: risk a fixed % of capital per trade, with a spot capital cap that prevents notional from ever exceeding 99% of available capital
- Multi-symbol equitable allocation: when running >1 symbol, each cycle sees `total_balance / N_symbols` as its working capital, so no single symbol can drain the pool
- Circuit breaker: halts trading on >15% drawdown; auto-resets after 4 hours or recovery
- Win-rate fallback: switches away from underperforming strategies automatically
- Nothing OS dashboard — 4-tab Streamlit UI: MONITOR · CONFIG · BACKTEST · OPTIMIZER
- **Walk-forward optimizer**: grid search over EMA SL/TP ATR multipliers on real historical data; approving a result updates the live config
- **Backtest runner**: historical simulation with Parquet-cached klines; supports fee modelling and weekly momentum filter
- Full dry-run mode: no exchange calls, but equity curve is still recorded
- DEMO / MAINNET mode switch from the dashboard settings panel
- Telegram notifications: trade open/close, circuit breaker trigger, bot start/stop — tagged with `🧪 DEMO` or `🔴 MAINNET`
- Telegram commands: `/pause`, `/resume`, `/status`, `/report` — control and monitor the bot from any Telegram chat

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    main.py — hourly scheduler                    │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                  ┌────────────▼────────────┐
                  │     BinanceClient        │
                  │  200 OHLCV candles (TF)  │
                  │  higher-TF bias candles  │
                  │  USDT balance            │──► total_balance
                  └────────────┬────────────┘
                               │ df + df_high
                  ┌────────────▼────────────┐
                  │  Capital allocation      │  main.py
                  │  balance = total / N     │  N = active symbols
                  └────────────┬────────────┘
                               │ allocated balance
                  ┌────────────▼────────────┐
                  │   StrategyOrchestrator   │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  RegimeDetector    │  │
                  │  │  (on primary TF)   │  │
                  │  │  L1: ATR spike?    │  │──► VOLATILE  → Breakout
                  │  │  L2: ADX >= 25?    │  │──► TRENDING  → EMA Crossover
                  │  │  L3: Hurst H?      │  │──► RANGING   → Mean Reversion
                  │  └────────────────────┘  │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  Strategy          │  │
                  │  │  .generate_signal()│  │──► Signal(action, strength, SL, TP, ATR)
                  │  └────────────────────┘  │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  BiasFilter        │  │
                  │  │  (on df_high)      │  │
                  │  │  EMA9 > EMA21?     │  │──► BULLISH → only BUY passes
                  │  │  EMA9 < EMA21?     │  │──► BEARISH → only SELL passes
                  │  │  gap < 0.1%?       │  │──► NEUTRAL → no signal (fail-closed)
                  │  └────────────────────┘  │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  RiskManager       │  │
                  │  │  validate_signal() │  │──► reject if strength < 0.4 or CB active
                  │  │  position_size()   │  │──► qty = min(risk-based, capital cap)
                  │  └────────────────────┘  │
                  └────────────┬────────────┘
                               │ order dict
                  ┌────────────▼────────────┐
                  │     BinanceClient        │
                  │   place_order() (live)   │  (skipped in --dry-run)
                  └────────────┬────────────┘
                               │
                  ┌────────────▼────────────┐
                  │       SQLite DB          │
                  │  trades / equity /       │◄── equity snapshot every cycle
                  │  signals (+ bias col)    │
                  └────────────┬────────────┘
                               │
                  ┌────────────▼────────────┐
                  │   Streamlit Dashboard    │
                  │   auto-refresh 60s       │
                  └─────────────────────────┘
```

---

## Strategies

| Strategy | Regime | Entry Condition | SL | TP | Key Parameters |
|---|---|---|---|---|---|
| **EMA Crossover** | TRENDING | EMA9 crosses EMA21 (or trend-continuation pullback within 1.0 ATR of EMA9) | 1.5× ATR | 4.5× ATR¹ | fast=9, slow=21 |
| **Mean Reversion** | RANGING | Price at Bollinger Band + RSI confirmation | 1.5× ATR | BB midline (SMA20) | BB(20, 2σ), RSI(14) oversold<35 / overbought>65 (1h) |
| **Breakout** | VOLATILE | Close breaks Donchian channel with volume > 1.5–2.0× average | 2.0× ATR | 3.0× ATR | channel=20–30, vol_mult=1.5–2.0 (TF-dependent) |

¹ EMA Crossover SL/TP multipliers are runtime-configurable via the Optimizer dashboard and stored in the DB; they take effect on the next bot restart.

Signal strength is a 0.0–1.0 score. Signals with strength < 0.4 are rejected by the risk manager.
Opposite signals with strength >= 0.5 close an open position (signal reversal exit).

---

## Quick Start (Docker)

**Step 1** — Get Binance Testnet API keys:
1. Go to [testnet.binance.vision](https://testnet.binance.vision)
2. Sign in with GitHub
3. Generate HMAC keys

**Step 2** — Configure and start:

```bash
git clone https://github.com/Jorditomasg/trading-bot.git
cd trading-bot
cp .env.example .env
# edit .env — add BINANCE_API_KEY and BINANCE_API_SECRET
docker compose up -d
```

**Step 3** — Open the dashboard:

```
http://localhost:8501
```

That's it. The bot starts immediately and runs every hour on the hour.

---

## Development Setup

```bash
# Create and activate virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy and configure env
cp .env.example .env
# In dry-run mode, API keys are not validated — you can use placeholder values

# Start the bot (no real orders placed, equity curve is still recorded)
python main.py --dry-run

# Start the dashboard (separate terminal)
streamlit run dashboard/app.py
```

Logs are written to `logs/bot.log` and stdout simultaneously.

---

## Configuration

All configuration is read from environment variables (`.env` file or Docker env).

| Variable | Default | Valid Range | Description |
|---|---|---|---|
| `BINANCE_API_KEY` | — | required (live) | Binance HMAC API key |
| `BINANCE_API_SECRET` | — | required (live) | Binance API secret |
| `BINANCE_TESTNET` | `true` | `true` / `false` | Route requests to testnet endpoint |
| `SYMBOL` | `BTCUSDT` | any Binance pair | Trading pair |
| `TIMEFRAME` | `4h` | Binance intervals | Candle interval for strategy. **`4h` is the validated baseline** — `1h` is unviable in backtest (PF=0.75). |
| `INITIAL_CAPITAL` | `10000` | > 0 | Fallback balance when Binance API is unreachable |
| `RISK_PER_TRADE` | `0.015` | (0, 0.10] | Fraction of capital risked per trade. Production reads from DB seed; this is the dataclass fallback. |
| `DB_PATH` | `trading_bot.db` | writable path | SQLite database file location |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | Python log level |
| `TZ` | `UTC` | any IANA timezone | Timezone for log timestamps (e.g. `Europe/Madrid`) |
| `DECIMAL_SEPARATOR` | `dot` | `dot` / `comma` | Dashboard number format — `dot`: 1,234.56 · `comma`: 1.234,56 |

`RISK_PER_TRADE` must be between 0 and 0.10 (10%). 4% pushes max drawdown to ~37% on
the 3-year BTC+ETH portfolio matrix; 1.5% (the seeded production value) gives ~15% DD.
See `scripts/risk_scaler_matrix.py` for the full risk × scaler comparison. The bot exits
on startup if validation fails (except in `--dry-run` mode, where validation is skipped).

**Risk manager defaults** (configurable via `RiskConfig` in code):

| Parameter | Default | Description |
|---|---|---|
| `max_drawdown` | 0.15 (15%) | Circuit breaker threshold |
| `max_concurrent_trades` | 1 | Maximum open positions per symbol (each symbol has its own orchestrator) |
| `min_signal_strength` | 0.4 | Minimum signal strength to trade |
| `cooldown_hours` | 4 | Circuit breaker cooldown duration |
| `quantity_precision` | 5 | Decimal places for order quantity; overridden at startup from Binance `LOT_SIZE` filter |

**Trailing stop has been removed from the production code.** Backtest evidence showed it destroyed performance (PF 1.55 → 0.76 with trailing on; only 1 of 131 trades hit TP). The `trailing_stop_enabled` config flag, the ratcheting logic in `position_manager`, and the `TRAILING_STOP` exit reason were all removed. Positions exit only via SL or TP. The `trades.trailing_sl` DB column is preserved for legacy rows but is never written by the live bot. See gotcha #1 in `CLAUDE.md`.

Position sizing applies a **spot capital cap**: when the risk-based formula would request a notional larger than the available capital, `quantity` is reduced to `(capital × 0.99) / entry`. A WARNING log line fires when the cap activates so you can detect when your `risk_per_trade × stop_atr_mult` combination is too aggressive for spot.

---

## Dashboard Guide

The dashboard has 4 tabs: **MONITOR**, **CONFIG**, **BACKTEST**, **OPTIMIZER**.

All times are UTC.

### MONITOR tab

**Topbar** — Bot name, running status pill, mode badge (`● DEMO` or `● MAINNET` — reads actual active mode from DB), current regime badge, clock (updates every 5s). Settings (⚙) button at the top right opens the configuration popover.

**KPI Row** — Six cards: Balance, Total PnL ($ and %), Win Rate, Annualised Sharpe Ratio, Max Drawdown, Total Closed Trades.

**Equity Curve** (60% width) — Balance over time. Line is white above starting capital, red below. Dotted reference line marks initial capital.

**Drawdown Panel** (40% width, beside equity curve) — Drawdown chart (inverted Y axis) with 15% circuit breaker reference line.

**State Panel** — Current regime + regime timeline strip + open position card (entry, SL, TP, quantity) or "NO OPEN POSITION".

**Strategy Performance** — Horizontal bar chart, win rate per strategy. Bars are white above 50%, red below. Each bar shows trade count.

**P&L Distribution** — Histogram of closed trade P&L. Wins (white) and losses (red) overlaid.

**Risk Metrics** — Profit Factor, Max Loss Streak, Average Win $, Average Loss $. Below: win rate per regime.

**Trade History** — Last 50 closed trades. PnL in white (positive) or red (negative). Exit reason column shows STOP_LOSS, TAKE_PROFIT, TRAILING_STOP, or SIGNAL_REVERSAL.

**Signal Log** — Last 20 signals generated. Shows timestamp, strategy, regime, action (BUY/SELL/HOLD), and strength score.

**Settings (⚙ popover)** — Always accessible from the top right. Contains DEMO/MAINNET mode switch and the Telegram configuration section (token, chat ID, enable toggle, Save and Test buttons). Changes take effect immediately without restarting the bot.

### CONFIG tab

Manage runtime bot settings from the dashboard: symbol, timeframe, risk per trade, and EMA strategy SL/TP multipliers. Changes are saved to the `bot_config` DB table; they take effect on the next bot restart.

### BACKTEST tab

Run a historical simulation directly from the dashboard. Uses cached Parquet klines from `data/klines/` (downloaded automatically on first run). Results include equity curve, trade log, and summary metrics (PF, Sharpe, win rate, max drawdown).

### OPTIMIZER tab

Runs a grid search over EMA `stop_atr_mult` × `tp_atr_mult` on recent historical data.

- Select symbol, timeframe, lookback period, risk %, and fee per side
- Results are scored by Profit Factor and filtered by viability constraints (≥15 trades, DD ≤20%, Sharpe ≥0.4, PF ≥1.05)
- Viable configs are saved as `pending` proposals in the DB
- A banner appears at the top of the OPTIMIZER tab for the best pending proposal — click **Approve & Apply** to write the parameters to the bot config, or **Reject** to discard
- Approved parameters take effect after restarting the bot

---

## Telegram Setup

Telegram config is managed from the dashboard — no environment variables needed.

1. Create a bot via [@BotFather](https://t.me/BotFather) → copy the token
2. Start a chat with your bot (or add it to a group) → get the chat ID via [@userinfobot](https://t.me/userinfobot)
3. Open the dashboard → **⚙** (top right) → Telegram section
4. Enter the token and chat ID, enable notifications, press **Save**
5. Press **Test** to verify connectivity — you should receive a test message

Configuration is stored in the SQLite `bot_config` table and read on every notification send, so changes take effect immediately without restarting the bot.

**Commands** (send to your bot in Telegram):

| Command | Effect |
|---|---|
| `/pause` | Skip strategy cycles — SL/TP monitoring and Telegram polling continue |
| `/resume` | Resume normal operation |
| `/status` | Current balance, bot state (Running/Paused), and open position summary |
| `/report` | Full performance summary: win rate, PnL, profit factor, Sharpe, max drawdown, max loss streak, best strategy |

Commands are registered in the Telegram chat menu automatically on bot startup (`setMyCommands`).

All notifications include a mode tag (`🧪 DEMO` or `🔴 MAINNET`) so you always know which environment an alert is coming from.

---

## CI/CD

The project is designed for automated deployment via GitHub Actions and GitHub Container Registry.

**Flow:**

1. Push to `main` branch
2. GitHub Actions workflow builds the Docker image
3. Image is pushed to `ghcr.io/jorditomasg/trading-bot:latest`
4. Server pulls the new image and restarts via `docker compose pull && docker compose up -d`

Both the `bot` and `dashboard` services use the same image. No separate Dockerfiles needed.

---

## Troubleshooting

**Bot starts but no trades are opening**

Two common causes:

1. **Circuit breaker active** — drawdown > 15%. Check logs for `CIRCUIT BREAKER triggered`. Resets after 4 hours or on drawdown recovery.
2. **BiasFilter blocking signals** — if the higher-TF EMA9/21 are too close (< 0.1% gap) or the higher-TF fetch is failing, the filter returns `NEUTRAL` and blocks all directional signals. Check logs for `BiasFilter blocked signal` or kline fetch errors. Also check that signal strength is reaching 0.4+ in the dashboard signal log.

**Dashboard shows "waiting for data..." on charts**

The equity table needs at least 2 rows. The bot records a snapshot every cycle. Run at least
one cycle (`python main.py --dry-run`) and refresh the dashboard.

**`Configuration error: BINANCE_API_KEY is not set`**

You are running without `--dry-run` and `BINANCE_API_KEY` is missing or set to the placeholder
value from `.env.example`. Either add real testnet keys or add `--dry-run` to the command.

**`Not enough data (N rows, need M) — defaulting to RANGING`**

RegimeDetector needs at least 64 rows (max of ATR lookback + volatile lookback, 2× ADX period,
Hurst lookback). With `KLINES_LIMIT=200` this should never happen in production. It can appear
on first startup with a fresh DB if the exchange returns fewer candles.

**Trailing SL column is always NULL in the DB**

This is expected: trailing stop has been **removed from the production code**. Backtest
evidence showed it destroyed performance (PF 1.55 → 0.76, only 1 of 131 trades hit TP with
trail on). The `trades.trailing_sl` column is preserved for legacy rows but is no longer
written. See gotcha #1 in `CLAUDE.md` for full context. Positions exit only via SL or TP.

**Docker container exits immediately**

Check logs with `docker compose logs bot`. Common causes: invalid API keys (in live mode),
`RISK_PER_TRADE` outside 0–0.05 range, or DB_PATH not writable. The `data` volume must be
writable by the container process.

---

## Risk Disclaimer

This software is intended for educational purposes and paper trading. It supports both Binance
Testnet (simulated funds) and Mainnet (real funds) via a mode switch in the dashboard. Using
Mainnet mode involves real financial risk. Do not enable Mainnet trading without fully
understanding the strategies, risk parameters, and potential for loss. Past performance in
simulation does not guarantee future results. The authors accept no liability for financial
losses of any kind.
