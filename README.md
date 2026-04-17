# * Trading Bot — BTC/USDT

Regime-adaptive algorithmic trading bot for Binance Testnet. Automatically selects between three strategies based on real-time market regime detection.

![Python 3.12](https://img.shields.io/badge/Python-3.12-blue?style=flat-square)
![Docker](https://img.shields.io/badge/Docker-compose-2496ED?style=flat-square)
![Binance Testnet](https://img.shields.io/badge/Binance-Testnet-F0B90B?style=flat-square)

---

## Overview

The bot runs on a 1-hour candle cycle. Each cycle it:

1. Fetches the last 200 OHLCV candles from Binance
2. Classifies the market as TRENDING, RANGING, or VOLATILE using a 3-level detection cascade
3. Selects the best-fit strategy for the current regime (with live win-rate fallback logic)
4. Generates a signal and validates it through a risk manager
5. Opens or closes a position on Binance, writes results to SQLite
6. Records an equity snapshot for the dashboard

**Key features:**

- 3-level regime detection: ATR volatility spike → ADX → Hurst exponent (R/S analysis)
- 3 strategies, each tuned to a different market condition
- Dynamic position sizing: risk a fixed % of capital per trade (default 1%)
- Trailing stop-loss that activates after a configurable ATR distance
- Circuit breaker: halts trading on >15% drawdown; auto-resets after 4 hours or recovery
- Win-rate fallback: switches away from underperforming strategies automatically
- Nothing OS dashboard — real-time Streamlit UI with equity curve, drawdown, P&L, signal log
- Full dry-run mode: no exchange calls, but equity curve is still recorded

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
                  └────────────┬────────────┘
                               │ pd.DataFrame
                  ┌────────────▼────────────┐
                  │   StrategyOrchestrator   │
                  │                          │
                  │  ┌────────────────────┐  │
                  │  │  RegimeDetector    │  │
                  │  │                    │  │
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
                  │  signals tables          │
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
| `BINANCE_API_KEY` | — | required (live) | Binance Testnet HMAC API key |
| `BINANCE_API_SECRET` | — | required (live) | Binance Testnet API secret |
| `BINANCE_TESTNET` | `true` | `true` / `false` | Use testnet endpoint |
| `SYMBOL` | `BTCUSDT` | any Binance pair | Trading pair |
| `TIMEFRAME` | `1h` | Binance intervals | Candle interval for strategy |
| `INITIAL_CAPITAL` | `10000` | > 0 | Starting USDT capital (equity tracking) |
| `RISK_PER_TRADE` | `0.01` | 0.001 – 0.05 | Fraction of capital risked per trade |
| `DB_PATH` | `trading_bot.db` | writable path | SQLite database location |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR | Python log level |

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

**Topbar** — Bot name, running status pill, testnet badge, current regime badge, last refresh time.

**KPI Row** — Six cards: Balance, Total PnL ($ and %), Win Rate, Annualised Sharpe Ratio, Max Drawdown, Total Closed Trades.

**Equity Curve** — Balance over time. Line is white above starting capital, red below. Dotted reference line marks initial capital.

**State Panel** — Current regime + active strategy. Drawdown chart (inverted Y axis) with 15% circuit breaker reference line. Open position card (entry, SL, TP, quantity) or "NO OPEN POSITION".

**Strategy Performance** — Horizontal bar chart, win rate per strategy. Bars are white above 50%, red below. Each bar shows trade count.

**P&L Distribution** — Histogram of closed trade P&L. Wins (white) and losses (red) overlaid.

**Risk Metrics** — Profit Factor, Max Loss Streak, Average Win $, Average Loss $. Below: win rate per regime.

**Trade History** — Last 50 closed trades. PnL in white (positive) or red (negative). Exit reason column shows STOP_LOSS, TAKE_PROFIT, TRAILING_STOP, or SIGNAL_REVERSAL.

**Signal Log** — Last 20 signals generated. Shows timestamp, strategy, regime, action (BUY/SELL/HOLD), and strength score.

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
references `ghcr.io/${GITHUB_REPO:-youruser/trading-bot}:latest`.

Both the `bot` and `dashboard` services use the same image. No separate Dockerfiles needed.

---

## Troubleshooting

**Bot starts but no trades are opening**

The circuit breaker may be active (drawdown > 15%). Check logs for `CIRCUIT BREAKER triggered`.
It resets after 4 hours or when drawdown recovers. Also check that signal strength is reaching 0.4+
— the signal log in the dashboard shows `STR` per signal.

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

This software is for educational and paper trading purposes only. It operates exclusively on
Binance Testnet with simulated funds. Do not use this code to trade real assets without
understanding the risks. Past performance in simulation does not guarantee future results.
The authors accept no liability for financial losses of any kind.
