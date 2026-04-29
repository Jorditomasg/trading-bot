# Research scripts

Reproducible backtests that drove the bot's current default config.

All scripts read OHLCV from `data/klines/*.parquet` (no network) and write
result tables to `data/*.json` — both directories are gitignored.

## Run order

```bash
.venv/bin/python -u scripts/research/baseline.py
.venv/bin/python -u scripts/research/hypotheses.py
.venv/bin/python -u scripts/research/round2_risk_scaling.py
.venv/bin/python -u scripts/research/round3_macro_postfilter.py
.venv/bin/python -u scripts/research/round3b_macro_engine.py
.venv/bin/python -u scripts/research/oos_validation.py
.venv/bin/python -u scripts/research/round4_y3_focused.py
```

`.venv/bin/python` is required (pyarrow lives there, not in system Python).

## Findings (April 2026)

| Round | Finding |
|---|---|
| baseline | Documented optimum reproduced: 22.57% CAGR / Sharpe 9.63 / PF 1.55 / DD 20.51% / WR 38.9% / 90 trades over 2022-04→2025-04. |
| 1 | Single-knob sweep (TP, SL, max_distance, momentum band) — no winner outright. Weekly 20-SMA momentum filter improves Sharpe & DD risk-adjusted. |
| 2 | H1c (8% band) + risk scaling: risk 3% → 31.5% CAGR / DD 18% / PF 1.62. Strict Pareto improvement. |
| 3 / 3b | Stacked daily EMA200 filter on top of H1c → 57% CAGR full sample. **Looked great in-sample.** |
| OOS | Y3 (2024-04→2025-04) revealed the daily-EMA stack was OVERFIT: PF 1.18, DD 30%. Discarded. |
| 4 | H1c-only at risk 4% survives every subsample. **Adopted as default.** |

## Adopted config

- `momentum_filter_enabled = True`
- `momentum_sma_period = 20`
- `momentum_neutral_band = 0.08`
- `risk_per_trade = 0.04`
- All other live values unchanged from documented optimum
  (long_only=True, no trailing stop, ema_max_distance_atr=1.0, TP=4.5, SL=1.5)

## Subsample summary (H1c only at risk 4%)

| Window | CAGR | Sharpe | PF | MaxDD | Trades |
|---|---:|---:|---:|---:|---:|
| FULL 2022-04→2025-04 | 42.08% | 10.56 | 1.59 | 23.86% | 83 |
| Y1 2022-04→2023-04 (bear) | +2.53% | 2.37 | 0.84 | 23.86% | 17 |
| Y2 2023-04→2024-04 (bull) | +107.08% | 16.96 | 2.24 | 19.49% | 32 |
| Y3 2024-04→2025-04 (OOS)  | +37.37% | 8.17 | 1.41 | 12.68% | 33 |

## Re-running on fresh data

`baseline.py` loads from cache. To extend the cache forward run:
`.venv/bin/python -c "from bot.backtest.cache import fetch_and_cache; from datetime import datetime, timezone; fetch_and_cache('BTCUSDT', '4h', datetime(2022,4,1,tzinfo=timezone.utc), datetime.now(tz=timezone.utc))"`
