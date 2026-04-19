# * Trading Bot — BTC/USDT

Regime-adaptive algorithmic trading bot for Binance (Testnet and Mainnet). Automatically selects between three strategies based on real-time market regime detection.

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?style=flat-square)
![Binance](https://img.shields.io/badge/Binance-Testnet%20%7C%20Mainnet-F0B90B?style=flat-square)

---

## Overview

The bot runs on a 1-hour candle cycle. Each cycle it:

1. Fetches the last 200 OHLCV candles (1h) and 150 candles (4h) from Binance
2. Classifies the market as TRENDING, RANGING, or VOLATILE using a 3-level detection cascade
3. Selects the best-fit strategy for the current regime (with live win-rate fallback logic)
4. Generates a signal and filters it through the multi-timeframe BiasFilter (4h EMA9/21 alignment)
5. Validates the signal through the risk manager
6. Opens or closes a position on Binance, writes results to SQLite
7. Records an equity snapshot for the dashboard

**Key features:**

- 3-level regime detection: ATR volatility spike → ADX → Hurst exponent (R/S analysis)
- 3 strategies, each tuned to a different market condition
- **Multi-timeframe BiasFilter**: EMA9/21 on 4h candles gates 1h signals — only trades in the direction of the higher-timeframe trend; fail-closed (network errors block signals, not bypass them)
- Dynamic position sizing: risk a fixed % of capital per trade (default 1%)
- Trailing stop-loss that activates after a configurable ATR distance
- Circuit breaker: halts trading on >15% drawdown; auto-resets after 4 hours or recovery
- Win-rate fallback: switches away from underperforming strategies automatically
- Nothing OS dashboard — real-time Streamlit UI with equity curve, drawdown, P&L, signal log
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
                  │  200 OHLCV candles (1h)  │
                  │  150 OHLCV candles (4h)  │
                  └────────────┬────────────┘
                               │ df_1h + df_4h
                  ┌────────────▼────────────┐
                  │   StrategyOrchestrator   │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  RegimeDetector    │  │
                  │  │  (on df_1h)        │  │
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
                  │  │  (on df_4h)        │  │
                  │  │  EMA9 > EMA21?     │  │──► BULLISH → only BUY passes
                  │  │  EMA9 < EMA21?     │  │──► BEARISH → only SELL passes
                  │  │  gap < 0.1%?       │  │──► NEUTRAL → no signal (fail-closed)
                  │  └────────────────────┘  │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  RiskManager       │  │
                  │  │  validate_signal() │  │──► reject if strength < 0.4 or CB active
                  │  │  position_size()   │  │──► qty = risk_amount / (entry - SL)
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
| **EMA Crossover** | TRENDING | EMA9 crosses EMA21 | 1.5× ATR | 2.5× ATR | fast=9, slow=21 |
| **Mean Reversion** | RANGING | Price at Bollinger Band + RSI confirmation | 1.5× ATR | BB midline (SMA20) | BB(20, 2σ), RSI(14) oversold<30 / overbought>70 |
| **Breakout** | VOLATILE | Close breaks Donchian channel (20) with volume > 1.5× average | 2.0× ATR | 3.0× ATR | channel=20, vol_mult=1.5 |

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
| `TIMEFRAME` | `1h` | Binance intervals | Candle interval for strategy |
| `INITIAL_CAPITAL` | `10000` | > 0 | Fallback balance when Binance API is unreachable |
| `RISK_PER_TRADE` | `0.01` | 0.001 – 0.05 | Fraction of capital risked per trade |
| `DB_PATH` | `trading_bot.db` | writable path | SQLite database file location |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | Python log level |
| `TZ` | `UTC` | any IANA timezone | Timezone for log timestamps (e.g. `Europe/Madrid`) |
| `DECIMAL_SEPARATOR` | `dot` | `dot` / `comma` | Dashboard number format — `dot`: 1,234.56 · `comma`: 1.234,56 |

`RISK_PER_TRADE` must be between 0 and 0.05 (5%). The bot exits on startup if this is violated
(except in `--dry-run` mode, where validation is skipped).

**Risk manager defaults** (configurable via `RiskConfig` in code):

| Parameter | Default | Description |
|---|---|---|
| `max_drawdown` | 0.15 (15%) | Circuit breaker threshold |
| `max_concurrent_trades` | 1 | Maximum open positions |
| `min_signal_strength` | 0.4 | Minimum signal strength to trade |
| `cooldown_hours` | 4 | Circuit breaker cooldown duration |
| `trail_atr_mult` | 1.5 | Trailing SL distance in ATR units |
| `trail_activation_mult` | 1.0 | Price must move this many ATRs before trailing SL activates |

---

## Dashboard Guide

The dashboard auto-refreshes every 60 seconds. All times are UTC.

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
3. Image is pushed to `ghcr.io/${GITHUB_REPO}:latest`
4. Server pulls the new image and restarts via `docker compose pull && docker compose up -d`

**Setup:**

Set the `GITHUB_REPO` environment variable to your `username/repository` slug. The `docker-compose.yml`
references `ghcr.io/${GITHUB_REPO:-jorditomasg/trading-bot}:latest`.

Both the `bot` and `dashboard` services use the same image. No separate Dockerfiles needed.

---

## Troubleshooting

**Bot starts but no trades are opening**

Two common causes:

1. **Circuit breaker active** — drawdown > 15%. Check logs for `CIRCUIT BREAKER triggered`. Resets after 4 hours or on drawdown recovery.
2. **BiasFilter blocking signals** — if the 4h EMA9/21 are too close (< 0.1% gap) or the 4h fetch is failing, the filter returns `NEUTRAL` and blocks all directional signals. Check logs for `BiasFilter blocked signal` or `Failed to fetch 4h klines`. Also check that signal strength is reaching 0.4+ in the dashboard signal log.

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

This is normal for any trade that hasn't moved `trail_activation_mult × ATR` (default: 1 ATR)
from entry. NULL means the trailing stop has not activated yet. The static SL is still in effect.

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
