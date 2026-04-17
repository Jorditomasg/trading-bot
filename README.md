# * Trading Bot — BTC/USDT

Algorithmic trading bot for Binance Testnet. Paper trading only — no real funds.

## Stack

- **Exchange**: Binance Testnet (BTC/USDT, H1)
- **Regime detection**: ATR volatility override → ADX → Hurst exponent (R/S analysis)
- **Strategies**: EMA Crossover · Mean Reversion · Donchian Breakout
- **Risk**: 1% per trade · 15% circuit breaker · dynamic position sizing
- **Storage**: SQLite
- **Dashboard**: Streamlit (Nothing OS theme)
- **Infra**: Docker · GitHub Actions CI/CD

## Architecture

```
Binance API
    │
    ▼
RegimeDetector  ──►  TRENDING   ──►  EMA Crossover
                ──►  RANGING    ──►  Mean Reversion
                ──►  VOLATILE   ──►  Breakout
                          │
                          ▼
                    RiskManager  ──►  position size · circuit breaker
                          │
                          ▼
                      SQLite DB  ──►  Streamlit Dashboard
```

## Quickstart

```bash
# 1. Clone and configure
git clone https://github.com/Jorditomasg/trading-bot.git
cd trading-bot
cp .env.example .env
nano .env  # add your Binance Testnet API keys

# 2. Run with Docker
docker compose pull
docker compose up -d

# 3. Dashboard
open http://localhost:8501
```

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py --dry-run
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `BINANCE_API_KEY` | — | Testnet API key |
| `BINANCE_API_SECRET` | — | Testnet API secret |
| `BINANCE_TESTNET` | `true` | Use testnet endpoint |
| `SYMBOL` | `BTCUSDT` | Trading pair |
| `TIMEFRAME` | `1h` | Candle interval |
| `INITIAL_CAPITAL` | `10000` | Simulated USDT capital |
| `RISK_PER_TRADE` | `0.01` | Risk per trade (1%) |
| `DB_PATH` | `trading_bot.db` | SQLite database path |

## Get Testnet API keys

1. Go to [testnet.binance.vision](https://testnet.binance.vision)
2. Log in with GitHub
3. Generate HMAC keys
4. Paste into `.env`
