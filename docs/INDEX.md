# Documentation Index

Fast topic → file lookup. Use this before greping — most questions have a
canonical answer location. CLAUDE.md is intentionally slim and points here
for anything detailed.

---

## Where to find things

| You want to… | Go to |
|---|---|
| Understand validated parameters / why these defaults | `CLAUDE.md` → "Validated Baseline" |
| See the full data flow diagram (live cycle) | `CLAUDE.md` → "Architecture / Full Data Flow" |
| Know what each module owns | `CLAUDE.md` → "Module Map" |
| Look up a numbered gotcha (`gotcha #N` in code) | `docs/gotchas.md` (search `### N.`) |
| Add a new strategy | `docs/adding_strategies.md` |
| Audit live↔backtest fidelity | `docs/backtest_vs_live.md` |
| Read past walk-forward audit reports | `docs/audits/` |
| Read SDD design specs | `docs/superpowers/specs/` |

---

## By workflow

### Working on the live bot (`run_cycle` / orchestrator)

1. `CLAUDE.md` → "Validated Baseline" — what *can't* be changed without re-audit
2. `bot/orchestrator.py:step()` — the live entry pipeline
3. `docs/gotchas.md` → #4 (circuit breaker), #25 (Kelly), #30–#31 (HWM), #36–#37 (parity)

### Working on the backtest engine

1. `bot/backtest/engine.py` (single-symbol) or `portfolio_engine.py` (multi-symbol)
2. `docs/backtest_vs_live.md` — known gaps, **read before changing exit logic or sizing**
3. `docs/gotchas.md` → #1 (trailing), #21 (leverage+momentum), #24 (intra-bar wicks), #26 (filters propagation)
4. `tests/test_parity_runtime.py` — must stay green when changing the config builder

### Working on the dashboard backtest section

1. `dashboard/sections/backtest_runner.py` — thin Streamlit wrapper only
2. `bot/backtest/portfolio_runner.py` — the actual logic (pure, testable)
3. `tests/test_parity_runtime.py` — runtime guard against config drift
4. `tests/test_dashboard_parity.py` + `tests/test_dashboard_runner.py` — fast AST guards

### Working on tests

1. `tests/conftest.py` — shared OHLCV factories and DB fixtures (`uptrend`, `flat`, `seeded_db`, `tmp_db`)
2. Use `make_ohlcv(...)` not ad-hoc DataFrame construction — keeps test data consistent

### Debugging a live↔backtest discrepancy

1. **First**: `tests/test_parity_runtime.py` — run it. If it fails, your config builder drifted.
2. **Second**: `docs/backtest_vs_live.md` — read sections 1 and 2 to rule out intentional gaps.
3. **Third**: gotchas **#36** (key drift) and **#37** (warmup) — known repeat offenders.
4. **Last resort**: enable verbose logging on both paths and diff trade lists.

### Working on Telegram / commands

1. `CLAUDE.md` → "Telegram Integration"
2. `bot/telegram_notifier.py` (outbound) and `bot/telegram_commands.py` (inbound)
3. `docs/gotchas.md` → #11 (lazy DB reads), #12 (one-shot breaker notify), #29 (poll loop survives errors)

### Working on the optimizer / auto-optimizer

1. `bot/optimizer/walk_forward.py` (grid), `bot/optimizer/auto_optimizer.py` (daemon)
2. `CLAUDE.md` → "Walk-Forward Optimizer"
3. `docs/gotchas.md` → #18 (hot-reload), #19 (viability gates), #32 (audit configs spec-locked), #35 (Phase 2 results)

### Working on the database

1. `bot/database/db.py` — all queries
2. `docs/gotchas.md` → #5 (StrEnum equality), #6 (`_migrate_schema` is safe), #8 (single-connection in dashboard)

---

## File layout cheat sheet

```
bot/
  backtest/
    engine.py           ← single-symbol backtest
    portfolio_engine.py ← multi-symbol cash pool
    portfolio_runner.py ← pure dashboard runner (testable)
    runner.py           ← CLI runner (`python -m bot.backtest.runner`)
    cache.py            ← parquet OHLCV cache
  orchestrator.py       ← live pipeline (regime → signal → bias → risk)
  risk/
    manager.py          ← circuit breaker, position sizing
    kelly.py            ← Kelly fraction math (pure)
    drawdown_scaler.py  ← DD-aware risk scaler (disabled by default)
    vol_regime.py       ← volatility-regime gate (disabled by default)
  strategy/
    ema_crossover.py    ← the only live strategy
  database/db.py        ← SQLite + bot_config KV
  audit/                ← walk-forward audit framework

dashboard/
  app.py                ← Streamlit entry
  sections/
    backtest_runner.py  ← thin wrapper around bot.backtest.portfolio_runner

docs/
  gotchas.md            ← 37-entry catalog, referenced as `gotcha #N`
  backtest_vs_live.md   ← parity gaps (intentional vs simplifications)
  adding_strategies.md  ← strategy onboarding guide
  audits/               ← past walk-forward reports
  superpowers/specs/    ← SDD design specs

tests/
  conftest.py           ← shared fixtures
  test_parity_runtime.py ← live↔dashboard config parity (runtime)
  test_dashboard_parity.py ← AST-level parity guards
```

---

## Code → docs cross-reference

When source comments reference a doc, this is the authoritative list. Keep
this table aligned with reality if you ever move things.

| Code reference | Lives at |
|---|---|
| `gotcha #N` (any N from 1 to 37) | `docs/gotchas.md` → `### N.` anchor |
| "validated baseline" / "validated parameters" | `CLAUDE.md` → "Validated Baseline" |
| "audit spec" | `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md` |
| "the parity test" | `tests/test_parity_runtime.py` |
| "the AST parity guards" | `tests/test_dashboard_parity.py`, `tests/test_dashboard_runner.py` |
