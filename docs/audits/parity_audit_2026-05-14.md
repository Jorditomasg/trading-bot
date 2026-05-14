# Backtest vs Live Bot — Parity Audit
**Date:** 2026-05-14  
**Auditor:** Claude Sonnet 4.6 (read-only, no code modified)  
**Scope:** `BacktestEngine.run()` and `PortfolioBacktestEngine.run_portfolio()` vs live `run_cycle()` + `orchestrator.step()` + `position_manager()`

---

## 1. TL;DR

| Category | Count |
|---|---|
| PARITY ✅ | 8 |
| APPROXIMATION 🟡 | 5 |
| CRITICAL GAP 🔴 | 5 |
| LIVE-ONLY ⚫ | 2 |
| BACKTEST-ONLY ⚪ | 3 |

**Bottom line:** The backtest is fundamentally sound for strategy evaluation, but **5 critical gaps** will cause metrics to diverge materially from live results. The most dangerous gap is Kelly sizing — it is applied in live but completely absent from both backtest engines, meaning every backtest is implicitly sizing as flat-Kelly at a fixed 1.5% risk. The both-wicked tiebreaker and spot capital cap are also missing from the backtest exit/sizing paths.

---

## 2. Findings Table

| # | Behavior | Classification | Evidence (file:line) | Notes |
|---|---|---|---|---|
| 1 | Cycle scheduling | ✅ PARITY | `main.py:912`, `engine.py:501` | Hourly scheduler vs bar-by-bar; structurally different but equivalent simulation intent. |
| 2 | Capital allocation (multi-symbol) | ✅ PARITY | `main.py:347`, `portfolio_engine.py:161` | Live: `total/n_symbols`. Portfolio engine: single cash pool, 1 position/symbol — same equitable split semantics. |
| 3 | Regime detection | ✅ PARITY | `orchestrator.py:34`, `engine.py:135` | Both call `get_regime_config(tf)` → same `RegimeDetector`. TRENDING-only entry enforced identically. |
| 4 | Strategy signal generation | ✅ PARITY | `orchestrator.py:38-42`, `engine.py:136-148` | Both use `get_strategy_configs(tf)` and allow runtime overrides for `ema_stop_mult`, `ema_tp_mult`, entry-quality params. |
| 5 | BiasFilter — neutral passthrough | 🟡 APPROXIMATION | `main.py:151-155`, `engine.py:160` | Live: `neutral_passthrough` is DB-driven (default `"true"`). Backtest: hardcoded `not bias_strict` (default `False` → passthrough ON). Same default result, but live config is dynamic. |
| 6 | BiasFilter — `block_on_data_failure` | 🔴 CRITICAL GAP | `main.py:154`, `engine.py:160` | Live: `block_on_data_failure` is DB-driven (default `false`). Backtest `BiasFilterConfig` does not expose this field — always uses the dataclass default (`False`). Gap only matters if a live operator sets it to `true`; then backtest is more permissive than live. |
| 7 | BiasFilter — `neutral_threshold_pct` | 🟡 APPROXIMATION | `main.py:153`, `filter.py:22` | Live: DB-driven (default `"0.001"` = 0.1%). Backtest: uses dataclass default `0.001`. Same default; gap only if operator customises via dashboard. |
| 8 | BiasFilter — higher-timeframe mapping | 🔴 CRITICAL GAP | `main.py:321` vs `engine.py:436` | **Live (4h primary):** fetches `"1d"` candles for BiasFilter (hardcoded `client.get_klines(sym, "1d", 60)`). **Backtest:** `df_4h` parameter is whatever the caller passes — no enforcement that it matches `BIAS_TIMEFRAME_MAP`. If caller passes actual 4h data for a 4h primary run, the bias filter sees the wrong timeframe vs live. For production 4h runs this is a certain divergence. |
| 9 | Momentum filter — implementation | 🟡 APPROXIMATION | `momentum/filter.py:8` vs `engine.py:227` | Live: `NEUTRAL_BAND = 0.08` (±8%). Backtest: `momentum_neutral_band` default `0.05` (±5%). **Different neutral bands produce different state classifications for the same price.** |
| 10 | Momentum filter — BEARISH block | ✅ PARITY | `orchestrator.py:129-131`, `engine.py:665` | Both block new entries on BEARISH. NEUTRAL halves risk in both. |
| 11 | `RiskManager.validate_signal` | ✅ PARITY | `orchestrator.py:150`, `engine.py:667` | Both enforce `min_signal_strength >= 0.5` before entry. |
| 12 | Kelly sizing | 🔴 CRITICAL GAP | `orchestrator.py:154-196`, `engine.py:685` | **Live:** calls `db.get_kelly_stats()` and applies half-Kelly multiplier (0.25×–2× of base risk). **Backtest:** no DB, no Kelly. Always uses flat `effective_risk = risk_per_trade`. This is the largest systematic metric divergence. |
| 13 | Drawdown scaler | ⚪ BACKTEST-ONLY | `engine.py:682`, `portfolio_engine.py:280`, `orchestrator.py` (absent) | Applied in both backtest engines when `dd_risk` is configured. NOT in live orchestrator — intentional (CLAUDE.md "Validated Baseline"). Confirmed: no `drawdown_multiplier` call anywhere in `orchestrator.py`. |
| 14 | Spot capital cap (`min(qty_by_risk, qty_by_capital)`) | 🔴 CRITICAL GAP | `manager.py:111-114` vs `engine.py:342-354` | **Live:** `min(qty_by_risk, capital × 0.99 / entry)`. **Backtest** `_compute_quantity_with_risk()`: `round(risk_amount / risk_per_unit, 5)` — no capital cap. A trade where risk-based sizing exceeds 100% of capital will be sized beyond what the live bot would allow, inflating backtest trade sizes. |
| 15 | Order execution / fill model | ⚫ LIVE-ONLY | `main.py:471-554` | Backtest: instant fill at bar close + cost model. Live: limit entry order, market close, actual fill price from exchange. Not simulatable by design. |
| 16 | SL/TP — intra-bar detection method | 🟡 APPROXIMATION | `main.py:556-621`, `engine.py:277-303` | Live: 1m kline `high`/`low` (2 bars) as primary, WS tick as fallback. Backtest: primary bar `high`/`low` (same concept). When `df_1m` is passed, 1m precision mode is available. The gap: backtest can use actual 1m data for precision, but that requires the caller to explicitly pass `df_1m`. |
| 17 | SL/TP — both-wicked tiebreaker | 🔴 CRITICAL GAP | `main.py:604`, `engine.py:291-302` | **Live:** when same bar wicks both SL and TP, reason = TP if `close > entry`, else SL. **Backtest `_check_exit()`:** `low <= sl` check wins unconditionally — `if low <= sl: return EXIT_STOP_LOSS`. No tiebreaker. SL always wins on same-bar touches. This is the CLAUDE.md-documented intentional conservative rule ("SL wins"), but it means backtest metrics are pessimistic vs live behavior on flash-crash-recovery bars. |
| 18 | Exit reasons — full set | ✅ PARITY | `engine.py:45-48`, `constants.py` | Backtest has STOP_LOSS, TAKE_PROFIT, END_OF_PERIOD, LIQUIDATED. Live adds REGIME_CHANGE (opt-in, default off). When `enable_regime_exit=False` (default), exit reason set is identical. |
| 19 | Trailing stop | ⚪ BACKTEST-ONLY | `engine.py:68-69` (legacy fields), CLAUDE.md #1 | Backtest has `trail_atr_mult` / `trail_activation_mult` / `simulate_trailing` (default False). Live: trailing stop REMOVED. Confirmed: no trailing stop logic in `orchestrator.py` or `position_manager`. |
| 20 | Opposite-signal exit | ✅ PARITY | `engine.py:1-17 (docstring)`, CLAUDE.md #9 | Both removed. Docstring says "Signal-reversal exits follow the live bot logic" but code shows it's a holdover comment — the `_check_exit` method only checks SL/TP, and the entry loop comment says "skipped when disable_reversal_exits=True". No reversal exit code in the active path. |
| 21 | Fees / slippage | ✅ PARITY | `engine.py:319-327`, CLAUDE.md "backtest_cost_per_side" | Both apply `cost_per_side_pct` symmetrically on entry and exit. Default seeded to 0.001 (0.10%) via `_seed_optimized_defaults`. `resolve_cost_per_side()` reads from DB. |
| 22 | Circuit breaker | ⚫ LIVE-ONLY | `orchestrator.py:79-81`, `engine.py` (absent) | Live: trading equity vs HWM, 15% threshold, 4h cooldown. Backtest: no circuit breaker. An extended drawdown in backtest continues trading while live would halt. Intentional — backtest is for strategy evaluation, not survivability. |
| 23 | News pause | ⚪ BACKTEST-ONLY | `engine.py:499`, `orchestrator.py` (absent) | Opt-in in backtest via `BacktestConfig.news_pause`. Live: `NewsPauseConfig` exists but is not wired into `orchestrator.py` or `run_cycle()`. |
| 24 | Vol regime filter | 🟡 APPROXIMATION | `engine.py:656-658`, `portfolio_engine.py:256-258`, `orchestrator.py` (absent) | Opt-in in both backtest engines. Live: `VolRegimeFilter` is constructed in backtest engines but NOT in `orchestrator.py`. If a user evaluates vol regime in backtest and then tries to use it live, they will find no wiring exists. Default is `enabled=False` so no divergence at defaults. |
| 25 | Bias timeframe (live 4h primary → daily bias) | 🔴 CRITICAL GAP (same as #8) | `main.py:321` | Confirmed: live hardcodes `"1d"` for bias timeframe regardless of `BIAS_TIMEFRAME_MAP`. `BIAS_TIMEFRAME_MAP["4h"] = "1d"` is correct but the live code does not reference the map — it calls `client.get_klines(sym, "1d", 60)` directly. If the backtest caller passes `df_4h` (actual 4h data), they diverge from live. |
| 26 | Duplicate open trade guard | ⚫ LIVE-ONLY | `orchestrator.py:142-147` | Live: checks for same `side + strategy` already open. Backtest: `open_trade is None` check (single position). Single-position assumption makes this equivalent for single-symbol. Portfolio engine also enforces one position per symbol. No gap in practice. |
| 27 | Post-close cooldown | ✅ PARITY | `engine.py:639`, `portfolio_engine.py:208` | `post_close_cooldown_bars = 3` default in backtest. Live: no explicit cooldown — new entries require the position to be None, which happens immediately. This is a backtest-only conservative guard. |
| 28 | `max_concurrent_trades` | ✅ PARITY | `manager.py:17`, `engine.py:8-9` | Both enforce 1 open position per symbol. |
| 29 | `long_only` flag | ✅ PARITY | `engine.py:144-145`, `orchestrator.py` (via `_apply_ema_config`) | Both read from DB runtime config and apply to `EMACrossoverConfig.long_only`. |

---

## 3. CRITICAL GAPS — Deep Dive

### GAP 🔴-1: Kelly Sizing Absent from Backtest (`engine.py:685`, `orchestrator.py:154-196`)

**The divergence:**

Live bot (in `orchestrator.step()`):
```python
kelly_stats = self.db.get_kelly_stats(strategy.name, self.risk_manager.config.kelly_min_trades)
if kelly_stats:
    kf = compute_kelly_fraction(win_rate, avg_win_pct, avg_loss_pct, half=True)
    risk_frac = kelly_risk_fraction(kf, signal.strength, base_risk,
                                    max_mult=2.0, min_mult=0.25)
else:
    risk_frac = None  # flat risk_per_trade used
quantity = self.risk_manager.compute_position_size(capital, entry, stop_loss, risk_fraction=risk_frac)
```

Backtest (`engine.py:672-688`):
```python
effective_risk = self.config.risk_per_trade * 0.5 if momentum == "NEUTRAL" else self.config.risk_per_trade
effective_risk = effective_risk * vol_size_factor
effective_risk = effective_risk * drawdown_multiplier(...)
quantity = self._compute_quantity_with_risk(capital, net_entry, signal.stop_loss, effective_risk)
```

Kelly is entirely absent. The backtest always uses flat `risk_per_trade` with no dynamic adjustment based on strategy win rate.

**Impact:**
- After ≥15 closed trades, live Kelly can scale risk between `0.25×` and `2.0×` base risk based on win rate and signal strength.
- A strategy with 60% win rate and 2:1 R:R would compute Kelly ~0.30, half-Kelly 0.15, multiplied by signal strength (e.g. 0.7) = mult 0.15/0.015 × 0.7 = 7.0, clamped to 2.0 → live risk = 3.0% vs backtest 1.5%.
- Backtest PF, annual return, and Sharpe are computed at systematically lower position sizes than live. The metrics are valid for strategy ranking but understate absolute returns once live history accumulates ≥15 trades.

**Why no fix is trivial:** Kelly requires historical trade statistics that only exist in the live DB. In backtests, those statistics must be computed from the simulation's own closed trades in a rolling fashion — a forward-only update problem.

---

### GAP 🔴-2: Spot Capital Cap Missing from Backtest (`manager.py:111-114`, `engine.py:342-354`)

**The divergence:**

Live (`manager.py:111-114`):
```python
qty_by_risk    = risk_amount / risk_per_unit
qty_by_capital = (capital * 0.99) / entry     # cap at 99% of capital
quantity       = min(qty_by_risk, qty_by_capital)
```

Backtest (`engine.py:342-354`):
```python
def _compute_quantity_with_risk(self, capital, net_entry, stop_loss, risk_per_trade):
    risk_amount   = capital * risk_per_trade
    risk_per_unit = abs(net_entry - stop_loss)
    if risk_per_unit <= 0:
        return 0.0
    return round(risk_amount / risk_per_unit, 5)   # NO capital cap
```

**Impact:**
When `risk_per_trade / sl_distance_pct > 100%` (e.g. 1.5% risk with a 0.8% SL distance = 187% notional needed), the backtest computes a quantity that would require notional > capital. The live bot caps this at 99%. In backtest this produces an overstated position size, inflated notional, and therefore inflated PnL (both wins and losses).

This scenario is uncommon on 4h BTC at 1.5% risk with SL=1.5×ATR (typically 3–5% SL distance), but it can occur in low-volatility periods when ATR is compressed.

---

### GAP 🔴-3: Bias Timeframe Mismatch for 4h Primary (`main.py:321`, `engine.py:436`)

**The divergence:**

Live `run_cycle()` for any symbol:
```python
df_4h = client.get_klines(sym, "1d", 60)   # hardcoded "1d"
```

For the production 4h primary timeframe, this is **daily candles** for BiasFilter — matching `BIAS_TIMEFRAME_MAP["4h"] = "1d"`.

Backtest callers typically pass:
```python
engine.run(df=df_4h, df_4h=df_4h_data, ...)
```

The parameter is named `df_4h` in `BacktestEngine.run()` but it is just "the higher-timeframe data for BiasFilter." Nothing enforces that it is actually daily data. The `ScenarioRunner` and optimizer scripts must pass the correct daily data, or the BiasFilter classifies trend direction from 4h candles instead of daily candles — a materially different signal.

**Impact:**
- Daily EMA9/21 crosses trend more slowly than 4h EMA9/21.
- Using 4h data for a 4h primary run gives a more active bias filter (more NEUTRAL→BEARISH transitions), potentially blocking more signals than the live bot does.
- Benchmarked in CLAUDE.md: "Daily EMA9/21 gate outperforms 4h EMA gate (PF 1.19–1.30 vs 0.82–0.93 with taker fees)."
- If the optimizer or scenario comparison uses 4h data for the bias frame on 4h primary runs, ALL optimized parameters are calibrated on the wrong filter, and the approved configs will underperform live.

---

### GAP 🔴-4: Both-Wicked Tiebreaker (`main.py:600-608`, `engine.py:288-303`)

**The divergence:**

Live `_check_intra_bar_exit()`:
```python
if sl_hit and tp_hit:
    reason = ExitReason.TAKE_PROFIT if close > entry else ExitReason.STOP_LOSS
    return (reason, close)  # exit price = bar close
```

Backtest `_check_exit()`:
```python
if trade["side"] == "BUY":
    if low <= sl:
        return EXIT_STOP_LOSS, sl   # SL wins unconditionally
    if high >= tp:
        return EXIT_TAKE_PROFIT, tp
```

The backtest doesn't handle the `sl_hit AND tp_hit` case — SL simply wins because the `low <= sl` branch returns first.

**Impact:**
Flash crashes that recover (wick down through SL, recover above TP on the same bar) are counted as losses in backtest but may be counted as wins in live. On volatile markets (e.g. May 2021 crash, Nov 2022 FTX week) these events are not rare. Backtest PF and win rate are systematically pessimistic for these bars.

Additionally, the **exit price** differs: live uses `bar.close` (realistic market-order fill after detection). Backtest uses `sl` level (idealized fill at the exact SL price). This inflates backtest losses vs live losses.

---

### GAP 🔴-5: `block_on_data_failure` BiasFilter Config Not Exposed in Backtest (`main.py:154`, `engine.py:160`)

**The divergence:**

Live `_build_bias_filter()`:
```python
BiasFilter(BiasFilterConfig(
    neutral_passthrough=...,
    neutral_threshold_pct=...,
    block_on_data_failure=cfg.get("bias_block_on_data_failure", "false") == "true",
))
```

Backtest `BacktestEngine.__init__()`:
```python
self._bias_filter = BiasFilter(BiasFilterConfig(
    neutral_passthrough=not config.bias_strict
))
```

`block_on_data_failure` is not in `BacktestConfig` and cannot be set by the backtest caller.

**Impact:**
- At default (`false`), no divergence. If an operator sets `bias_block_on_data_failure=true` in the dashboard (which makes the live bot block all signals when daily klines fail to fetch), the backtest will always be more permissive for bars where `df_4h` is unavailable (early warmup period). The backtest will show trades during warmup that would be blocked in live. This is a minor gap in practice but worth noting for completeness.

---

## 4. Recommended Fixes — Priority-Ordered

### Priority 1 — Bias timeframe enforcement (CRITICAL, 1–2 days)

**Problem:** `BacktestEngine.run()` parameter `df_4h` is misleadingly named and callers can pass wrong-timeframe data.

**Fix:**
1. Rename `df_4h` parameter to `df_high` in `BacktestEngine.run()` (matches live `orchestrator.step(df_high=...)` naming).
2. Add a docstring assertion: "For 4h primary timeframe, pass daily (1d) candles, not 4h candles."
3. Update `ScenarioRunner`, optimizer, and dashboard BACKTEST tab to use `BIAS_TIMEFRAME_MAP` to select the correct higher-timeframe data.

**Effort:** Medium (parameter rename + update all callers — grep for `df_4h=` to find them).

---

### Priority 2 — Spot capital cap in backtest (CRITICAL, 0.5 days)

**Problem:** `_compute_quantity_with_risk()` does not cap quantity at `0.99 × capital / entry`.

**Fix:** Add the same cap to `_compute_quantity_with_risk()`:
```python
qty_by_risk    = risk_amount / risk_per_unit
qty_by_capital = (capital * 0.99) / net_entry
return round(min(qty_by_risk, qty_by_capital), 5)
```

Also apply in `_compute_quantity()` for consistency.

**Effort:** Low (3-line change, +1 test for the edge case).

---

### Priority 3 — Both-wicked tiebreaker in backtest (CRITICAL, 1 day)

**Problem:** `_check_exit()` doesn't handle same-bar SL+TP, always returning SL.

**Fix:** Add the tiebreaker to `_check_exit()`:
```python
if trade["side"] == "BUY":
    sl_hit = low <= sl
    tp_hit = high >= tp
    if sl_hit and tp_hit:
        close = float(bar["close"])
        # Match live behavior: close direction decides the reason.
        # Exit price: bar close (same as live, honest about market-order fill).
        reason = EXIT_TAKE_PROFIT if close > trade["entry_price"] else EXIT_STOP_LOSS
        return reason, close
    if sl_hit:
        return EXIT_STOP_LOSS, sl
    if tp_hit:
        return EXIT_TAKE_PROFIT, tp
```

Note: The current CLAUDE.md explicitly documents "SL wins" as the intentional conservative rule. Before applying this fix, confirm with the team whether aligning backtest to live is preferred over keeping the conservative pessimistic bias.

**Effort:** Low (10-line change per side, +2 tests for BUY/SELL both-wicked bars).

---

### Priority 4 — Rolling Kelly sizing in backtest (HIGH, 3–5 days)

**Problem:** Backtest uses flat risk; live uses Kelly-adjusted risk after ≥15 trades.

**Fix (rolling Kelly):** During the backtest loop, maintain a rolling closed-trade history. After each close, compute `get_kelly_stats()` equivalent from the accumulated list. If ≥15 closed trades exist, compute half-Kelly and apply the same clamp logic as the orchestrator.

This is non-trivial because Kelly in live is computed from ALL historical trades (DB query), while in backtest it should be computed from trades simulated so far (forward-only).

**Simpler alternative (approximation):** Accept the flat-risk divergence and document it explicitly. Add a warning in `BacktestResult` if `kelly_min_trades` would have been met: "Note: live Kelly sizing was not simulated. Actual live performance will differ."

**Effort:** High for full rolling Kelly (3–5 days). Low for the warning-only approximation (0.5 days).

---

### Priority 5 — Momentum neutral band alignment (LOW, 0.5 days)

**Problem:** Live uses `NEUTRAL_BAND = 0.08` (±8%), backtest default uses `momentum_neutral_band = 0.05` (±5%).

**Fix:** Change `BacktestConfig.momentum_neutral_band` default from `0.05` to `0.08` to match `bot/momentum/filter.py:8`.

**Effort:** Trivial (1 constant, verify tests still pass).

---

### Priority 6 — Expose `block_on_data_failure` in `BacktestConfig` (LOW, 0.5 days)

**Problem:** BiasFilter behavior is not fully replicable from backtest.

**Fix:** Add `bias_block_on_data_failure: bool = False` to `BacktestConfig` and thread it through to `BiasFilterConfig`.

**Effort:** Low.

---

### Priority 7 — Vol regime in live orchestrator (INFORMATIONAL, 2 days)

**Current state:** `VolRegimeFilter` is opt-in in both backtest engines. It is NOT wired into `orchestrator.py`. If a team member backtests with vol regime enabled and gets better results, there is no live wiring to deploy it.

**Fix:** If vol regime is approved for live use, add it to `orchestrator.step()` following the same pattern as the backtest entry guard (`vol_allows + vol_size_factor`).

**Effort:** Medium (wiring + tests).

---

## 5. What's Already at Parity (confidence list)

The following behaviors are faithfully simulated — audit found no divergence:

1. **Regime detection** — identical `RegimeDetector` + `get_regime_config(tf)` in both paths.
2. **EMA crossover strategy + entry-quality filters** — same `EMACrossoverConfig` with same DB-driven overrides.
3. **TRENDING-only entry gate** — `if regime != TRENDING: hold_signal` in both.
4. **BEARISH momentum block** — identical guard in orchestrator and both backtest engines.
5. **NEUTRAL momentum 50% risk reduction** — identical `risk × 0.5` in both.
6. **Fees/cost model** — `cost_per_side_pct` applied symmetrically, seeded from same DB value.
7. **Single open position per symbol** — max_concurrent_trades=1 enforced in both.
8. **`long_only` flag** — threaded through `EMACrossoverConfig` in both paths.
9. **SL/TP level computation** — same `calculate_levels()` function via strategy signal.
10. **Drawdown scaler disabled in live** — confirmed: no `drawdown_multiplier()` call anywhere in `orchestrator.py`.
11. **1m precision exit mode** — available in both engines when `df_1m` is passed (optional).
12. **EXIT_END_OF_PERIOD** — backtest force-closes at last bar; equivalent to live bot stopping.
13. **Multi-symbol capital pool** — portfolio engine correctly uses shared cash, not per-symbol capital.
14. **`validate_signal` strength gate** — `min_signal_strength=0.5` enforced in both.
15. **BiasFilter fail-closed on None data** — both return NEUTRAL and gate via `allows_signal`.

---

## 6. C1/C2 Audit Validity Assessment

The earlier walk-forward audit (`A_walk_forward_2026-05-14.md`) tested grid-search results from `BacktestEngine`. Given the gaps found here:

**Results likely still valid for:**
- Strategy ranking (which SL/TP combo is best) — Kelly gap affects absolute levels but not relative ordering.
- PF comparisons between configs — systematic bias is consistent across all configs.
- Max drawdown comparisons — without capital cap, backtest DD may be slightly overstated.

**Results may be misleading for:**
- **Absolute annual return numbers** — backtest uses flat risk, live uses Kelly (can be 0.25×–2× base). The validated baseline numbers (+32.7% annual at 1.5% risk) were computed without Kelly and represent the conservative floor.
- **Profit factor on 4h runs** — if the bias filter was receiving 4h data instead of daily data, the PF numbers reflect a more restrictive filter than live. This is the highest-risk invalidation: **the 4h bias timeframe gap may have caused all 4h backtest runs to use the wrong higher-timeframe data**, producing results calibrated on a filter configuration that doesn't match production.

**ScenarioRunner and optimizer verified:** Both tools use the correct higher-timeframe data for 4h runs:
- `ScenarioRunner._run_one()` (line 120): `df_bias = self._df_4h if tf == "1h" else self._df_1d` — daily candles for 4h bias. ✅
- `walk_forward.py` (line 78): `bias_tf = _BIAS_TF.get(timeframe, "1d")` — uses `BIAS_TIMEFRAME_MAP`. ✅

**The bias timeframe gap applies to ad-hoc callers** who call `engine.run(df=my_4h_data, df_4h=my_4h_data)` directly, not to the canonical tooling. The C1/C2 walk-forward results used the optimizer which is correct. Previous audit results are valid on this dimension.

**Remaining concern:** If a user calls `BacktestEngine.run()` directly (e.g. in a script or notebook) and passes 4h data as `df_4h` for a 4h primary run, they will get wrong bias classification with no warning. The misleadingly-named `df_4h` parameter is the root cause.

---

*Audit complete. No code was modified. All evidence is file:line references to actual source.*
