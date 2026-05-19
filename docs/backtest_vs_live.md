# Backtest vs Live — Fidelity Gaps

This document catalogs every known divergence between the backtest engine and
the live bot. Some gaps are **intentional design decisions** (conservative
biases). Others are **simplifications** that don't materially affect strategy
evaluation but should not be confused with live behaviour. A third bucket
contains items that have been **validated as benign** through empirical
audits.

> **Why this document exists**: gotchas #36 and #37 (May 2026) cost a full
> debug session because subtle dashboard↔live mismatches produced PF=0.90 vs
> the true PF=1.39. The runtime parity test now guards config divergence.
> This doc covers the runtime/data-flow divergence the test can't see.

Update this list whenever you change either `bot/backtest/engine.py` or
`main.run_cycle()` / `position_manager()`.

---

## 1. Intentional design decisions (keep)

These divergences are **on purpose**. Changing them requires a fresh
walk-forward audit (`scripts/audit/run_walk_forward.py`) before merging.

### 1.1 Both-wicked tiebreaker — engine stays conservative

**Live** (`gotcha #24`): when a 1m bar wicks BOTH SL and TP within the same
primary bar, the exit reason is chosen by close direction relative to entry —
close on the winning side → `TAKE_PROFIT`, otherwise → `STOP_LOSS`. Exit
price = bar close.

**Backtest** (`bot/backtest/engine.py`): when a primary bar wicks both SL and
TP, **SL wins**. There is no real fill price in simulation to anchor the
financial outcome to a "winning" close — so the pessimistic assumption
maintains statistical correctness. Exit price = SL level.

**Why keep**: bar-close as exit price requires a real fill counterfactual.
In sim we have neither real fills nor true intra-bar order, so the
conservative rule is statistically honest.

### 1.2 Live exit price after intra-bar wick — bar close, not level

**Live** (`gotcha #24`): when a 1m wick triggers SL or TP, the recorded exit
price is the **bar's `close`**, not the SL/TP level itself. Reasoning: when
we send the market close after detection, Binance fills at spot — which is
the bar close, not the wick. Empirically this beats "close at level" on max
drawdown by ~24% on a 3-year run.

**Backtest** (`bot/backtest/engine.py`): closes at the SL/TP **level**, not
the bar close.

**Why keep**: the live close-at-bar-close behaviour is a property of real
fill mechanics. Sim has no real fills — closing at level is the canonical
historical-data assumption used by every public backtest.

### 1.3 Single-strategy engine — no win-rate fallback

**Live** (current production): `StrategyOrchestrator` runs ONLY
`EMACrossoverStrategy` for all regimes. The historical win-rate fallback
(`win_rate < 40%` after `min 20 trades` triggers strategy override) was
removed.

**Backtest** (`bot/backtest/engine.py`): single-strategy only by default.
`strategy=` or `strategies_by_regime=` constructor args let research scripts
override.

**Why keep**: parity is correct. Past versions diverged; current versions
align.

---

## 2. Documented simplifications (acceptable)

Gaps that simplify simulation without distorting strategy evaluation. Listed
for completeness; do not "fix" without a measured improvement.

### 2.1 Funding rate is constant

**Live**: futures funding fluctuates 8-hourly. Sometimes positive (longs pay
shorts), sometimes negative.

**Backtest**: `BacktestConfig.funding_rate_per_8h` is a single constant
(default `0.0001` = 0.01% per 8h, typical BTC perp).

**Impact**: per-trade cost differs from true historical funding. Over a
3-year backtest of long-only BTC, expected error is bounded (~ ±$200 on a
$10k base on 100 trades). Doesn't affect strategy ranking.

### 2.2 No exchange filters in sim

**Live**: Binance enforces `LOT_SIZE` (stepSize), `MIN_NOTIONAL` (5 USDT
minimum), `PRICE_FILTER` (tickSize). Orders below the minimum are rejected
with `-1013`. Orders with wrong precision rejected with `-1111`.

**Backtest**: no minimum notional check. Quantity precision is the
`BacktestConfig.quantity_precision` field (not the real `LOT_SIZE` from
`exchangeInfo`).

**Impact**: in sim, tiny trades on small capital go through. On the
validated baseline ($10k capital, 1.5% risk, BTC), this never triggers — but
research scripts running on $100 capital will see backtest trades that
wouldn't fill live. Use realistic capital for credible numbers.

### 2.3 No partial fills, no order-book impact

**Live**: large market orders can partially fill at deteriorating prices on
thin order books.

**Backtest**: 100% fill at single price (entry close ± `cost_per_side_pct`).

**Impact**: for the validated baseline (~0.05% of typical BTC daily volume),
slippage is dominated by spread, not order-book depth. The flat
`cost_per_side_pct` is a reasonable upper bound. **Do not increase capital
to a level where this matters without re-validating.**

### 2.4 No latency between signal and fill

**Live**: from signal generation to order fill there's ~200ms–1s of latency
(API round-trip + matching engine). Price can move during that window.

**Backtest**: entry at the signal bar's `close` price exactly.

**Impact**: bounded by typical 4h-bar volatility (~0.5–1% per minute on
average). Strategy ranking unaffected; absolute returns slightly optimistic
in choppy regimes.

### 2.5 No live database state interaction during sim

**Live**: every `run_cycle()` reads `account_baseline`, `peak_capital`,
`bot_paused`, circuit-breaker state from DB. The circuit breaker can pause
trading mid-period.

**Backtest**: no DB. Engine tracks peak capital in memory; drawdown scaler
(when enabled) reads it directly.

**Impact**: the engine cannot reproduce a live "breaker triggered, then
recovered" scenario across sessions. For continuous-period backtests this
is irrelevant — the breaker reset logic (gotcha #4) is per-cycle and
state-equivalent.

---

## 3. Items to monitor

These have been validated as benign in the current baseline but should be
re-checked if any of the following change: capital, risk, leverage, symbol,
timeframe.

| Gap | Where to look | Re-validate when |
|---|---|---|
| Constant `funding_rate_per_8h` | `BacktestConfig.funding_rate_per_8h` | leverage>1, holding >7d on average |
| No minimum-notional check | `RiskManager.compute_position_size` | capital < $1000 OR risk < 0.5% |
| Single-price fills | `BacktestEngine._open_position` | trade size > 0.5% of daily volume |
| Backtest 1m mode | `BacktestConfig` (no field; pass `df_1m=` to `run`) | comparing intra-bar wick parity |

---

## 4. Known parity guarantees

Items that ARE faithful and should stay that way. The runtime parity test
(`tests/test_parity_runtime.py`) guards (1) and (2).

1. **Config keys**: every `bot_config` runtime key the dashboard reads is
   applied by live via `_apply_ema_config` / `_apply_runtime_config`. The
   parity test asserts equivalent attribute values across paths.
2. **Filter warmup**: dashboard fetches add `BIAS_WARMUP=30d` and
   `MOMENTUM_WARMUP=154d` so BiasFilter and MomentumFilter have enough
   history at `backtest_start`. The parity test asserts these constants
   meet the filter requirements.
3. **Strategy parameters**: both live and backtest read
   `bot/config_presets.py` for the per-timeframe base config, then override
   from DB. Single source of truth.
4. **Intra-bar SL/TP detection**: when `df_1m` is provided to
   `BacktestEngine.run()`, the engine slices 1m bars and applies the same
   wick logic as `position_manager._check_intra_bar_exit` (gotcha #24).
   Without `df_1m`, primary bar high/low is used (acceptable for coarse
   analysis but documented in 1.1).
5. **Drawdown scaler / vol-regime / Kelly**: applied identically in
   `BacktestEngine` and `PortfolioBacktestEngine` (gotcha #26 regression
   guard). Live applies via `orchestrator.step()` — semantically equal.

---

## 5. When to update this document

- Adding a new field to `BacktestConfig` that doesn't exist in live → add to
  section 2 with rationale.
- Adding a new state machine to live (`run_cycle` / `position_manager`)
  that isn't in `BacktestEngine` → add to section 1 or 2 with rationale.
- Closing a gap (making backtest more faithful) → remove from sections 1–3,
  re-run `scripts/audit/run_walk_forward.py`, link the audit in the commit.
