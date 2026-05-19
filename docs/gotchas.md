# Gotchas Catalog

These WILL bite you if you don't know them. Indexed by number — code comments
across the repo reference gotchas as `gotcha #N`. Search this file by
`### N.` to jump to a specific entry.

This catalog used to live in CLAUDE.md but was moved here to keep CLAUDE.md
focused on orientation. Anchors and numbering are preserved exactly.

> Update protocol: when you fix a bug worth remembering or discover a non-obvious
> invariant, add a new entry at the bottom (next free number). Do NOT renumber
> existing entries — code comments depend on the numbers.

---

### 1. Trailing stop is OFF in the live bot — but `BacktestConfig` still has the fields

**Live**: trailing stop is removed from the live position manager. The
ratcheting block, `TRAILING_STOP` exit reason, and `trailing_stop_enabled`
config flag have been removed from `RiskConfig`, `position_manager()`, and
`bot/constants.py`. Live positions exit ONLY via SL or TP.

**Backtest**: `BacktestConfig` still has `trail_atr_mult` and
`trail_activation_mult` fields, plus a legacy `simulate_trailing` flag.
These exist so research scripts (`scripts/risk_scaling.py` and similar) can
still measure trailing-stop variants. **All production-relevant backtests
keep `simulate_trailing=False`.**

3-year legacy backtest with trail ON: PF=0.764, Ann=-5%. Only 1 of 131 trades
hit TP — trail cut every winner before target. Hence the live removal.

Legacy artifacts:
- `trades.trailing_sl` column in the SQLite schema — preserved for old rows.
  Never written by the live bot.
- `BacktestConfig.simulate_trailing` defaults to False.

Re-enabling in live would require re-introducing both the config flag AND the
`position_manager` logic, plus a fresh backtest demonstrating it adds value
(unlikely on EMA crossover BTC; the removal was data-driven).

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

### 4. Circuit breaker resets THREE ways — and SURVIVES restarts

The circuit breaker is NOT permanent. It resets if ANY condition is met:
- `cooldown_hours` (default 4h) have elapsed since trigger, OR
- drawdown recovers below `max_drawdown` threshold (15% default), OR
- Manual `/reset_hwm` Telegram command (clears all `breaker_triggered_at_*` rows atomically)

```python
# If drawdown recovers before cooldown expires:
if drawdown < self.config.max_drawdown:
    self._breaker_triggered_at = None  # immediate reset
    return False
```

**Breaker input is TRADING EQUITY (post-May 2026)**: `check_circuit_breaker` now receives
`trading_equity = account_baseline + SUM(closed_pnl)`, NOT raw exchange balance. This means
faucet deposits and withdrawals do NOT affect the breaker threshold. See gotcha #31.

**Persistence**: `_breaker_triggered_at` is saved to `bot_config` under the
key `breaker_triggered_at_{symbol}` on every state change. The orchestrator
passes `db=db` when constructing `RiskManager`, which restores the timestamp
on `__init__`. This means the cooldown **survives `init 6` reboots and
`docker compose restart`** — a breaker triggered at 2am with a 4h cooldown
will still be active at 3am after a 02:30 restart, instead of being silently
wiped by the in-memory reset.

**New `bot_config` keys added in May 2026**:
- `account_baseline` — USDT trading-start balance; back-computed once at first Phase-2 start; never auto-mutated
- `peak_capital` — now stores peak TRADING EQUITY (not raw balance); ratcheted by orchestrator each cycle

If `db` or `symbol` are not provided (e.g. ad-hoc test instances), the
manager falls back to in-memory state — backward compatible.

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

### 9. Opposite-signal exit is REMOVED — positions exit only via SL / TP / regime_change

Older versions of this codebase closed an open position when the strategy
emitted a strong opposite signal (`strength >= 0.75`, configurable via a
`RiskConfig.min_exit_signal_strength` field). Both the `min_exit_signal_strength`
field and the opposite-signal exit branch have been **removed**. Live positions
exit through:

1. **SL / TP** — the natural exit (gotcha #24 covers intra-bar wick detection).
2. **Liquidation** — only relevant when leverage > 1 (spot doesn't liquidate).
3. **Regime change** — opt-in via `RiskConfig.enable_regime_exit=True` (gotcha #15).
4. **End of period** — backtest-only.

If you want to re-introduce opposite-signal closing, it'll need a fresh backtest:
empirically, exiting on opposite signals shortens winning trades on
trend-following EMA strategies — same failure mode as the trailing stop.

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

### 17. `orchestrator.step()` third arg is `df_high`, not `df_4h`

The parameter was renamed from `df_4h` to `df_high` to reflect that it carries the
higher-timeframe candles for `BiasFilter` — which is **not always 4h** depending on
the primary timeframe:

```python
_BIAS_TF = {"1h": "4h", "2h": "4h", "4h": "1d", "8h": "1d", "1d": "1w"}
```

`main.py` fetches the correct bias timeframe based on `TIMEFRAME` setting and passes it
as `df_high`. The optimizer also follows this mapping.

### 18. Auto-optimizer hot-reloads EMA config without restart

The **auto-optimizer** (`bot/optimizer/auto_optimizer.py`) runs weekly in a daemon thread.
When it finds a better config it writes `ema_stop_mult` and `ema_tp_mult` to the `bot_config`
KV store AND hot-patches the live `EMACrossoverStrategy` object via `_apply_ema_config()`:

```python
# in main.py — on_applied callback
_apply_ema_config(db, orchestrator)   # hot-patches config.stop_atr_mult / config.tp_atr_mult
```

Changes take effect on the very next `run_cycle()` tick — no restart needed.
Manual approvals from the dashboard OPTIMIZER tab write to the DB but require a restart;
`_apply_ema_config()` is called once at startup to pick those up.

### 19. Optimizer viability constraints — all four must pass

`walk_forward.py` gates results before saving to DB. A config is `viable` only if:
- `total_trades >= 15`
- `max_drawdown_pct <= 20.0`
- `sharpe_ratio >= 0.4`
- `profit_factor >= 1.05`

Skipped R:R combos: any combo where `tp_mult / stop_mult < 1.5` is skipped outright
(minimum 1.5:1 risk-reward enforced). Results are sorted viable-first then by PF DESC.

### 20. Parquet cache lives at `data/klines/` — shared by backtest and optimizer

`bot/backtest/cache.py` stores OHLCV data as `data/klines/{SYMBOL}_{INTERVAL}.parquet`.
The cache is incremental: only missing bars are fetched. The directory is created
automatically on first use. Both `BacktestEngine` (via `fetch_and_cache`) and the optimizer
use this cache — running the backtest runner first populates the cache for the optimizer.
Thread-safe for reads; single-writer per file.

### 21. `BacktestEngine.run()` accepts `df_weekly` for momentum filter and `leverage` for futures simulation

`BacktestConfig` has three new field groups (all default to spot/no-filter behaviour):

```python
# Leverage (1.0 = spot, unchanged)
leverage: float = 1.0
funding_rate_per_8h: float = 0.0001   # BTC perp typical

# Weekly momentum filter (False = off, unchanged)
momentum_filter_enabled: bool = False
momentum_sma_period: int = 20          # 20-week SMA
momentum_neutral_band: float = 0.05   # ±5% around SMA
```

`run()` signature now includes `df_weekly: pd.DataFrame | None = None`. When `momentum_filter_enabled=True` and `df_weekly` is provided:
- Price > SMA × 1.05 → **BULLISH** → full risk
- Price < SMA × 0.95 → **BEARISH** → entry blocked (no new trades)
- Within band → **NEUTRAL** → risk halved

Liquidation price for BUY: `entry × (1 − 0.9 / leverage)`. Trades closed with `EXIT_LIQUIDATED`; loss = full margin. Filter only gates **new entries** — open positions are never force-closed by momentum state.

Use `ScenarioRunner` (not `BacktestEngine` directly) when comparing multiple leverage/momentum combinations — it handles data routing (1h→4h bias, 4h→1d bias) and computes annual return correctly.

### 22. Multi-symbol balance is split equitably across symbols in `run_cycle`

`run_cycle()` accepts `n_symbols: int` and divides the fetched USDT balance evenly:

```python
total_balance = client.get_balance("USDT")
balance = total_balance / max(1, n_symbols)
```

`run_all_cycles()` passes `n_symbols=len(orchestrators)` so each per-symbol cycle sees a
fair share of the pool. Without this, the first symbol in the loop would size positions
against 100% of capital, draining the pool and starving every symbol after it
(`-2010 insufficient balance` for the rest).

The split is **equitable, not weighted**. To customise per-symbol weights you would have
to plumb a weight map through `run_all_cycles()`. Currently each symbol gets `1/N`.

A side effect: `BTCUSDT` and `ETHUSDT` both see `total/2` even when one of them has no
open position. This trades capital efficiency for predictability — no symbol can blow up
the pool. If you only want this behaviour above N>1, the `if n_symbols > 1` log line lets
you spot the allocation in the cycle output.

### 23. `RiskManager.compute_position_size` caps quantity by available capital

Spot trading has no margin: notional cannot exceed cash. When `risk_per_trade / sl_distance_pct`
yields more notional than 100% of capital (e.g. 3% risk × 2.76% SL = 108% of capital), the
risk-based formula computes an impossible position. Binance rejects with `-2010`.

To prevent this, `compute_position_size()` takes the minimum of two formulas:

```python
qty_by_risk    = (capital * risk_fraction) / (entry - stop_loss)
qty_by_capital = (capital * 0.99) / entry          # 99% leaves margin for fees
quantity       = min(qty_by_risk, qty_by_capital)  # cap kicks in only if needed
```

When the cap activates, a WARNING log line fires:

```
[BTCUSDT] Qty capped by capital: risk-based=0.54352 → 0.49507
  (risk 3.00% × SL_dist 2.76% would need notional > 100% of capital)
```

The cap is a safety net — seeing it consistently means your `risk_per_trade` and
`stop_atr_mult` combination is too aggressive for spot. The fix is to widen the SL
(higher `ema_stop_mult`) or lower the risk per trade, not to ignore the warning.

### 24. `position_manager` uses intra-bar high/low (1m kline) — not just live_tick

Live SL/TP detection runs in two stages inside `_manage_single_position`:

1. **Primary** — `_check_intra_bar_exit()` fetches the last 2 1m klines via
   `BinanceClient.get_klines(symbol, "1m", limit=2)` and compares each bar's
   `high`/`low` against the trade's SL/TP. Captures intra-second wicks that
   `live_tick` (sampled from WS trade events) can miss.
2. **Fallback** — if the kline fetch fails or returns empty, falls back to
   `db.get_live_tick().price` and the original spot comparison.

When a wick is detected, the **exit price stored is the bar's `close`**, NOT
the SL/TP level. This is intentional: when we send a market close after the
detection, Binance fills at spot, not at the level. The bar close is the
honest proxy for what we'll actually fill at. Empirically validated — closing
at bar close beats "close at level" on max drawdown by ~24% on a 3-year run
(see `scripts/test_wick_variants.py`).

**Both-wicked tiebreaker**: when the same 1m bar wicks BOTH SL and TP
(rare but real during flash crashes that recover), the exit reason is
chosen by the close direction relative to entry — close on the winning
side → `TAKE_PROFIT`, otherwise → `STOP_LOSS`. The exit price is still
the bar close, so the reason now matches the financial outcome. The
backtest engine in `bot/backtest/engine.py` keeps the legacy "SL wins"
conservative rule — there is no real fill price to anchor to in
simulation, so the pessimistic assumption stays statistically correct.

`position_manager` builds the BinanceClient once per cycle (only when there
are open trades), passes it to all `_manage_single_position` calls. Adds 1
unauthenticated REST call per open trade per 60s — trivial against Binance's
1200-weight/min limit.

### 25. Kelly sizing is wired in `orchestrator.step()` — half-Kelly with clamps

The orchestrator computes a per-strategy Kelly fraction from closed-trade history
before sizing each entry. Lives in `bot/risk/kelly.py` (`compute_kelly_fraction`,
`kelly_risk_fraction`) and is invoked at `bot/orchestrator.py:141-184`.

Mechanism:
1. Pull stats with `db.get_kelly_stats(strategy.name, kelly_min_trades=15)`. Returns
   `None` when fewer than 15 closed trades exist for that strategy — sizing then
   falls back to flat `RiskConfig.risk_per_trade`.
2. `compute_kelly_fraction(win_rate, avg_win_pct, avg_loss_pct, half=True)` —
   half-Kelly by default, floored at 0 for negative-edge strategies.
3. `kelly_risk_fraction(...)` clamps the dynamic risk between `kelly_min_mult=0.25`
   and `kelly_max_mult=2.0` of the base `risk_per_trade`. Signal strength scales
   the Kelly multiplier: `mult = (kelly_f / base_risk) * signal_strength`.

`RiskConfig` fields controlling this: `kelly_max_mult`, `kelly_min_mult`,
`kelly_min_trades`, `kelly_half`. Defaults are conservative (half-Kelly,
0.25×–2× clamp). Don't crank `kelly_max_mult` past 2.0 without a fresh backtest:
full Kelly is brutal under estimation error and tends to blow up on drawdowns
that exceed the historical sample.

### 26. `dd_risk` (drawdown scaler) and `vol_regime` apply in BOTH backtest engines

`BacktestEngine` (single-symbol) and `PortfolioBacktestEngine` (multi-symbol cash
pool) both apply `drawdown_multiplier()` and the `vol_regime` size factor to
`effective_risk` at entry time. The portfolio engine had a bug (May 2026) where
both filters were silently bypassed in its duplicated sizing path — a 15-config
risk × scaler matrix produced identical metrics across OFF/Conservative/Moderate
columns, surfacing the bypass. Fix: tracked `peak_capital` HWM and propagated
both factors into the entry block. Test `test_drawdown_scaler_invoked_in_portfolio_sizing`
in `tests/test_portfolio_engine.py` is a regression guard.

When duplicating a sizing path between engines, ALL filters that affect
`effective_risk` must be re-applied — `momentum_state` halving, `vol_size_factor`,
and `drawdown_multiplier`. Identical-metrics-across-configs is the telltale of a
bypass.

### 27. `_execute_order` retries DB writes after exchange fills — orphan alerts on persistent failure

`main._execute_order` is not transactional across Binance + SQLite. To prevent
orphans (filled on exchange but missing from DB), DB writes after a successful
order go through `_retry_db_write` (3 attempts, exponential backoff starting at
0.5s). On final failure `_alert_orphan_position` logs CRITICAL and sends a
Telegram alert with the exchange `orderId` and trade details — the bot does NOT
attempt to undo the order (compounding risk). Manual reconciliation required.

This means the bot can leave inconsistent state if SQLite is broken for
extended periods. The alert is the safety net, not a fix.

### 28. `_retry` decorator only retries network/Binance exceptions

`bot/exchange/binance_client.py:_retry` previously caught `Exception` (everything),
which meant a programming bug like a `KeyError` would burn ~14s on three retries
before propagating. Now narrowed to `(BinanceAPIException, BinanceRequestException,
requests.exceptions.RequestException, TimeoutError, ConnectionError)`. Programming
bugs surface immediately.

### 29. Telegram poll loop survives transient errors

`TelegramCommandHandler._poll_loop` wraps both the per-update handler and the
top-level loop body in `try/except`. Per-update errors log and skip; loop-level
errors back off 10s and continue. Without this, a malformed update or a brief
DB hiccup would kill the daemon thread silently and `/pause`, `/status`,
`/report` would stop responding with no signal to the user.

### 30. HWM is account-level (cross-symbol) — BTC losses pause ETH trading

There is ONE shared `peak_capital` value for the entire account (all symbols). A single
`trading_equity = account_baseline + db.get_closed_pnl_sum()` aggregates PnL across ALL
symbols with no filter. This means:
- If BTC closes a losing trade that crosses 15% drawdown, ETH's `step()` also sees that
  drawdown and returns `[]` (no trades) — even if ETH itself was profitable.
- If ETH recovers the drawdown, the breaker clears for BTC too on next cycle.

This is **intentional** — capital is fungible. The breaker protects the ACCOUNT, not
individual symbol performance. Per-symbol HWM was rejected as unnecessarily complex.

Implementation: `db.get_closed_pnl_sum()` has no symbol filter by default. Both orchestrators
(BTCUSDT and ETHUSDT) call it and get the same cross-symbol total. See `test_cross_symbol_hwm_shared`
in `tests/test_orchestrator_hwm.py`.

### 31. `peak_capital` key semantic changed in May 2026

**Pre-May 2026**: `peak_capital` in `bot_config` stored the peak of the **raw USDT exchange balance**.
This was deposit-prone — a testnet faucet drop of +30k USDT would ratchet the peak to 39277+,
making the circuit breaker permanently stuck (no real trading loss could ever recover from that HWM).

**Post-May 2026**: `peak_capital` stores the peak of **TRADING EQUITY** = `account_baseline + SUM(closed_pnl)`.
The raw balance is no longer the breaker's input. The KEY NAME was kept (`peak_capital`) for API
stability across `get_peak_capital()`, `set_peak_capital()`, and every test that passes a plain
float — callers don't need to change.

**Migration**: at first Phase-2 startup, `_init_account_baseline(db, client)` in `main.py` detects
`account_baseline IS NULL` and runs a one-shot seed:
1. Fetches current USDT balance from Binance
2. Computes `baseline = balance - SUM(pnl_all_closed_trades)`
3. Sets `account_baseline` and resets `peak_capital` to current trading equity

After that, every `orchestrator.step()` re-computes `trading_equity` from DB (no in-memory cache)
and ratchets `peak_capital` if it's higher. Faucet drops have zero effect.

If the back-computed baseline is wrong (e.g. testnet faucet history polluted), use `/reset_hwm`
to clear the peak, then manually DB-poke `account_baseline` to the correct value.

### 32. Audit configs are SPEC-LOCKED — never tweak mid-run

`scripts/audit/run_walk_forward.py` hardcodes `CONFIG_C1_BASELINE`, `CONFIG_C2_PROD`, and
(as of Phase 1 May 2026) `CONFIG_C3_LIVE`. These mirror the audit spec
(`docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md` section 5) and MUST NOT
drift from production silently — that would invalidate prior reports. If you need to test a
different config, add a new constant and a new CLI flag, never mutate C1/C2/C3 in place. The
verdict thresholds in `bot/audit/verdict.py` follow the same lock.

The walk-forward audit (May 2026, `docs/audits/A_walk_forward_2026-05-14.md`) revealed that
C2 (production: 3% risk, SL=1.25×ATR, TP=3.5×ATR) yields **NO-GO** verdict — max DD of 45%
across all 10 quarterly windows, 29.6% above the 35% safety ceiling — while C1 (baseline:
1.5% risk, SL=1.5×ATR, TP=4.5×ATR) is rock-solid **GO** with DD=14% in every window,
PF=1.46 mean, Calmar=16.4. Paired t-test confirms C1 dominates with p < 0.0001, Cohen's
d = 3.62 (huge effect). The auto-optimizer's evolution (1.5/4.5 → 1.25/3.5 + 3% risk) is
degrading risk-adjusted return.

**Phase 1 (May 2026) — C3_LIVE audit**: `CONFIG_C3_LIVE` (1.5% risk, SL=1.5×ATR, TP=5.0×ATR,
0.08 momentum band, bar_dir=True, ema_momentum=True) yields **GO** verdict across 10 windows:
PF mean=1.383, Calmar mean=13.52, max DD=24.56%, WR mean=36.3%. See
`docs/audits/A_walk_forward_2026-05-17.md`. C3_LIVE is now the baseline for Phase 2
champion-challenger testing (ADX gate + EMA200 alignment).

**Key difference between C1/C2 and C3**: C1 and C2 use `momentum_neutral_band=0.05` (spec-locked
to reproduce historical reports). C3_LIVE uses `0.08` which matches the live `MomentumFilter`
configuration. The `BacktestConfig.momentum_neutral_band` default was changed from 0.05 → 0.08
in Phase 1 to reflect live parity. C1 and C2 pass `0.05` explicitly to preserve reproducibility.

### 33. `MomentumFilterConfig.neutral_band` — promoted from module constant, was 0.08 all along

In Phase 1 (win-rate-uplift-2026-05), `bot/momentum/filter.py` promoted `NEUTRAL_BAND = 0.08`
(module constant) to `MomentumFilterConfig(neutral_band=0.08)` (dataclass). This makes the
band configurable per caller — audit scripts can now pass `MomentumFilterConfig(neutral_band=0.05)`
to reproduce C1/C2 results without changing the live default.

The live bot's `MomentumFilter.get_state()` behaviour is UNCHANGED — the default `MomentumFilterConfig()`
still produces 0.08. The signature change is backward-compatible: old callers with positional
`(df_weekly, current_price)` still work.

The `BacktestConfig.momentum_neutral_band` default was aligned from `0.05` → `0.08` simultaneously.
This WAS a behaviour-affecting change for any code relying on the BacktestConfig default — but audit
C1/C2 explicitly pass `0.05` and are protected by regression tests.

### 34. Legacy C2 DB values — detect-and-warn, NOT auto-correct

If a running bot was previously seeded with C2 config values (`risk_per_trade=0.03`,
`ema_stop_mult=1.25`, `ema_tp_mult=3.5`, `ema_vol_mult=2.0`, `ema_bar_dir=false`,
`momentum_neutral_band=0.05`), Phase 1's `_seed_optimized_defaults()` will detect them and
emit a WARNING log on startup. It will NOT auto-correct these values (design D11: the user
may have intentionally set them for paper testing).

To apply the recommended Phase 1 values, run:

```bash
PYTHONPATH=. python scripts/migrate_to_b_pick.py
```

This script is interactive, shows old/new values, and requires explicit confirmation. Use
`--dry-run` to preview without writing. Run it while the bot is stopped to avoid a race
condition with live cycles.

The legacy-value detection covers: `risk_per_trade`, `ema_stop_mult`, `ema_tp_mult`,
`ema_vol_mult`, `ema_bar_dir`, `momentum_neutral_band`. Values defined in `_LEGACY_OVERRIDES`
in `main.py`.

### 35. Phase 2 entry filters — shipped dark; champion-challenger verdict INCONCLUSIVE

**Phase 2 (win-rate-uplift-2026-05)** adds two optional entry-quality gates to
`EMACrossoverStrategy`. Both default to OFF and have no live-behaviour impact:

| Config field | Default | Effect |
|---|---|---|
| `EMACrossoverConfig.min_entry_adx` | `0.0` (disabled) | Gate continuation entries when ADX < threshold; crossovers always pass |
| `EMACrossoverConfig.require_ema200_alignment` | `False` | BUY blocked if close < EMA200; fail-open when < 200 bars |

**Architecture**: ADX gate applies ONLY to trend-continuation entries (`in_trend_buy` /
`in_trend_sell`). Fresh crossovers are never gated — this is intentional (design D1).
EMA200 is long-only asymmetric (BUY-only); SELL direction is unaffected.

**Parity contract**: Both filters propagate through the full stack:
- Live orchestrator (`bot/orchestrator.py` → `EMACrossoverStrategy`)
- `BacktestEngine` (`bot/backtest/engine.py` — `BacktestConfig.ema_min_entry_adx`, `ema_require_ema200_alignment`)
- `PortfolioBacktestEngine` automatically via `BacktestEngine` constructor
- `_apply_ema_config()` in `main.py` — hot-patch path for DB-driven updates

Seeds in `_seed_optimized_defaults()`: `ema_min_entry_adx="0.0"`, `ema_require_ema200="false"`.
4h preset in `bot/config_presets.py` reflects the same OFF defaults.

**Helper function**: `adx_last(df, period) -> float` extracted to `bot/indicators/utils.py`
(pure function, bit-identical with `RegimeDetector._adx()` which now delegates to it).

**Champion-challenger audit (May 2026, `docs/audits/CC_2026-05-17_PHASE2_SUMMARY.md`)**:
4 challengers tested against C3_LIVE (Calmar=13.52) over 10 quarterly windows (2022-04 → 2026-05):

| Challenger | Verdict | Calmar | Cohen's d | Wins/10 |
|---|---|---|---|---|
| C3_ADX25 (min_entry_adx=25) | **REJECT** | 3.53 | -2.19 | 0 |
| C3_ADX30 (min_entry_adx=30) | **REJECT** | 8.03 | -1.21 | 1 |
| C3_EMA200 (require_ema200=True) | **INCONCLUSIVE** | 13.28 | -0.35 | 4 |
| C3_BOTH (ADX=25 + EMA200) | **REJECT** | 3.58 | -2.20 | 0 |

**Overall verdict: INCONCLUSIVE** — no filter earned ADOPT.

**Interpretation**:
- ADX gates (25/30) are catastrophically damaging: filtering by trend strength
  removes exactly the continuation entries that carry the most alpha on EMA crossover
  in 4h BTC/ETH. Calmar collapses from 13.52 to 3-8, with 0-1 windows won out of 10.
- EMA200 alignment is statistically neutral (p=0.29, d=-0.35, 4 wins vs 3 losses).
  Not harmful, but not a proven improvement either. Trade count drops from ~960 to ~912
  (fewer entries, same quality).
- Combined (C3_BOTH): ADX damage dominates; EMA200 cannot rescue.

**Recommendation**: Keep all Phase 2 filters OFF. Do not enable `min_entry_adx` or
`require_ema200_alignment` in production. The code ships as-is for future testing
with different thresholds or longer evaluation windows. EMA200 alone may be worth
revisiting after accumulating more post-2025 data (the EMA200 signal tends to become
more relevant in extended bear regimes not well-represented in the 2022-2026 training set).

### 36. Phase 1 parity follow-up — `ema_min_atr` was missing, `ema_momentum` key typo

The original Phase 1 of `win-rate-uplift-2026-05` shipped with two parity holes that
caused the dashboard to diverge from live by **~45% more trades**:

1. **`ema_min_atr` not seeded**. Live 4h preset has `min_atr_pct=0.005` (filter dead
   markets) but the dashboard fallback in `backtest_runner.py` was `0.0`. Effect: the
   dashboard accepted entries in low-volatility chop that live rejected.
2. **`ema_momentum_req` seed key vs `ema_momentum` consumer key**. Seed wrote
   `ema_momentum_req`, but `_apply_ema_config()` (live hot-patch) and
   `backtest_runner.py` (dashboard) both read `ema_momentum`. The live hot-patch
   for `require_ema_momentum` never fired since Phase 1 (fallback default kept it
   correct by accident). The dashboard was also reading the wrong key but with the
   right fallback.

**Fix (2026-05-17)**:
- `_seed_optimized_defaults()` now seeds `ema_min_atr="0.005"`.
- `_apply_ema_config()` and `backtest_runner.py` now read `ema_momentum_req` (matching seed).
- Dashboard `ema_min_atr` fallback is `0.005` (matching preset).
- `tests/test_dashboard_parity.py::KEYS_TO_CHECK` extended with both keys to guard
  against future drift via AST inspection.

**Impact on user-reported PF=0.90**: was a measurement artifact of bug #1.
Post-fix dashboard reproduces live (BTC 6mo: PF=1.79 / WR=36% / 11 trades / DD=2.3% /
PnL=+5.93%) instead of the pre-fix dashboard (PF=0.90 / WR=25% / 16 trades / DD=6.5% /
PnL=-1.5%). Verifier: `scripts/audit/verify_dashboard_fix.py`. Live behaviour did not
change — the live preset always had `min_atr_pct=0.005` and `require_ema_momentum=True`.

### 37. Phase 1 parity follow-up #2 — dashboard fetches must include filter warmup

`dashboard/sections/backtest_runner.py:_run_portfolio_backtest` previously called
`fetch_and_cache(sym, bias_tf, start_dt, end_dt)` and `fetch_and_cache(sym, "1w", start_dt, end_dt)`
with no warmup buffer. Since `bot/backtest/cache.py:176-177` strictly filters the
returned dataframe to `[start_ts, end_ts]`, the higher-timeframe filters arrived
at backtest_start with insufficient history:

- **BiasFilter** needs `slow_period+1 = 22` daily bars → fell into the
  `passthrough` branch (NEUTRAL doesn't block) for the first ~3 weeks of the
  backtest period (gotcha #14).
- **MomentumFilter** needs `sma_period+1 = 21` weekly bars → fell into the
  fail-open `BULLISH` branch (`bot/momentum/filter.py:49-50`) for the first
  ~5 months (a 6mo backtest has only ~26 weekly bars).

Net effect: both filters were effectively disabled for a large portion of the
backtest period, letting in low-quality entries the live bot rejects. This was
distinct from gotcha #36 but produced the **same external symptom** (~45% extra
trades on BTC 6mo, PF=0.90), which is why fixing #36 alone didn't resolve the
user-reported divergence.

**Measurement (BTC 6mo, identical config):**
- WITH warmup (live-parity): PF=1.79, WR=36.4%, 11 trades, PnL=+5.93%
- WITHOUT warmup (dashboard pre-fix): PF=0.90, WR=25.0%, 16 trades, PnL=-1.48%

**Fix (2026-05-17)**: added `BIAS_WARMUP = timedelta(days=30)` and
`MOMENTUM_WARMUP = timedelta(days=154)` (21 weeks + 1 week safety) in
`_run_portfolio_backtest`. Both filter fetches now pass `start_dt - <warmup>`
instead of raw `start_dt`. Primary timeframe fetch is unchanged — `BacktestEngine`
handles its own ATR/EMA warmup internally. Regression guards in
`tests/test_dashboard_parity.py::TestDashboardFilterWarmup` (3 AST checks).

**Live behaviour did NOT change**: `main.run_cycle()` fetches the last N bars
fresh each cycle (e.g. `get_klines(symbol, "4h", limit=200)`), so the live bot
always has 200 bars of warmup baked in. Only the dashboard backtest path was
affected — it requested a precise `[start_dt, end_dt]` window without thinking
about filter warmup.

The fix was later refactored: `_run_portfolio_backtest` is now a thin Streamlit
wrapper around `bot/backtest/portfolio_runner.run_portfolio_backtest_core`,
which owns the warmup constants and is testable without Streamlit. See
`tests/test_parity_runtime.py::TestFetchPlanWarmup` for the runtime guards.
