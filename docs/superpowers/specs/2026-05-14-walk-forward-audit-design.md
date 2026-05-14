# Walk-Forward Validation Audit — Sub-Project A

**Date**: 2026-05-14
**Status**: Design (pending implementation plan)
**Parent project**: Full system audit (4 sub-projects A/B/C/D)
**Owner**: Jordi
**Test runner**: `.venv/bin/pytest tests/ -q` (strict TDD active)

---

## 1. Context

The trading bot ships with a Validated Baseline in `CLAUDE.md` claiming PF 1.52, +32.7% annual, -15.2% max DD on BTC+ETH 4h over 3 years at 1.5% risk. Production now runs an evolved config (3% risk, SL=1.25/TP=3.5, auto-optimized entry-quality filters) and shows mixed live signals — only 1 closed trade in the visible production history, one anomalous trade classification, week-long circuit-breaker blockage, and a 6-month ad-hoc backtest showing PF 1.30 (vs baseline 1.52) with ETH at PF 0.98.

Three concerns motivate a formal walk-forward audit:

1. Is the historical edge **stable out of sample**, or was the baseline a lucky in-sample artifact?
2. Is the current production config **better than baseline**, or is the auto-optimizer overfitting recent noise?
3. Per-symbol, does the edge **generalize to ETH** or only to BTC?

This sub-project answers all three with predetermined success criteria to avoid post-hoc rationalization (p-hacking).

## 2. Goal

Produce a **GO / WATCH / NO-GO verdict** per config, plus a **comparative verdict** (C1 vs C2), backed by per-window statistics across a rolling walk-forward over the full available cache (and extending the cache if needed).

The audit is **read-only on production logic**. No changes to `bot/`, `dashboard/`, or `main.py`. The deliverable is data + a markdown report + reusable audit infrastructure for sub-projects B and D.

## 3. Non-goals

- ETH-specific deep dive (sub-project B) — we will report per-symbol metrics but not analyze why.
- Code quality audit (sub-project C).
- Capital allocation / Kelly / drawdown scaler analysis (sub-project D).
- Dashboard UI for this audit. CLI is sufficient.
- CI/CD for recurring audits. Manual re-run via CLI.
- Multi-fee sensitivity analysis. Single fee (production setting).
- Universe expansion beyond BTC + ETH.
- Live trading impact. Audit does not touch the live bot.

## 4. Success criteria (predetermined — cannot be moved after running)

### 4.1 Per-config verdict thresholds

| Bucket | Conditions (ALL must hold) |
|---|---|
| **GO** | mean PF ≥ 1.20 across windows AND ≥ 65% of windows have PF > 1.0 AND mean Calmar ≥ 1.5 AND no window has max DD > 35% AND ≤ 20% of windows have < 5 trades |
| **WATCH** | Any condition fails by ≤ 10% margin |
| **NO-GO** | Any condition fails by > 10% margin |

Thresholds are 0.8× the baseline numbers from `CLAUDE.md` (PF 1.52 × 0.8 ≈ 1.20; Calmar 2.16 × 0.8 ≈ 1.5), accounting for expected out-of-sample decay.

### 4.2 Comparative verdict (C1 vs C2)

| Outcome | Conditions |
|---|---|
| **Prefer C2 (prod actual)** | Δ mean PF ≥ 0.10 (practical significance) AND paired t-test p < 0.05 AND Cohen's d > 0.3 |
| **Equivalent** | Anything else within paired t-test p > 0.10 |
| **Prefer C1 (baseline)** | Δ mean PF ≥ 0.10 in C1's favor AND paired t-test p < 0.05 AND Cohen's d > 0.3 |

A WATCH/NO-GO config does not invalidate the comparison — both configs can be NO-GO and we still report which is "less bad."

## 5. Configs under test (frozen)

| Field | **C1: Baseline** | **C2: Prod actual** |
|---|---|---|
| `risk_per_trade` | 0.015 | 0.03 |
| `ema_stop_mult` | 1.5 | 1.25 |
| `ema_tp_mult` | 4.5 | 3.5 |
| `ema_max_distance_atr` | 1.0 | 1.0 |
| `ema_volume_mult` | None (engine default) | 2.0 |
| `ema_require_momentum` | False | True |
| `ema_require_bar_dir` | False | False |
| `ema_min_atr_pct` | 0.0 | 0.0 |
| `long_only` | True | True |
| `momentum_filter_enabled` | True | True |
| `momentum_sma_period` | 20 | 20 |
| `momentum_neutral_band` | 0.05 | 0.05 |
| `bias_filter` | enabled (1d EMA9/21) | enabled (1d EMA9/21) |
| `cost_per_side_pct` | 0.001 (0.10%) | 0.001 (0.10%) |

Both run via `PortfolioBacktestEngine` with BTC + ETH shared capital pool, initial capital $10,000.

The values above are this spec's source of truth. Any deviation between live `bot_config` and these constants is intentional — the audit tests historical edges, not the moving target of production drift.

## 6. Walk-forward methodology

### 6.1 Window structure

| Setting | Value | Rationale |
|---|---|---|
| Train period | 18 months | Indicator warm-up + 2-3 market regimes |
| Test period | 3 months | Forces ≥7 test windows over 3.5y of cache |
| Step | 3 months (non-overlapping) | Independent test samples → valid paired t-test |
| Window mode | Rolling (not expanding) | Tests if the edge survives drift, not just accumulation |

### 6.2 No retraining within audit

Both C1 and C2 use **fixed parameters** across all windows. Train period is only used as indicator warm-up. The auto-optimizer's re-fitting behavior is **out of scope here** and will be analyzed in sub-project D.

### 6.3 Data window

- Use existing parquet cache at `data/klines/`: 2022-04 to 2026-05 (≈49 months)
- After 18mo train + 3mo step, expect ≈ 10 test windows (depending on edge alignment)
- If fewer than 7 test windows fit cleanly, extend cache by fetching back to 2020-04 (no Binance API limit issues for BTC/ETH 4h)
- Decision rule: if 7 windows produce a clear verdict, stop. If verdict is WATCH or marginal, extend cache and re-run.

### 6.4 Metric collection per window

For each (window, config) cell, collect:

- `pf` (profit factor)
- `calmar` (annual return / max DD)
- `sharpe` (annualized)
- `win_rate_pct`
- `max_drawdown_pct`
- `total_trades`
- `final_pnl_pct`
- `bt_pnl_btc` and `bt_pnl_eth` (per-symbol breakdown)
- `bt_trades_btc` and `bt_trades_eth`

### 6.5 Aggregate statistics

Across all windows for each config:

- mean, median, std of each metric
- 95% bootstrap confidence interval for mean PF (10,000 resamples)
- hit rate: % of windows with PF > 1.0
- tail: worst-window PF, worst-window DD
- distribution diagnostic: count of windows with < 5 trades (sparsity flag)

### 6.6 Comparison statistics (C1 vs C2)

- Paired t-test on per-window PF (alternative two-sided)
- Cohen's d effect size on PF
- Same pair of tests on Calmar as secondary metric

## 7. Architecture

### 7.1 Module layout

```
bot/audit/
├── __init__.py
├── walk_forward.py        # Window splitter + runner + aggregator
├── metrics.py             # Per-window metric extraction
├── verdict.py             # Threshold evaluation per config
└── comparison.py          # Paired t-test, Cohen's d

scripts/audit/
└── run_walk_forward.py    # CLI entry point

tests/                              # flat, project convention
├── test_walk_forward_splitter.py
├── test_walk_forward_aggregator.py
├── test_walk_forward_verdict.py
└── test_walk_forward_comparison.py
```

### 7.2 Reuse boundary

The audit reuses (does NOT reimplement):
- `bot.backtest.engine.BacktestConfig`
- `bot.backtest.portfolio_engine.PortfolioBacktestEngine`
- `bot.backtest.cache.fetch_and_cache`

The audit does NOT touch:
- `bot/orchestrator.py`
- `bot/risk/manager.py`
- `dashboard/`
- `main.py`

### 7.3 Public API of `bot/audit/walk_forward.py`

```python
@dataclass(frozen=True)
class WalkForwardConfig:
    start_date: datetime
    end_date: datetime
    train_months: int       # 18
    test_months: int        # 3
    step_months: int        # 3
    symbols: tuple[str, ...]  # ("BTCUSDT", "ETHUSDT")
    timeframe: str          # "4h"

@dataclass(frozen=True)
class Window:
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    index: int

@dataclass(frozen=True)
class WindowResult:
    window: Window
    config_name: str        # "C1" or "C2"
    backtest_config: BacktestConfig
    pf: float
    calmar: float
    sharpe: float
    win_rate_pct: float
    max_drawdown_pct: float
    total_trades: int
    final_pnl_pct: float
    per_symbol: dict[str, dict]  # {"BTCUSDT": {...}, "ETHUSDT": {...}}

def split_windows(cfg: WalkForwardConfig) -> list[Window]: ...

def run_window(
    window: Window,
    backtest_config: BacktestConfig,
    dfs: dict, dfs_bias: dict, dfs_weekly: dict,
    config_name: str,
) -> WindowResult: ...

def run_all(
    cfg: WalkForwardConfig,
    backtest_configs: dict[str, BacktestConfig],  # {"C1": ..., "C2": ...}
) -> list[WindowResult]: ...
```

### 7.4 CLI

```bash
# Default run (uses spec configs)
python -m scripts.audit.run_walk_forward

# Custom window count
python -m scripts.audit.run_walk_forward --train-months 12 --test-months 6 --step-months 6

# Single config only (debugging)
python -m scripts.audit.run_walk_forward --only C1
```

Outputs:
- `data/audits/A_walk_forward_<ISO>.json` (raw)
- `docs/audits/A_walk_forward_<date>.md` (report)
- Plotly HTML charts under `docs/audits/A_walk_forward_<date>_charts/`

## 8. Deliverables

### 8.1 Raw data

`data/audits/A_walk_forward_<ISO>.json` — a list of WindowResult dicts, one per (window, config). Schema documented in the report.

### 8.2 Markdown report

`docs/audits/A_walk_forward_<date>.md` with sections:

1. **TL;DR** (1 paragraph): verdicts + key numbers
2. **Methodology summary**: window setup, configs, success criteria (linked to this spec)
3. **Aggregate metrics table**: C1 vs C2, mean / median / std / 95% CI / hit rate
4. **Per-window detail table**: every (window, config) row with all metrics
5. **Verdict per config**: GO / WATCH / NO-GO with threshold-by-threshold breakdown
6. **Comparative verdict**: t-test + Cohen's d + practical significance
7. **Per-symbol breakdown**: BTC and ETH PF / Calmar per window (input for sub-project B)
8. **Charts**: equity curves overlaid per config, PF over time, DD over time
9. **Discussion**: regime alignment, sparsity, survivorship caveat
10. **Recommendations**: actionable next steps per outcome
11. **Appendix**: raw JSON path, command to reproduce, audit-suite version (git SHA)

### 8.3 Code

Reusable `bot/audit/` package + CLI + tests. Designed for sub-projects B (will add per-symbol Monte Carlo to compare) and D (will add config-grid sweep) to plug in.

## 9. Testing strategy (TDD strict)

### 9.1 Unit tests per module

`test_walk_forward_splitter.py`:
- Happy path: 49-month range → expected N windows
- Edge: data < window size → empty list
- Edge: exact window boundary alignment
- Edge: step ≠ test_months (overlap mode disabled here but parameter exists)
- Property: no two test windows overlap when step == test_months
- Property: train always precedes test

`test_walk_forward_aggregator.py`:
- Mean / median / std on mock results
- Hit rate calculation
- Bootstrap CI computation (deterministic seed for test)
- NaN / inf handling (PF can be inf if no losses)

`test_walk_forward_verdict.py`:
- Each branch GO / WATCH / NO-GO triggered by minimal mock input
- Threshold boundary tests (PF = 1.20 exact → GO; 1.19 → WATCH; 1.07 → NO-GO)

`test_walk_forward_comparison.py`:
- Paired t-test on known synthetic distributions
- Cohen's d directional sign correct
- Effect size magnitude on known means/stds

### 9.2 Integration test

Synthetic OHLCV (deterministic random walk seeded) → run 2 windows × 2 configs → verify JSON output structure → verify report Markdown sections exist.

### 9.3 Out-of-scope tests

- Performance benchmarking (audit runs offline; runtime is informational)
- Real-data acceptance tests (would couple tests to live cache state; acceptance is the audit itself)

## 10. Anti-overfitting safeguards

1. **Frozen configs**: this spec's section 5 is the source of truth. Any deviation requires a spec revision.
2. **Frozen success criteria**: section 4 is also locked. No moving thresholds after seeing results.
3. **Deterministic backtests**: same config + same data → same metrics. No random seeds in scope.
4. **Automated metric extraction**: report is generated from raw JSON; no manual cherry-picking.
5. **Audit-suite version pinning**: every report includes git SHA of `bot/audit/` at run time.
6. **Independent windows**: step == test_months → no overlap → paired t-test is valid.

## 11. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Survivorship bias (BTC/ETH only) | Certain | Moderate | Document explicitly; expand universe in a future audit |
| Regime asymmetry across windows | Certain | Low | Feature of walk-forward, not bug; reported per-window |
| Cache data quality issues | Low | High | Cache already validated in prior backtests; integrity check on JSON output |
| Runtime > 1 hour | Low | Low | Reuses cached parquet; portfolio engine is fast |
| Spec-impl drift during build | Medium | High | TDD enforces test-first; spec referenced in module docstrings |
| ETH momentum-filter sparsity (few trades per window) | Medium | Medium | Flagged by sparsity check; window discarded if < 5 trades |

## 12. Resource estimate

- Implementation: 2-3 hours TDD-strict
- Audit execution: 20-40 minutes (depending on extending cache)
- Report generation: < 1 minute (templated)

## 13. Definition of Done

- All TDD tests green
- CLI runnable end-to-end on real cache
- Report committed under `docs/audits/` (per Jordi's workflow — Jordi commits)
- Raw JSON persisted under `data/audits/`
- Verdict communicated in TL;DR
- Plotly charts render
- Audit-suite git SHA recorded in report

## 14. Open questions deferred

- **Auto-optimizer interaction**: out of scope here. If C2 wins big, that validates the optimizer; if C1 wins, it suggests overfitting. The follow-up is sub-project D.
- **Position sizing variants**: this audit assumes per-trade fixed risk %. Kelly variants and dd-scaler are sub-project D.
- **Cross-validation against `optimizer_runs` pending proposals**: see if any pending optimizer config from `bot_config` is a Pareto-improvement over both C1 and C2. Deferred.

## 15. Sub-project A roadmap

| Phase | Owner | Output |
|---|---|---|
| Design (this spec) | Brainstorm | ✅ This document |
| Implementation plan | writing-plans skill | Detailed task list |
| Implementation | sdd-apply or manual | `bot/audit/` package + tests |
| Run audit | Jordi via CLI | Raw JSON + report |
| Decision | Jordi | Proceed to B/D or pivot |

---

## Appendix A — Source of values

- `CLAUDE.md` Validated Baseline section: baseline params + 3-year metrics
- `bot_config` snapshot 2026-05-14: production runtime params (C2 source)
- `bot/backtest/engine.py:BacktestConfig`: dataclass defaults (cross-checked)
- `scripts/risk_scaler_matrix.py`: prior walk-forward-style methodology reference

## Appendix B — Glossary

- **PF (Profit Factor)**: gross_profit / gross_loss. PF > 1 is profitable.
- **Calmar**: annualized_return / max_drawdown_abs. Survival-vs-return ratio.
- **Sharpe (annualized)**: mean_returns / std_returns × √252 (or √sqrt(periods_per_year)).
- **Cohen's d**: standardized mean difference between two paired samples.
- **Hit rate**: % of windows where PF > 1 (or any chosen threshold).
- **Tail risk**: worst-window's PF and DD as standalone metrics.
