# Trailing exit + look-ahead fix — 2026-10-12

**Question:** why did live lose −11.4 % (14,015 → 12,422 USDT, 2026-06-03 →
2026-10-12) while its own universe rallied — BTC +29.5 %, ETH +39.3 %, SOL
+51.8 % — for a long-only trend follower validated at +15.9 %/yr?

**Answer, in two parts:**

1. **The validation was reading the future (gotcha #49).** The engine fed the
   daily bias and the weekly momentum with the bar *still forming* — including
   its final close. Removing that look-ahead halves the strategy: BTC+ETH
   +15.9 % → **+6.6 %/yr**, BTC+ETH+SOL **+5.5 %/yr**, Sharpe ~0.65-0.70.
   Live was never broken relative to an honest backtest; the backtest was
   broken relative to live.
2. **The fixed 5×ATR take-profit was the main drag (gotcha #50).** Replacing it
   with a 4×ATR chandelier stop and no TP doubles CAGR and Sharpe, in-sample
   and out-of-sample, on both universes.

Testnet fills were also checked against mainnet 4h opens for all 28 live
entries: identical to < 0.05 % (one 0.29 %). The testnet market is not the
cause.

## Method

- Data: Binance mainnet 4h/1d/1w klines, BTC+ETH 2017-08 → 2026-10-12,
  SOL 2020-08 →. Cost 0.10 %/side.
- Exploration on a fast re-implementation (`sim4h`, 2 s per run) calibrated
  against the official engine: with the look-ahead switched back on it
  reproduces the documented baseline (16.0 % vs 15.9 %, PF 1.46 vs 1.48, 501 vs
  477 trades); without it, it matches the fixed engine within ~0.5 pp.
- Final numbers come from the **official** `PortfolioBacktestEngine` via
  `build_backtest_config` (live parity, weekly momentum ON as live):
  `scripts/validate_trailing_exit_2026_10.py`.
- In-sample = up to 2022-12-31, out-of-sample = 2023-01-01 →.

## Results — official engine, no look-ahead

| Universe | Exit | CAGR | Max DD | Sharpe | Calmar | PF | Trades | IS Sharpe | OOS Sharpe | OOS CAGR |
|---|---|---|---|---|---|---|---|---|---|---|
| BTC+ETH 2018→ | SL 1.5 / TP 5 (old) | 6.6 % | −10.7 % | 0.70 | 0.62 | 1.31 | 417 | 0.88 | 0.46 | 4.1 % |
| BTC+ETH 2018→ | **SL 2 / trail 4 / no TP** | **14.1 %** | −10.8 % | **1.20** | **1.31** | **1.81** | 382 | 1.38 | **0.94** | **10.1 %** |
| BTC+ETH+SOL 2020-10→ | SL 1.5 / TP 5 (old) | 5.5 % | −9.2 % | 0.65 | 0.60 | 1.22 | 423 | 0.87 | 0.53 | 4.4 % |
| BTC+ETH+SOL 2020-10→ | **SL 2 / trail 4 / no TP** | **11.3 %** | **−8.4 %** | **1.15** | **1.36** | **1.65** | 389 | 1.34 | **1.06** | **10.6 %** |

Calendar years, BTC+ETH+SOL (2020-Q4 … 2026): old `9, 10, −2, 11, 10, −3, −1`;
new `14, 16, −2, 18, 20, 2, 1`.

## Robustness — plateau, not peak (sim, Sharpe full | IS | OOS)

BTC+ETH+SOL 2020-10 →:

| SL \ trail | 2.5 | 3 | 4 | 5 | 6 | EMA-cross exit |
|---|---|---|---|---|---|---|
| 1.5 | 0.83/1.22/0.60 | 0.89/1.18/0.73 | 1.08/1.38/0.90 | 1.18/1.48/0.99 | 1.28/1.61/1.06 | 1.22/1.82/0.86 |
| 2.0 | 0.83/1.38/0.53 | 0.88/1.26/0.67 | 1.17/1.48/1.00 | 1.24/1.72/0.93 | 1.29/1.83/0.93 | 1.32/2.00/0.94 |
| 2.5 | 0.88/1.29/0.65 | 0.94/1.27/0.77 | 1.30/1.60/1.14 | 1.29/1.80/0.96 | 1.37/1.97/0.96 | 1.49/2.08/1.16 |
| 3.0 | — | 0.92/1.23/0.76 | 1.27/1.61/1.10 | 1.26/1.85/0.87 | 1.34/1.99/0.90 | 1.48/2.05/1.16 |

(this grid predates adding the regime gate to the sim; the regime is TRENDING
~97 % of bars and the conclusions did not move when it was added.)

Old exit on the same sim: 0.65 / 0.94 / 0.47. Every cell with trail ≥ 4 beats
it in-sample **and** out-of-sample; BTC+ETH shows the same shape. Trail < 4
degrades toward the old exit — that is the region the legacy gotcha #1 test
sat in. SL 2 / trail 4 was chosen from the centre of the plateau, not its best
cell.

Neighbours on the chosen point (sim, BTC+ETH+SOL): risk 1 % → same Sharpe 1.23,
DD −5.4 %; risk 2 % → Sharpe 1.23, DD −10.4 %. Risk stays at 1.5 %.

## What was rejected, and why

- **Dropping the bias/momentum filters**: CAGR 17-21 % but DD −17 to −20 %, same
  Sharpe. Not worth doubling the drawdown.
- **Trail + EMA-cross combined exit**: worse than either alone.
- **Daily-timeframe trend systems** (SMA 50-200, Donchian, EMA 20/50, with or
  without vol targeting): CAGR 30-80 % but drawdowns −40 to −60 %, Calmar below
  the new 4h system on BTC+ETH.
- **Daily SMA50 trend exit** (SL 3, exit on daily close < SMA50): highest
  Sharpe in places (1.24-1.52) but only ~100 trades in 6-9 years and DD −15 to
  −18 %. Too few trades to trust.

## Caveats

- The live window (2026-06-03 →, 4 months) is too short to judge anything;
  the new exit is −5 % on BTC+ETH and +1.7 % on BTC+ETH+SOL there. The edge is
  measured over 6-9 years, not 4 months.
- A trend follower without a TP gives back part of each winner by design
  (4×ATR from the peak). Expect fewer, larger wins and more "gave back 8 %"
  exits.
- After a bot outage, live ratchets with the current ATR and the max high since
  entry instead of bar-by-bar (gotcha #50). Never looser than the stored stop.
