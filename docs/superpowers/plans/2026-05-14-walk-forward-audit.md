# Walk-Forward Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable walk-forward validation framework, run it on BTC+ETH with two configs (baseline C1 vs production C2), and produce a GO/WATCH/NO-GO verdict report.

**Architecture:** New `bot/audit/` package + `scripts/audit/` CLI. Reuses `PortfolioBacktestEngine` and `fetch_and_cache`. Read-only on production code — no edits to `bot/orchestrator.py`, `bot/risk/manager.py`, `dashboard/`, or `main.py`. Tests live flat under `tests/` (project convention).

**Tech Stack:** Python 3.14.4, pytest, pandas 2.2.2, numpy 1.26.4 (already in `requirements.txt`). NO new dependencies — paired t-test and Cohen's d implemented in pure numpy. Plotly charts saved as standalone HTML (no kaleido needed).

**Spec:** `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md`

**Commits:** the user (Jordi) commits manually. Commit steps in this plan are suggested commit messages and file lists — execute them yourself when you reach a green test state. Do NOT push.

---

## File map

| Path | Responsibility | Created in |
|---|---|---|
| `bot/audit/__init__.py` | Package marker, re-exports public API | Task 1 |
| `bot/audit/walk_forward.py` | Dataclasses + window splitter + window runner + aggregator + metric extraction | Tasks 2-5, 8-9 |
| `bot/audit/verdict.py` | GO / WATCH / NO-GO threshold evaluation | Task 6 |
| `bot/audit/comparison.py` | Paired t-test + Cohen's d (numpy-only) | Task 7 |
| `bot/audit/report.py` | Markdown + Plotly HTML chart writers | Task 11 |
| `scripts/audit/__init__.py` | Package marker | Task 10 |
| `scripts/audit/run_walk_forward.py` | CLI: fetch data, run windows, write report | Task 10 |
| `tests/test_walk_forward_splitter.py` | Window splitter tests | Task 3 |
| `tests/test_walk_forward_metrics.py` | Metric extraction tests | Task 4 |
| `tests/test_walk_forward_aggregator.py` | Aggregator tests (mean / CI / hit rate) | Task 5 |
| `tests/test_walk_forward_verdict.py` | Verdict branch + threshold tests | Task 6 |
| `tests/test_walk_forward_comparison.py` | t-test + Cohen's d tests | Task 7 |
| `tests/test_walk_forward_runner.py` | Window runner integration test (synthetic data) | Task 8 |

**Spec deviation:** spec section 7.1 listed a separate `bot/audit/metrics.py`. Plan consolidates metric extraction into `walk_forward.py` (files that change together live together). Test file naming follows project convention (flat `tests/`).

---

## Task 1: Setup package directories

**Files:**
- Create: `bot/audit/__init__.py`
- Create: `scripts/audit/__init__.py`

- [ ] **Step 1: Create package markers**

```bash
mkdir -p bot/audit scripts/audit
touch bot/audit/__init__.py scripts/audit/__init__.py
```

- [ ] **Step 2: Verify with pytest collection**

Run: `.venv/bin/pytest tests/ -q --collect-only 2>&1 | tail -3`
Expected: `334 tests collected` (no change, packages have no tests yet)

- [ ] **Step 3: Commit (Jordi runs manually)**

```bash
git add bot/audit/__init__.py scripts/audit/__init__.py
git commit -m "chore(audit): scaffold bot/audit and scripts/audit packages"
```

---

## Task 2: Define core dataclasses

**Files:**
- Modify: `bot/audit/walk_forward.py` (new file)
- Test: covered indirectly in Task 3

- [ ] **Step 1: Create walk_forward.py with frozen dataclasses**

Write the following to `bot/audit/walk_forward.py`:

```python
"""Walk-forward validation framework.

Splits a date range into rolling train/test windows, runs PortfolioBacktestEngine
on each test window with a fixed BacktestConfig, and collects per-window metrics
for downstream verdict / comparison analysis.

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dateutil.relativedelta import relativedelta


@dataclass(frozen=True)
class WalkForwardConfig:
    """Parameters for splitting a date range into walk-forward windows."""

    start_date: datetime
    end_date:   datetime
    train_months: int                       # warm-up period (no params learned here)
    test_months:  int                       # length of each out-of-sample test window
    step_months:  int                       # how far to shift the window each step
    symbols:      tuple[str, ...]
    timeframe:    str


@dataclass(frozen=True)
class Window:
    """A single train + test window pair within the walk-forward run."""

    index:       int
    train_start: datetime
    train_end:   datetime
    test_start:  datetime
    test_end:    datetime


@dataclass(frozen=True)
class WindowResult:
    """Metrics from running ONE config on ONE window's test period."""

    window:            Window
    config_name:       str                  # "C1" | "C2" | arbitrary label
    pf:                float
    calmar:            float
    sharpe:            float
    win_rate_pct:      float
    max_drawdown_pct:  float
    total_trades:      int
    final_pnl_pct:     float
    per_symbol:        dict[str, dict[str, Any]] = field(default_factory=dict)
```

- [ ] **Step 2: Smoke test — instantiate**

Run inline:

```bash
.venv/bin/python3 -c "
from datetime import datetime
from bot.audit.walk_forward import WalkForwardConfig, Window, WindowResult
cfg = WalkForwardConfig(
    start_date=datetime(2022,4,1), end_date=datetime(2026,5,1),
    train_months=18, test_months=3, step_months=3,
    symbols=('BTCUSDT','ETHUSDT'), timeframe='4h',
)
print('OK', cfg)
"
```

Expected: `OK WalkForwardConfig(...)` with no errors.

- [ ] **Step 3: Verify dateutil is installed**

Run: `.venv/bin/python3 -c "from dateutil.relativedelta import relativedelta; print('OK')"`
Expected: `OK`

If FAIL: dateutil is not a direct dep. Add to `requirements.txt`:

```
python-dateutil>=2.8
```

Then: `.venv/bin/pip install -r requirements.txt`

- [ ] **Step 4: Commit**

```bash
git add bot/audit/walk_forward.py
# If requirements.txt changed:
# git add requirements.txt
git commit -m "feat(audit): define WalkForwardConfig + Window + WindowResult dataclasses"
```

---

## Task 3: Window splitter

**Files:**
- Modify: `bot/audit/walk_forward.py` (append `split_windows()`)
- Test: `tests/test_walk_forward_splitter.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward_splitter.py`:

```python
"""Tests for bot.audit.walk_forward.split_windows()."""
from datetime import datetime

import pytest

from bot.audit.walk_forward import WalkForwardConfig, Window, split_windows


def _cfg(**overrides) -> WalkForwardConfig:
    base = dict(
        start_date=datetime(2022, 4, 1),
        end_date=datetime(2026, 4, 1),       # 48 months total
        train_months=18,
        test_months=3,
        step_months=3,
        symbols=("BTCUSDT",),
        timeframe="4h",
    )
    base.update(overrides)
    return WalkForwardConfig(**base)


def test_happy_path_window_count() -> None:
    """48 months total - 18 train = 30 months of testable space. 30 / 3 step = 10 windows."""
    windows = split_windows(_cfg())
    assert len(windows) == 10


def test_first_window_boundaries() -> None:
    windows = split_windows(_cfg())
    w0 = windows[0]
    assert w0.index == 0
    assert w0.train_start == datetime(2022, 4, 1)
    assert w0.train_end   == datetime(2023, 10, 1)
    assert w0.test_start  == datetime(2023, 10, 1)
    assert w0.test_end    == datetime(2024, 1, 1)


def test_last_window_does_not_exceed_end_date() -> None:
    windows = split_windows(_cfg())
    assert windows[-1].test_end <= datetime(2026, 4, 1)


def test_test_windows_do_not_overlap_when_step_equals_test_months() -> None:
    """With step == test_months, test windows must be back-to-back, never overlapping."""
    windows = split_windows(_cfg())
    for prev, curr in zip(windows, windows[1:]):
        assert prev.test_end <= curr.test_start, (
            f"Overlap: window {prev.index}.test_end={prev.test_end} > "
            f"window {curr.index}.test_start={curr.test_start}"
        )


def test_train_always_precedes_test() -> None:
    for w in split_windows(_cfg()):
        assert w.train_start < w.train_end
        assert w.train_end == w.test_start  # back-to-back
        assert w.test_start < w.test_end


def test_data_shorter_than_one_window_returns_empty() -> None:
    """Range = 12 months, train_months=18 → no window fits."""
    short = _cfg(end_date=datetime(2023, 4, 1))
    assert split_windows(short) == []


def test_window_indices_are_sequential_from_zero() -> None:
    windows = split_windows(_cfg())
    assert [w.index for w in windows] == list(range(len(windows)))


def test_custom_step_creates_overlapping_test_windows() -> None:
    """step=1 with test=3 means test windows DO overlap — allowed for non-statistical runs."""
    cfg = _cfg(step_months=1)
    windows = split_windows(cfg)
    # First two windows should share 2/3 of test period
    assert windows[1].test_start < windows[0].test_end


def test_train_months_zero_is_allowed() -> None:
    """No warm-up — full date range used as test. Useful for sanity-check runs."""
    cfg = _cfg(train_months=0)
    windows = split_windows(cfg)
    assert windows[0].train_start == windows[0].train_end == datetime(2022, 4, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_walk_forward_splitter.py -v`
Expected: All tests FAIL with `ImportError: cannot import name 'split_windows'` or similar.

- [ ] **Step 3: Implement split_windows()**

Append to `bot/audit/walk_forward.py`:

```python
def split_windows(cfg: WalkForwardConfig) -> list[Window]:
    """Return rolling train+test windows over [cfg.start_date, cfg.end_date].

    Each window has back-to-back train and test periods. Subsequent windows
    are shifted by cfg.step_months. When step == test_months, test windows
    are non-overlapping (independent samples — valid for paired t-tests).
    """
    train_delta = relativedelta(months=cfg.train_months)
    test_delta  = relativedelta(months=cfg.test_months)
    step_delta  = relativedelta(months=cfg.step_months)

    windows: list[Window] = []
    train_start = cfg.start_date
    index = 0
    while True:
        train_end  = train_start + train_delta
        test_start = train_end
        test_end   = test_start + test_delta
        if test_end > cfg.end_date:
            break
        windows.append(Window(
            index       = index,
            train_start = train_start,
            train_end   = train_end,
            test_start  = test_start,
            test_end    = test_end,
        ))
        train_start = train_start + step_delta
        index += 1
    return windows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_walk_forward_splitter.py -v`
Expected: All 9 tests PASS.

- [ ] **Step 5: Run full suite to confirm no regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `343 passed, 1 skipped` (334 prior + 9 new).

- [ ] **Step 6: Commit**

```bash
git add bot/audit/walk_forward.py tests/test_walk_forward_splitter.py
git commit -m "feat(audit): implement split_windows() with rolling train/test boundaries"
```

---

## Task 4: Metric extraction from portfolio result

**Files:**
- Modify: `bot/audit/walk_forward.py` (append `extract_window_metrics()`)
- Test: `tests/test_walk_forward_metrics.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward_metrics.py`:

```python
"""Tests for bot.audit.walk_forward.extract_window_metrics()."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.audit.walk_forward import Window, WindowResult, extract_window_metrics


def _window() -> Window:
    return Window(
        index=0,
        train_start=datetime(2022, 4, 1),
        train_end=datetime(2023, 10, 1),
        test_start=datetime(2023, 10, 1),
        test_end=datetime(2024, 1, 1),
    )


def _portfolio_result(global_summary: dict, per_symbol: dict) -> SimpleNamespace:
    """Build a minimal stub matching the shape of PortfolioBacktestResult.

    PortfolioBacktestResult has `portfolio_summary` (dict) and
    `per_symbol_summary` (dict[symbol, dict]).
    """
    return SimpleNamespace(
        portfolio_summary=global_summary,
        per_symbol_summary=per_symbol,
    )


def test_extracts_global_metrics() -> None:
    pr = _portfolio_result(
        global_summary={
            "profit_factor":      1.40,
            "sharpe_ratio":       1.20,
            "win_rate_pct":       42.0,
            "max_drawdown_pct":   12.5,
            "total_trades":       18,
            "total_pnl_pct":      8.0,
        },
        per_symbol={
            "BTCUSDT": {"profit_factor": 1.8, "total_trades": 10, "total_pnl_pct": 6.0},
            "ETHUSDT": {"profit_factor": 0.9, "total_trades": 8,  "total_pnl_pct": 2.0},
        },
    )
    result = extract_window_metrics(
        window=_window(),
        portfolio_result=pr,
        config_name="C1",
    )
    assert isinstance(result, WindowResult)
    assert result.config_name == "C1"
    assert result.pf == 1.40
    assert result.sharpe == 1.20
    assert result.win_rate_pct == 42.0
    assert result.max_drawdown_pct == 12.5
    assert result.total_trades == 18
    assert result.final_pnl_pct == 8.0


def test_calmar_computed_from_annualized_return() -> None:
    """Calmar = annualized_return / abs(max_dd). Test period = 3 months → 4x scaling."""
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 1.5, "sharpe_ratio": 1.0, "win_rate_pct": 40.0,
            "max_drawdown_pct": 10.0, "total_trades": 10, "total_pnl_pct": 5.0,
        },
        per_symbol={},
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C1")
    # 5% over 3 months → 20% annualized → Calmar = 20 / 10 = 2.0
    assert result.calmar == pytest.approx(2.0, abs=0.01)


def test_per_symbol_carried_through() -> None:
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 1.5, "sharpe_ratio": 1.0, "win_rate_pct": 40.0,
            "max_drawdown_pct": 10.0, "total_trades": 10, "total_pnl_pct": 5.0,
        },
        per_symbol={
            "BTCUSDT": {"profit_factor": 2.0, "total_trades": 7, "total_pnl_pct": 4.0},
            "ETHUSDT": {"profit_factor": 0.5, "total_trades": 3, "total_pnl_pct": 1.0},
        },
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C2")
    assert result.per_symbol["BTCUSDT"]["profit_factor"] == 2.0
    assert result.per_symbol["ETHUSDT"]["total_trades"] == 3


def test_zero_dd_yields_inf_calmar() -> None:
    """A perfect run (no drawdown) has infinite Calmar — must not crash on division."""
    pr = _portfolio_result(
        global_summary={
            "profit_factor": 5.0, "sharpe_ratio": 3.0, "win_rate_pct": 100.0,
            "max_drawdown_pct": 0.0, "total_trades": 5, "total_pnl_pct": 10.0,
        },
        per_symbol={},
    )
    result = extract_window_metrics(window=_window(), portfolio_result=pr, config_name="C1")
    assert result.calmar == float("inf")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_walk_forward_metrics.py -v`
Expected: All tests FAIL — `ImportError: cannot import name 'extract_window_metrics'`.

- [ ] **Step 3: Implement extract_window_metrics()**

Append to `bot/audit/walk_forward.py`:

```python
def extract_window_metrics(
    window: Window,
    portfolio_result: Any,    # PortfolioBacktestResult — duck-typed for test stubs
    config_name: str,
) -> WindowResult:
    """Build a WindowResult from a PortfolioBacktestEngine output.

    PortfolioBacktestResult exposes `portfolio_summary` (dict) and
    `per_symbol_summary` (dict[symbol, dict]). We pull the canonical fields
    and compute Calmar from the annualized return.
    """
    s = portfolio_result.portfolio_summary

    pnl_pct        = float(s.get("total_pnl_pct", 0.0))
    max_dd_pct     = float(s.get("max_drawdown_pct", 0.0))
    test_months    = relativedelta(window.test_end, window.test_start).months \
                     + relativedelta(window.test_end, window.test_start).years * 12
    annual_factor  = 12.0 / max(test_months, 1)
    annualized_pct = pnl_pct * annual_factor

    if max_dd_pct == 0.0:
        calmar = float("inf")
    else:
        calmar = annualized_pct / abs(max_dd_pct)

    return WindowResult(
        window           = window,
        config_name      = config_name,
        pf               = float(s.get("profit_factor", 0.0)),
        calmar           = calmar,
        sharpe           = float(s.get("sharpe_ratio", 0.0)),
        win_rate_pct     = float(s.get("win_rate_pct", 0.0)),
        max_drawdown_pct = max_dd_pct,
        total_trades     = int(s.get("total_trades", 0)),
        final_pnl_pct    = pnl_pct,
        per_symbol       = dict(portfolio_result.per_symbol_summary),
    )
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_walk_forward_metrics.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `347 passed, 1 skipped` (343 + 4 new).

- [ ] **Step 6: Commit**

```bash
git add bot/audit/walk_forward.py tests/test_walk_forward_metrics.py
git commit -m "feat(audit): extract_window_metrics() converts portfolio result to WindowResult"
```

---

## Task 5: Aggregator (mean / CI / hit rate)

**Files:**
- Modify: `bot/audit/walk_forward.py` (append `aggregate_metrics()`)
- Test: `tests/test_walk_forward_aggregator.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward_aggregator.py`:

```python
"""Tests for bot.audit.walk_forward.aggregate_metrics()."""
import math
from datetime import datetime

import numpy as np
import pytest

from bot.audit.walk_forward import Window, WindowResult, aggregate_metrics


def _wr(pf: float, calmar: float = 1.5, max_dd: float = 10.0, n: int = 10,
        config_name: str = "C1") -> WindowResult:
    w = Window(
        index=0,
        train_start=datetime(2022,1,1), train_end=datetime(2023,1,1),
        test_start=datetime(2023,1,1),  test_end=datetime(2023,4,1),
    )
    return WindowResult(
        window=w, config_name=config_name,
        pf=pf, calmar=calmar, sharpe=1.0, win_rate_pct=40.0,
        max_drawdown_pct=max_dd, total_trades=n, final_pnl_pct=5.0,
    )


def test_mean_and_median_of_pf() -> None:
    results = [_wr(pf=1.0), _wr(pf=1.5), _wr(pf=2.0)]
    agg = aggregate_metrics(results)
    assert agg["pf"]["mean"]   == pytest.approx(1.5, abs=1e-6)
    assert agg["pf"]["median"] == pytest.approx(1.5, abs=1e-6)
    assert agg["pf"]["std"]    == pytest.approx(np.std([1.0, 1.5, 2.0], ddof=1), abs=1e-6)


def test_hit_rate_pf_above_one() -> None:
    """Hit rate = % of windows with PF > 1.0."""
    results = [_wr(pf=0.8), _wr(pf=1.0), _wr(pf=1.2), _wr(pf=1.5)]
    agg = aggregate_metrics(results)
    # 2 out of 4 strictly > 1.0 (PF=1.0 is NOT a hit)
    assert agg["pf"]["hit_rate"] == pytest.approx(0.5, abs=1e-6)


def test_bootstrap_ci_returns_two_floats() -> None:
    """95% bootstrap CI for mean PF on a clearly-positive sample."""
    results = [_wr(pf=p) for p in [1.2, 1.4, 1.5, 1.3, 1.6, 1.45]]
    agg = aggregate_metrics(results, bootstrap_seed=42, n_bootstrap=2000)
    ci_lo, ci_hi = agg["pf"]["ci95"]
    assert isinstance(ci_lo, float) and isinstance(ci_hi, float)
    assert ci_lo < agg["pf"]["mean"] < ci_hi


def test_worst_window_metrics() -> None:
    results = [
        _wr(pf=1.0, max_dd=5.0),
        _wr(pf=0.5, max_dd=25.0),   # worst PF AND worst DD
        _wr(pf=1.5, max_dd=8.0),
    ]
    agg = aggregate_metrics(results)
    assert agg["pf"]["worst"]              == pytest.approx(0.5, abs=1e-6)
    assert agg["max_drawdown_pct"]["worst"] == pytest.approx(25.0, abs=1e-6)


def test_sparsity_count_windows_under_5_trades() -> None:
    results = [_wr(pf=1.0, n=2), _wr(pf=1.0, n=4), _wr(pf=1.0, n=8), _wr(pf=1.0, n=10)]
    agg = aggregate_metrics(results)
    assert agg["sparsity"]["windows_lt_5_trades"] == 2
    assert agg["sparsity"]["pct"]                == pytest.approx(0.5, abs=1e-6)


def test_inf_calmar_ignored_in_mean() -> None:
    """inf Calmar (zero DD windows) should not poison the mean."""
    results = [
        _wr(pf=1.0, calmar=2.0),
        _wr(pf=1.0, calmar=float("inf")),
        _wr(pf=1.0, calmar=3.0),
    ]
    agg = aggregate_metrics(results)
    assert math.isfinite(agg["calmar"]["mean"])
    assert agg["calmar"]["mean"] == pytest.approx(2.5, abs=1e-6)


def test_empty_input_returns_empty_dict() -> None:
    assert aggregate_metrics([]) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_walk_forward_aggregator.py -v`
Expected: All tests FAIL.

- [ ] **Step 3: Implement aggregate_metrics()**

Append to `bot/audit/walk_forward.py`:

```python
import numpy as np


def aggregate_metrics(
    results: list[WindowResult],
    *,
    bootstrap_seed: int = 42,
    n_bootstrap:    int = 2000,
) -> dict:
    """Aggregate per-window metrics into mean / median / std / CI / hit rate / worst.

    Returns an empty dict on empty input. Infinite values are stripped before
    computing mean/std (zero-DD windows produce inf Calmar — valid but skews stats).
    """
    if not results:
        return {}

    def _stats(values: list[float], hit_threshold: float | None = None) -> dict:
        arr      = np.array([v for v in values if np.isfinite(v)], dtype=float)
        all_arr  = np.array(values, dtype=float)
        out = {
            "mean":   float(np.mean(arr))   if arr.size else float("nan"),
            "median": float(np.median(arr)) if arr.size else float("nan"),
            "std":    float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0,
            "min":    float(np.min(all_arr))  if all_arr.size else float("nan"),
            "max":    float(np.max(all_arr))  if all_arr.size else float("nan"),
            "worst":  float(np.min(all_arr))  if all_arr.size else float("nan"),
        }
        if hit_threshold is not None and all_arr.size:
            out["hit_rate"] = float(np.mean(all_arr > hit_threshold))
        # Bootstrap CI on the finite values
        if arr.size > 1:
            rng = np.random.default_rng(bootstrap_seed)
            samples = rng.choice(arr, size=(n_bootstrap, arr.size), replace=True)
            means   = samples.mean(axis=1)
            out["ci95"] = (float(np.percentile(means, 2.5)),
                           float(np.percentile(means, 97.5)))
        else:
            out["ci95"] = (out["mean"], out["mean"])
        return out

    pf_vals     = [r.pf for r in results]
    calmar_vals = [r.calmar for r in results]
    sharpe_vals = [r.sharpe for r in results]
    wr_vals     = [r.win_rate_pct for r in results]
    dd_vals     = [r.max_drawdown_pct for r in results]
    pnl_vals    = [r.final_pnl_pct for r in results]
    trades      = [r.total_trades for r in results]

    # Worst DD means highest DD (not lowest) — override the convention
    dd_stats          = _stats(dd_vals)
    dd_stats["worst"] = float(np.max(np.array(dd_vals)))

    return {
        "pf":               {**_stats(pf_vals, hit_threshold=1.0)},
        "calmar":           _stats(calmar_vals),
        "sharpe":           _stats(sharpe_vals),
        "win_rate_pct":     _stats(wr_vals),
        "max_drawdown_pct": dd_stats,
        "final_pnl_pct":    _stats(pnl_vals),
        "total_trades":     {
            "mean":   float(np.mean(trades)),
            "median": float(np.median(trades)),
            "min":    int(np.min(trades)),
            "max":    int(np.max(trades)),
        },
        "sparsity": {
            "windows_lt_5_trades": int(sum(1 for n in trades if n < 5)),
            "pct":                 float(np.mean(np.array(trades) < 5)),
        },
        "n_windows": len(results),
    }
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_walk_forward_aggregator.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `354 passed, 1 skipped` (347 + 7 new).

- [ ] **Step 6: Commit**

```bash
git add bot/audit/walk_forward.py tests/test_walk_forward_aggregator.py
git commit -m "feat(audit): aggregate_metrics() computes mean/CI/hit_rate across windows"
```

---

## Task 6: Verdict evaluation

**Files:**
- Create: `bot/audit/verdict.py`
- Test: `tests/test_walk_forward_verdict.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward_verdict.py`:

```python
"""Tests for bot.audit.verdict.evaluate_verdict()."""
import pytest

from bot.audit.verdict import VerdictThresholds, evaluate_verdict


def _agg(pf_mean: float, pf_hit: float, calmar_mean: float,
         worst_dd: float, sparsity_pct: float, n: int = 8) -> dict:
    return {
        "pf":               {"mean": pf_mean, "hit_rate": pf_hit},
        "calmar":           {"mean": calmar_mean},
        "max_drawdown_pct": {"worst": worst_dd},
        "sparsity":         {"pct": sparsity_pct},
        "n_windows":        n,
    }


def test_go_when_all_thresholds_met() -> None:
    v = evaluate_verdict(_agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
                              worst_dd=20.0, sparsity_pct=0.10))
    assert v["bucket"] == "GO"
    assert all(c["passed"] for c in v["checks"])


def test_no_go_when_pf_well_below_threshold() -> None:
    v = evaluate_verdict(_agg(pf_mean=0.95, pf_hit=0.40, calmar_mean=0.8,
                              worst_dd=42.0, sparsity_pct=0.30))
    assert v["bucket"] == "NO-GO"


def test_watch_when_marginal_failure() -> None:
    """PF mean = 1.10 is 8.3% below threshold 1.20 → WATCH (within 10%)."""
    v = evaluate_verdict(_agg(pf_mean=1.10, pf_hit=0.70, calmar_mean=1.6,
                              worst_dd=30.0, sparsity_pct=0.10))
    assert v["bucket"] == "WATCH"


def test_pf_exactly_at_threshold_is_go() -> None:
    """PF = 1.20 exact triggers GO (the threshold is inclusive)."""
    v = evaluate_verdict(_agg(pf_mean=1.20, pf_hit=0.70, calmar_mean=1.5,
                              worst_dd=20.0, sparsity_pct=0.10))
    assert v["bucket"] == "GO"


def test_dd_above_35_is_hard_fail() -> None:
    """Even with great PF, max DD > 35% fails."""
    v = evaluate_verdict(_agg(pf_mean=1.50, pf_hit=0.90, calmar_mean=2.5,
                              worst_dd=40.0, sparsity_pct=0.05))
    # 40 is 14% above 35 threshold → > 10% margin → NO-GO
    assert v["bucket"] == "NO-GO"


def test_checks_array_lists_each_threshold() -> None:
    v = evaluate_verdict(_agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
                              worst_dd=20.0, sparsity_pct=0.10))
    check_names = {c["name"] for c in v["checks"]}
    assert check_names == {
        "pf_mean", "pf_hit_rate", "calmar_mean", "max_dd_within_limit",
        "sparsity_within_limit",
    }


def test_custom_thresholds_override_defaults() -> None:
    """Caller can pass a custom VerdictThresholds for sub-projects that want different bars."""
    strict = VerdictThresholds(pf_mean=1.5, pf_hit_rate=0.80, calmar_mean=2.0,
                                max_dd_pct=25.0, sparsity_pct=0.10)
    v = evaluate_verdict(
        _agg(pf_mean=1.25, pf_hit=0.70, calmar_mean=1.8,
             worst_dd=20.0, sparsity_pct=0.10),
        thresholds=strict,
    )
    assert v["bucket"] != "GO"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_walk_forward_verdict.py -v`
Expected: All tests FAIL — module not found.

- [ ] **Step 3: Implement verdict.py**

Create `bot/audit/verdict.py`:

```python
"""GO / WATCH / NO-GO verdict for a single config's aggregated metrics.

Thresholds are predetermined per the audit spec to avoid p-hacking.
WATCH = any threshold fails by ≤ 10%. NO-GO = any threshold fails by > 10%.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerdictThresholds:
    """Predetermined thresholds (spec section 4.1)."""
    pf_mean:       float = 1.20
    pf_hit_rate:   float = 0.65
    calmar_mean:   float = 1.50
    max_dd_pct:    float = 35.0    # worst-window DD ceiling
    sparsity_pct:  float = 0.20    # max share of windows with < 5 trades


def _check(name: str, value: float, threshold: float, *, higher_is_better: bool) -> dict:
    if higher_is_better:
        passed = value >= threshold
        # how far below threshold (negative if passed)
        margin = (threshold - value) / threshold if threshold else 0.0
    else:
        passed = value <= threshold
        margin = (value - threshold) / threshold if threshold else 0.0
    return {
        "name":      name,
        "value":     value,
        "threshold": threshold,
        "passed":    bool(passed),
        "margin":    float(margin),     # positive when failed, negative when passed
    }


def evaluate_verdict(
    aggregate: dict,
    thresholds: VerdictThresholds = VerdictThresholds(),
) -> dict:
    """Return verdict dict with `bucket` (GO/WATCH/NO-GO) and `checks` list."""
    pf_mean      = aggregate["pf"]["mean"]
    pf_hit_rate  = aggregate["pf"]["hit_rate"]
    calmar_mean  = aggregate["calmar"]["mean"]
    worst_dd     = aggregate["max_drawdown_pct"]["worst"]
    sparsity_pct = aggregate["sparsity"]["pct"]

    checks = [
        _check("pf_mean",               pf_mean,      thresholds.pf_mean,      higher_is_better=True),
        _check("pf_hit_rate",           pf_hit_rate,  thresholds.pf_hit_rate,  higher_is_better=True),
        _check("calmar_mean",           calmar_mean,  thresholds.calmar_mean,  higher_is_better=True),
        _check("max_dd_within_limit",   worst_dd,     thresholds.max_dd_pct,   higher_is_better=False),
        _check("sparsity_within_limit", sparsity_pct, thresholds.sparsity_pct, higher_is_better=False),
    ]

    failures = [c for c in checks if not c["passed"]]
    if not failures:
        bucket = "GO"
    elif all(c["margin"] <= 0.10 for c in failures):
        bucket = "WATCH"
    else:
        bucket = "NO-GO"

    return {"bucket": bucket, "checks": checks, "thresholds": thresholds.__dict__}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_walk_forward_verdict.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `361 passed, 1 skipped` (354 + 7 new).

- [ ] **Step 6: Commit**

```bash
git add bot/audit/verdict.py tests/test_walk_forward_verdict.py
git commit -m "feat(audit): verdict.evaluate_verdict() with GO/WATCH/NO-GO bucketing"
```

---

## Task 7: Comparison (paired t-test + Cohen's d)

**Files:**
- Create: `bot/audit/comparison.py`
- Test: `tests/test_walk_forward_comparison.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_walk_forward_comparison.py`:

```python
"""Tests for bot.audit.comparison.compare_configs()."""
import numpy as np
import pytest

from bot.audit.comparison import (
    cohens_d_paired,
    paired_t_test,
    compare_configs,
)


def test_paired_t_test_zero_difference_yields_zero_t() -> None:
    a = [1.0, 1.2, 1.4, 1.6, 1.5]
    b = list(a)
    result = paired_t_test(a, b)
    assert result["t"]   == pytest.approx(0.0, abs=1e-9)
    assert result["p"]   == pytest.approx(1.0, abs=1e-6)


def test_paired_t_test_large_difference_yields_significant_p() -> None:
    a = [2.0, 2.1, 2.2, 2.3, 2.4]
    b = [1.0, 1.1, 1.2, 1.3, 1.4]
    result = paired_t_test(a, b)
    assert result["t"] > 0
    assert result["p"] < 0.01


def test_cohens_d_paired_zero_when_identical() -> None:
    assert cohens_d_paired([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 0.0


def test_cohens_d_paired_positive_when_a_higher() -> None:
    """A is higher than B → positive d."""
    a = [1.5, 1.6, 1.4, 1.7]
    b = [1.0, 1.1, 0.9, 1.2]
    assert cohens_d_paired(a, b) > 0


def test_cohens_d_paired_negative_when_b_higher() -> None:
    assert cohens_d_paired([1.0, 1.1], [1.5, 1.6]) < 0


def test_compare_configs_returns_full_summary() -> None:
    pf_c1 = [1.2, 1.4, 1.3, 1.5, 1.45]
    pf_c2 = [1.5, 1.6, 1.55, 1.7, 1.65]
    result = compare_configs(pf_c1, pf_c2, metric_name="pf")
    assert "t" in result and "p" in result
    assert "cohens_d" in result
    assert "mean_a" in result and "mean_b" in result
    assert "delta_mean" in result
    assert result["delta_mean"] == pytest.approx(np.mean(pf_c2) - np.mean(pf_c1), abs=1e-9)


def test_compare_configs_requires_equal_length() -> None:
    with pytest.raises(ValueError):
        compare_configs([1.0, 2.0], [1.0, 2.0, 3.0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_walk_forward_comparison.py -v`
Expected: All tests FAIL — module not found.

- [ ] **Step 3: Implement comparison.py**

Create `bot/audit/comparison.py`:

```python
"""Statistical comparison between two configs (numpy-only, no scipy dependency).

Paired t-test and Cohen's d for the case where the same set of windows is
evaluated under two configs. Returns the practical significance summary
needed by the audit's comparative verdict.
"""
from __future__ import annotations

import math

import numpy as np


def paired_t_test(a: list[float], b: list[float]) -> dict:
    """Two-sided paired t-test on samples a vs b. Returns dict with t and p.

    Implementation: t = mean(d) / (std(d, ddof=1) / sqrt(n)) where d = a - b.
    p value computed from Student's t CDF via incomplete beta function.
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")
    if arr_a.size < 2:
        raise ValueError("Need at least 2 paired samples")

    diff = arr_a - arr_b
    n    = diff.size
    mean = float(np.mean(diff))
    sd   = float(np.std(diff, ddof=1))

    if sd == 0.0:
        return {"t": 0.0, "p": 1.0, "df": n - 1}

    t  = mean / (sd / math.sqrt(n))
    df = n - 1
    # Two-sided p value via the regularized incomplete beta function.
    # p = I_{df/(df+t^2)}(df/2, 1/2) — the standard Student's-t two-sided CDF.
    x = df / (df + t * t)
    p = float(_betainc_regularized(df / 2.0, 0.5, x))
    # Numerical guard
    p = max(0.0, min(1.0, p))
    return {"t": float(t), "p": p, "df": df}


def cohens_d_paired(a: list[float], b: list[float]) -> float:
    """Cohen's d for paired samples: mean(a - b) / std(a - b, ddof=1)."""
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")
    diff = arr_a - arr_b
    if diff.size < 2:
        return 0.0
    sd = float(np.std(diff, ddof=1))
    if sd == 0.0:
        return 0.0
    return float(np.mean(diff) / sd)


def compare_configs(a: list[float], b: list[float], metric_name: str = "pf") -> dict:
    """Full comparison summary for two configs over the same windows.

    `delta_mean = mean(b) - mean(a)` so positive means config B is better
    (matches "Δ PF in C2's favor" reading from the spec).
    """
    arr_a = np.asarray(a, dtype=float)
    arr_b = np.asarray(b, dtype=float)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"Length mismatch: {arr_a.shape} vs {arr_b.shape}")

    t_result = paired_t_test(a, b)
    return {
        "metric":     metric_name,
        "n":          int(arr_a.size),
        "mean_a":     float(np.mean(arr_a)),
        "mean_b":     float(np.mean(arr_b)),
        "delta_mean": float(np.mean(arr_b) - np.mean(arr_a)),
        "t":          t_result["t"],
        "p":          t_result["p"],
        "df":         t_result["df"],
        "cohens_d":   cohens_d_paired(a, b),
    }


# ── Internal: regularized incomplete beta function (Numerical Recipes 6.4) ───

def _betainc_regularized(a: float, b: float, x: float) -> float:
    """I_x(a, b) = B(x; a, b) / B(a, b) — used by the Student t-distribution CDF."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    # Log gamma via lgamma for numerical stability
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, *, max_iter: int = 200, eps: float = 3e-7) -> float:
    """Continued fraction expansion of the regularized incomplete beta."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-30:
        d = 1e-30
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d  = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c  = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d  = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d  = 1.0 + aa * d
        if abs(d) < 1e-30:
            d = 1e-30
        c  = 1.0 + aa / c
        if abs(c) < 1e-30:
            c = 1e-30
        d  = 1.0 / d
        delta = d * c
        h    *= delta
        if abs(delta - 1.0) < eps:
            return h
    return h
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_walk_forward_comparison.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `368 passed, 1 skipped` (361 + 7 new).

- [ ] **Step 6: Commit**

```bash
git add bot/audit/comparison.py tests/test_walk_forward_comparison.py
git commit -m "feat(audit): paired t-test + Cohen's d in pure numpy (no scipy dep)"
```

---

## Task 8: Window runner (integration with PortfolioBacktestEngine)

**Files:**
- Modify: `bot/audit/walk_forward.py` (append `run_window()`)
- Test: `tests/test_walk_forward_runner.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_walk_forward_runner.py`:

```python
"""Integration test: run_window() against synthetic OHLCV."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import BacktestConfig
from bot.audit.walk_forward import Window, WindowResult, run_window


def _synthetic_klines(start: datetime, end: datetime, interval_h: int = 4,
                       seed: int = 0) -> pd.DataFrame:
    """Deterministic random-walk OHLCV for testing."""
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range(start, end, freq=f"{interval_h}h", tz=timezone.utc, inclusive="left")
    n = len(timestamps)
    prices = 100.0 + np.cumsum(rng.normal(0, 1, size=n))
    prices = np.maximum(prices, 1.0)
    df = pd.DataFrame({
        "open_time": timestamps,
        "open":  prices,
        "high":  prices * 1.005,
        "low":   prices * 0.995,
        "close": prices,
        "volume": rng.uniform(1000, 5000, size=n),
    })
    return df


def test_run_window_returns_window_result_for_synthetic_data() -> None:
    """run_window must accept slices, invoke PortfolioBacktestEngine, return WindowResult."""
    train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    test_end    = datetime(2024, 9, 1, tzinfo=timezone.utc)
    w = Window(
        index=0,
        train_start=train_start,
        train_end=datetime(2024, 7, 1, tzinfo=timezone.utc),
        test_start=datetime(2024, 7, 1, tzinfo=timezone.utc),
        test_end=test_end,
    )

    dfs = {
        "BTCUSDT": _synthetic_klines(train_start, test_end, interval_h=4, seed=1),
        "ETHUSDT": _synthetic_klines(train_start, test_end, interval_h=4, seed=2),
    }
    dfs_bias = {
        "BTCUSDT": _synthetic_klines(train_start, test_end, interval_h=24, seed=3),
        "ETHUSDT": _synthetic_klines(train_start, test_end, interval_h=24, seed=4),
    }

    cfg = BacktestConfig(
        initial_capital=10_000.0, risk_per_trade=0.015, timeframe="4h",
        long_only=True, ema_stop_mult=1.5, ema_tp_mult=4.5,
    )
    result = run_window(
        window=w,
        backtest_config=cfg,
        config_name="C1",
        dfs=dfs,
        dfs_bias=dfs_bias,
        dfs_weekly=None,
    )
    assert isinstance(result, WindowResult)
    assert result.config_name == "C1"
    assert result.window.index == 0
    # Numbers can be anything on synthetic data — just verify shape
    assert isinstance(result.pf, float)
    assert isinstance(result.total_trades, int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_walk_forward_runner.py -v`
Expected: FAIL — `run_window` not defined.

- [ ] **Step 3: Implement run_window()**

Append to `bot/audit/walk_forward.py`:

```python
def _slice_by_time(df: "pd.DataFrame | None", start: datetime, end: datetime):
    """Return df rows where open_time is within [start, end). None passes through."""
    if df is None:
        return None
    mask = (df["open_time"] >= start) & (df["open_time"] < end)
    return df.loc[mask].reset_index(drop=True)


def run_window(
    window:           Window,
    backtest_config:  Any,         # BacktestConfig — duck-typed for circular import safety
    config_name:      str,
    dfs:              dict,
    dfs_bias:         dict | None  = None,
    dfs_weekly:       dict | None  = None,
    dfs_1m:           dict | None  = None,
) -> WindowResult:
    """Slice dataframes to the window's [train_start, test_end) range, run portfolio
    backtest on the combined train+test, then extract metrics.

    The training period is used only as indicator warm-up — no parameter fitting.
    All metrics are computed over the FULL combined range; the per-window stats
    therefore include warm-up bars. This matches BacktestEngine semantics.
    """
    from bot.backtest.portfolio_engine import PortfolioBacktestEngine

    dfs_sliced        = {sym: _slice_by_time(df, window.train_start, window.test_end)
                         for sym, df in dfs.items()}
    dfs_bias_sliced   = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_bias.items()}
        if dfs_bias else None
    )
    dfs_weekly_sliced = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_weekly.items()}
        if dfs_weekly else None
    )
    dfs_1m_sliced     = (
        {sym: _slice_by_time(df, window.train_start, window.test_end)
         for sym, df in dfs_1m.items()}
        if dfs_1m else None
    )

    engine = PortfolioBacktestEngine(backtest_config)
    result = engine.run_portfolio(
        dfs_sliced,
        dfs_4h     = dfs_bias_sliced,
        dfs_weekly = dfs_weekly_sliced,
        dfs_1m     = dfs_1m_sliced,
    )
    return extract_window_metrics(window=window, portfolio_result=result, config_name=config_name)
```

- [ ] **Step 4: Run test**

Run: `.venv/bin/pytest tests/test_walk_forward_runner.py -v`
Expected: PASS (numbers are arbitrary but shape is verified).

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `369 passed, 1 skipped` (368 + 1 new). The synthetic-data run may print warnings — those are acceptable.

- [ ] **Step 6: Commit**

```bash
git add bot/audit/walk_forward.py tests/test_walk_forward_runner.py
git commit -m "feat(audit): run_window() slices data and invokes PortfolioBacktestEngine"
```

---

## Task 9: run_all() orchestrator

**Files:**
- Modify: `bot/audit/walk_forward.py` (append `run_all()`)
- Test: covered by `tests/test_walk_forward_runner.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_walk_forward_runner.py`:

```python
def test_run_all_iterates_windows_and_configs() -> None:
    train_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end         = datetime(2025, 6, 1, tzinfo=timezone.utc)

    dfs = {
        "BTCUSDT": _synthetic_klines(train_start, end, interval_h=4, seed=1),
        "ETHUSDT": _synthetic_klines(train_start, end, interval_h=4, seed=2),
    }
    dfs_bias = {
        "BTCUSDT": _synthetic_klines(train_start, end, interval_h=24, seed=3),
        "ETHUSDT": _synthetic_klines(train_start, end, interval_h=24, seed=4),
    }

    from bot.audit.walk_forward import WalkForwardConfig, run_all
    from bot.backtest.engine import BacktestConfig

    wf_cfg = WalkForwardConfig(
        start_date=train_start, end_date=end,
        train_months=6, test_months=3, step_months=3,
        symbols=("BTCUSDT", "ETHUSDT"), timeframe="4h",
    )
    configs = {
        "C1": BacktestConfig(initial_capital=10_000.0, risk_per_trade=0.015,
                              timeframe="4h", long_only=True,
                              ema_stop_mult=1.5, ema_tp_mult=4.5),
        "C2": BacktestConfig(initial_capital=10_000.0, risk_per_trade=0.03,
                              timeframe="4h", long_only=True,
                              ema_stop_mult=1.25, ema_tp_mult=3.5),
    }
    results = run_all(
        wf_config=wf_cfg, backtest_configs=configs,
        dfs=dfs, dfs_bias=dfs_bias, dfs_weekly=None,
    )
    # 6mo train + 3mo test, step 3mo over 17 months → ≥ 3 windows × 2 configs = 6 results
    assert len(results) >= 6
    config_names_seen = {r.config_name for r in results}
    assert config_names_seen == {"C1", "C2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_walk_forward_runner.py::test_run_all_iterates_windows_and_configs -v`
Expected: FAIL — `run_all` not defined.

- [ ] **Step 3: Implement run_all()**

Append to `bot/audit/walk_forward.py`:

```python
def run_all(
    wf_config:        WalkForwardConfig,
    backtest_configs: dict[str, Any],     # {"C1": BacktestConfig, "C2": ...}
    dfs:              dict,
    dfs_bias:         dict | None = None,
    dfs_weekly:       dict | None = None,
    dfs_1m:           dict | None = None,
    progress_cb:      callable | None = None,
) -> list[WindowResult]:
    """Run every (window, config) pair sequentially.

    Order is `[w0_C1, w0_C2, w1_C1, w1_C2, ...]` so paired-window comparison
    is straightforward. progress_cb receives a string per (window, config).
    """
    windows = split_windows(wf_config)
    results: list[WindowResult] = []
    for w in windows:
        for name, bt_cfg in backtest_configs.items():
            if progress_cb:
                progress_cb(f"window={w.index} config={name} test={w.test_start.date()}→{w.test_end.date()}")
            results.append(run_window(
                window=w, backtest_config=bt_cfg, config_name=name,
                dfs=dfs, dfs_bias=dfs_bias, dfs_weekly=dfs_weekly, dfs_1m=dfs_1m,
            ))
    return results
```

- [ ] **Step 4: Run test**

Run: `.venv/bin/pytest tests/test_walk_forward_runner.py -v`
Expected: PASS — both tests now pass.

- [ ] **Step 5: Full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `370 passed, 1 skipped`.

- [ ] **Step 6: Commit**

```bash
git add bot/audit/walk_forward.py tests/test_walk_forward_runner.py
git commit -m "feat(audit): run_all() iterates windows × configs and returns WindowResult list"
```

---

## Task 10: CLI entry point

**Files:**
- Create: `scripts/audit/run_walk_forward.py`

- [ ] **Step 1: Write the CLI script**

Create `scripts/audit/run_walk_forward.py`:

```python
"""CLI entry point for the walk-forward validation audit (sub-project A).

Usage:
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py --only C1
    PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py --train-months 12 --test-months 6 --step-months 6

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bot.audit.comparison import compare_configs
from bot.audit.verdict import evaluate_verdict
from bot.audit.walk_forward import (
    WalkForwardConfig,
    aggregate_metrics,
    run_all,
)
from bot.audit.report import write_markdown_report
from bot.backtest.cache import fetch_and_cache
from bot.backtest.engine import BacktestConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("audit")


# ── Spec-locked configs (DO NOT modify mid-audit; see spec section 5) ─────────

CONFIG_C1_BASELINE = BacktestConfig(
    initial_capital         = 10_000.0,
    risk_per_trade          = 0.015,
    timeframe               = "4h",
    cost_per_side_pct       = 0.001,
    long_only               = True,
    ema_stop_mult           = 1.5,
    ema_tp_mult             = 4.5,
    ema_max_distance_atr    = 1.0,
    momentum_filter_enabled = True,
    momentum_sma_period     = 20,
    momentum_neutral_band   = 0.05,
)

CONFIG_C2_PROD = BacktestConfig(
    initial_capital         = 10_000.0,
    risk_per_trade          = 0.03,
    timeframe               = "4h",
    cost_per_side_pct       = 0.001,
    long_only               = True,
    ema_stop_mult           = 1.25,
    ema_tp_mult             = 3.5,
    ema_max_distance_atr    = 1.0,
    ema_volume_mult         = 2.0,
    ema_require_momentum    = True,
    momentum_filter_enabled = True,
    momentum_sma_period     = 20,
    momentum_neutral_band   = 0.05,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-forward validation audit")
    p.add_argument("--start", type=str, default="2022-04-01",
                   help="ISO date for the earliest train_start")
    p.add_argument("--end",   type=str, default="2026-05-01",
                   help="ISO date for the latest test_end")
    p.add_argument("--train-months", type=int, default=18)
    p.add_argument("--test-months",  type=int, default=3)
    p.add_argument("--step-months",  type=int, default=3)
    p.add_argument("--only", type=str, default=None, choices=["C1", "C2"],
                   help="Run only the named config (debugging)")
    return p.parse_args()


def _to_jsonable(window_results) -> list[dict]:
    """Convert WindowResult dataclasses to plain dicts for JSON serialization."""
    out = []
    for r in window_results:
        d = asdict(r)
        # datetime objects in nested Window
        for key in ("train_start", "train_end", "test_start", "test_end"):
            d["window"][key] = d["window"][key].isoformat()
        # Replace inf floats (JSON can't represent them)
        for k, v in d.items():
            if isinstance(v, float) and (v == float("inf") or v == float("-inf")):
                d[k] = "Infinity" if v > 0 else "-Infinity"
        out.append(d)
    return out


def main() -> int:
    args = _parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    log.info("Walk-forward audit — start=%s end=%s train=%dm test=%dm step=%dm",
             start_dt.date(), end_dt.date(), args.train_months, args.test_months, args.step_months)

    # ── Fetch / load cached klines ──────────────────────────────────────────
    log.info("Loading klines (4h + 1d + 1w) for BTCUSDT and ETHUSDT …")
    dfs        = {sym: fetch_and_cache(sym, "4h", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_bias   = {sym: fetch_and_cache(sym, "1d", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}
    dfs_weekly = {sym: fetch_and_cache(sym, "1w", start_dt, end_dt) for sym in ("BTCUSDT", "ETHUSDT")}

    wf_cfg = WalkForwardConfig(
        start_date   = start_dt,
        end_date     = end_dt,
        train_months = args.train_months,
        test_months  = args.test_months,
        step_months  = args.step_months,
        symbols      = ("BTCUSDT", "ETHUSDT"),
        timeframe    = "4h",
    )

    bt_configs = {"C1": CONFIG_C1_BASELINE, "C2": CONFIG_C2_PROD}
    if args.only:
        bt_configs = {args.only: bt_configs[args.only]}

    # ── Run all (window × config) ───────────────────────────────────────────
    log.info("Running %d configs over expected windows …", len(bt_configs))
    results = run_all(
        wf_config        = wf_cfg,
        backtest_configs = bt_configs,
        dfs              = dfs,
        dfs_bias         = dfs_bias,
        dfs_weekly       = dfs_weekly,
        progress_cb      = log.info,
    )
    log.info("Collected %d window results", len(results))

    # ── Aggregate per config + verdicts + comparison ────────────────────────
    summaries: dict = {}
    for name in bt_configs:
        cfg_results = [r for r in results if r.config_name == name]
        if not cfg_results:
            continue
        agg     = aggregate_metrics(cfg_results)
        verdict = evaluate_verdict(agg)
        summaries[name] = {"aggregate": agg, "verdict": verdict, "n_windows": len(cfg_results)}

    comparison = None
    if "C1" in summaries and "C2" in summaries:
        # Use window index to pair correctly
        c1 = sorted([r for r in results if r.config_name == "C1"], key=lambda r: r.window.index)
        c2 = sorted([r for r in results if r.config_name == "C2"], key=lambda r: r.window.index)
        comparison = compare_configs([r.pf for r in c1], [r.pf for r in c2], metric_name="pf")

    # ── Persist raw JSON + markdown report ──────────────────────────────────
    iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    out_data_dir   = Path("data/audits"); out_data_dir.mkdir(parents=True, exist_ok=True)
    out_docs_dir   = Path("docs/audits"); out_docs_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_data_dir / f"A_walk_forward_{iso}.json"
    md_path   = out_docs_dir / f"A_walk_forward_{datetime.now().strftime('%Y-%m-%d')}.md"

    payload = {
        "args":       vars(args),
        "results":    _to_jsonable(results),
        "summaries":  summaries,
        "comparison": comparison,
        "generated":  iso,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("Raw results → %s", json_path)

    write_markdown_report(payload, md_path)
    log.info("Report → %s", md_path)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Quick syntax check**

Run: `.venv/bin/python3 -c "import ast; ast.parse(open('scripts/audit/run_walk_forward.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit (the report writer is the next task — CLI is stubbed against it)**

```bash
git add scripts/audit/run_walk_forward.py
git commit -m "feat(audit): CLI runner with spec-locked C1 (baseline) and C2 (prod) configs"
```

---

## Task 11: Report writer (markdown + Plotly HTML)

**Files:**
- Create: `bot/audit/report.py`

- [ ] **Step 1: Implement report writer**

Create `bot/audit/report.py`:

```python
"""Markdown + Plotly HTML report writer for walk-forward audit results."""
from __future__ import annotations

import math
from pathlib import Path

import plotly.graph_objects as go


def _fmt(v: object, digits: int = 2) -> str:
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        return f"{v:.{digits}f}"
    return str(v)


def _verdict_section(name: str, summary: dict) -> str:
    v        = summary["verdict"]
    bucket   = v["bucket"]
    emoji    = {"GO": "✅", "WATCH": "⚠️", "NO-GO": "❌"}[bucket]
    rows = []
    for c in v["checks"]:
        rows.append(
            f"| {c['name']} | {_fmt(c['value'], 3)} | {_fmt(c['threshold'])} | "
            f"{'✅' if c['passed'] else '❌'} | {_fmt(c['margin']*100, 1)}% |"
        )
    return (
        f"### {name}: {emoji} **{bucket}**\n\n"
        f"Windows tested: **{summary['n_windows']}**\n\n"
        "| Check | Value | Threshold | Passed | Margin |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )


def _aggregate_table(name: str, summary: dict) -> str:
    agg = summary["aggregate"]
    rows = []
    for metric in ("pf", "calmar", "sharpe", "win_rate_pct", "max_drawdown_pct", "final_pnl_pct"):
        m = agg[metric]
        ci_lo, ci_hi = m.get("ci95", (float("nan"), float("nan")))
        rows.append(
            f"| {metric} | {_fmt(m['mean'])} | {_fmt(m['median'])} | "
            f"{_fmt(m['std'])} | [{_fmt(ci_lo)}, {_fmt(ci_hi)}] | "
            f"{_fmt(m.get('worst', float('nan')))} |"
        )
    return (
        f"#### {name} — aggregate metrics\n\n"
        "| Metric | Mean | Median | Std | 95% CI | Worst |\n"
        "|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
        + f"\n**Hit rate (PF > 1.0)**: {_fmt(agg['pf']['hit_rate']*100, 1)}%  ·  "
          f"**Sparsity (windows < 5 trades)**: "
          f"{agg['sparsity']['windows_lt_5_trades']} ({_fmt(agg['sparsity']['pct']*100, 1)}%)\n"
    )


def _per_window_table(payload: dict) -> str:
    rows = []
    for r in payload["results"]:
        rows.append(
            f"| {r['config_name']} | {r['window']['index']} | "
            f"{r['window']['test_start'][:10]} | {r['window']['test_end'][:10]} | "
            f"{_fmt(r['pf'])} | {_fmt(r['calmar'])} | {_fmt(r['sharpe'])} | "
            f"{_fmt(r['win_rate_pct'])}% | {_fmt(r['max_drawdown_pct'])}% | "
            f"{r['total_trades']} | {_fmt(r['final_pnl_pct'])}% |"
        )
    return (
        "## Per-window results\n\n"
        "| Cfg | # | Test start | Test end | PF | Calmar | Sharpe | WR | DD | n | PnL |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )


def _comparison_section(payload: dict) -> str:
    cmp = payload.get("comparison")
    if not cmp:
        return ""
    delta = cmp["delta_mean"]
    p     = cmp["p"]
    d     = cmp["cohens_d"]
    if abs(delta) >= 0.10 and p < 0.05 and abs(d) > 0.3:
        verdict = "Prefer **C2 (prod actual)**" if delta > 0 else "Prefer **C1 (baseline)**"
    elif p > 0.10:
        verdict = "**Equivalent** — no significant difference"
    else:
        verdict = "**Inconclusive** — significant but not practically meaningful"
    return (
        "## Comparison: C2 vs C1\n\n"
        f"- Δ mean PF (C2 − C1): **{_fmt(delta, 4)}**\n"
        f"- Paired t = {_fmt(cmp['t'], 3)}, df = {cmp['df']}, p = {_fmt(p, 4)}\n"
        f"- Cohen's d = {_fmt(d, 3)}\n"
        f"- N paired windows = {cmp['n']}\n\n"
        f"**Verdict**: {verdict}\n"
    )


def _equity_chart(payload: dict, output_dir: Path) -> Path | None:
    """Write per-config PF over windows as Plotly HTML. Returns relative path."""
    fig = go.Figure()
    for cfg_name in {r["config_name"] for r in payload["results"]}:
        rows = sorted(
            [r for r in payload["results"] if r["config_name"] == cfg_name],
            key=lambda r: r["window"]["index"],
        )
        fig.add_trace(go.Scatter(
            x=[r["window"]["test_start"][:10] for r in rows],
            y=[r["pf"] for r in rows],
            mode="lines+markers",
            name=cfg_name,
        ))
    fig.add_hline(y=1.0, line_dash="dot", line_color="#888", annotation_text="break-even")
    fig.update_layout(
        title="PF per test window — C1 vs C2",
        xaxis_title="Test window start",
        yaxis_title="Profit Factor",
        height=420,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "pf_over_windows.html"
    fig.write_html(html_path, include_plotlyjs="cdn", full_html=True)
    return html_path


def write_markdown_report(payload: dict, md_path: Path) -> None:
    """Write the full audit report to md_path. Charts go in a sibling folder."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    charts_dir = md_path.parent / f"{md_path.stem}_charts"
    chart_path = _equity_chart(payload, charts_dir)

    parts: list[str] = []
    parts.append("# Walk-Forward Validation Audit — Sub-Project A\n")
    parts.append(f"_Generated: {payload['generated']}_\n\n")
    parts.append("**Spec**: `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md`\n\n")

    # TL;DR
    summaries = payload.get("summaries", {})
    cmp       = payload.get("comparison")
    tldr_bits: list[str] = []
    for name, s in summaries.items():
        tldr_bits.append(f"{name}: **{s['verdict']['bucket']}**")
    if cmp:
        tldr_bits.append(f"Δ PF (C2−C1) = {_fmt(cmp['delta_mean'], 3)} (p = {_fmt(cmp['p'], 3)})")
    parts.append("## TL;DR\n\n" + "  ·  ".join(tldr_bits) + "\n\n")

    # Per-config verdicts
    parts.append("## Verdicts\n\n")
    for name, s in summaries.items():
        parts.append(_verdict_section(name, s))
        parts.append("\n")

    # Aggregate tables
    for name, s in summaries.items():
        parts.append(_aggregate_table(name, s))
        parts.append("\n")

    # Comparison
    parts.append(_comparison_section(payload))
    parts.append("\n")

    # Per-window detail
    parts.append(_per_window_table(payload))
    parts.append("\n")

    # Chart link
    if chart_path:
        parts.append(f"## Charts\n\n[PF over windows]({chart_path.relative_to(md_path.parent)})\n\n")

    # Reproduction footer
    args = payload["args"]
    parts.append("## Reproduction\n\n```bash\n")
    parts.append(
        "PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py \\\n"
        f"  --start {args['start']} --end {args['end']} \\\n"
        f"  --train-months {args['train_months']} "
        f"--test-months {args['test_months']} --step-months {args['step_months']}\n"
    )
    parts.append("```\n")

    md_path.write_text("".join(parts), encoding="utf-8")
```

- [ ] **Step 2: Syntax check**

Run: `.venv/bin/python3 -c "import ast; ast.parse(open('bot/audit/report.py').read()); print('OK')"`
Expected: `OK`

- [ ] **Step 3: Full regression (report writer is plotly-dependent — already in requirements)**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: still `370 passed, 1 skipped` (no new tests, no regressions).

- [ ] **Step 4: Commit**

```bash
git add bot/audit/report.py
git commit -m "feat(audit): markdown + Plotly HTML report writer"
```

---

## Task 12: Export public API in `bot/audit/__init__.py`

**Files:**
- Modify: `bot/audit/__init__.py`

- [ ] **Step 1: Add re-exports**

Write to `bot/audit/__init__.py`:

```python
"""Walk-forward validation framework.

Spec: docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md
"""
from bot.audit.walk_forward import (
    WalkForwardConfig,
    Window,
    WindowResult,
    aggregate_metrics,
    extract_window_metrics,
    run_all,
    run_window,
    split_windows,
)
from bot.audit.verdict import VerdictThresholds, evaluate_verdict
from bot.audit.comparison import (
    cohens_d_paired,
    compare_configs,
    paired_t_test,
)
from bot.audit.report import write_markdown_report

__all__ = [
    "WalkForwardConfig",
    "Window",
    "WindowResult",
    "VerdictThresholds",
    "aggregate_metrics",
    "cohens_d_paired",
    "compare_configs",
    "evaluate_verdict",
    "extract_window_metrics",
    "paired_t_test",
    "run_all",
    "run_window",
    "split_windows",
    "write_markdown_report",
]
```

- [ ] **Step 2: Verify imports resolve**

Run: `.venv/bin/python3 -c "from bot.audit import split_windows, run_all, evaluate_verdict, compare_configs, write_markdown_report; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Final full regression**

Run: `.venv/bin/pytest tests/ -q 2>&1 | tail -3`
Expected: `370 passed, 1 skipped` (no change).

- [ ] **Step 4: Commit**

```bash
git add bot/audit/__init__.py
git commit -m "chore(audit): re-export public API from bot/audit package"
```

---

## Task 13: Execute the audit and produce deliverables

**Files:**
- Run: CLI from Task 10
- Outputs: `data/audits/A_walk_forward_<ISO>.json` + `docs/audits/A_walk_forward_<date>.md` + `docs/audits/A_walk_forward_<date>_charts/`

- [ ] **Step 1: Verify all kline cache is present**

Run:
```bash
ls -la data/klines/{BTCUSDT,ETHUSDT}_{4h,1d,1w}.parquet 2>&1 | tail -10
```
Expected: 6 files. If any missing, the runner will auto-fetch on its first call to `fetch_and_cache`.

- [ ] **Step 2: Dry run with --only C1 to confirm the pipeline executes**

Run:
```bash
PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py --only C1 2>&1 | tail -20
```
Expected:
- `Loading klines …`
- One log line per window (`window=0 config=C1 test=…`)
- `Collected N window results`
- `Raw results → data/audits/A_walk_forward_<ISO>.json`
- `Report → docs/audits/A_walk_forward_<date>.md`
- `Done.`

If the run errors out, fix the regression and re-run.

- [ ] **Step 3: Full run with both configs**

Run:
```bash
PYTHONPATH=. .venv/bin/python3 scripts/audit/run_walk_forward.py 2>&1 | tee /tmp/audit_run.log | tail -10
```
Expected runtime: 5-15 min depending on hardware. The final log line is `Done.`

- [ ] **Step 4: Inspect the report**

Read: `docs/audits/A_walk_forward_<date>.md`
Verify:
- TL;DR present with verdicts per config
- Per-config verdict tables present (5 checks each)
- Aggregate tables for C1 and C2
- Comparison section with t-test + Cohen's d
- Per-window table with one row per (config, window)
- Chart link present

- [ ] **Step 5: Sanity-check the numbers vs. CLAUDE.md baseline**

Open `docs/audits/A_walk_forward_<date>.md` and verify:
- C1 mean PF is **directionally** consistent with CLAUDE.md baseline (1.52 over 3 years). A walk-forward mean might be lower (0.8–1.3 is plausible) — that's the audit's purpose.
- If C1 has DD per-window > 35%, flag as a NO-GO and report.

- [ ] **Step 6: Commit the deliverables (Jordi runs)**

```bash
git add data/audits/A_walk_forward_*.json docs/audits/A_walk_forward_*.md docs/audits/A_walk_forward_*_charts/
git commit -m "audit(A): walk-forward results — $(date +%Y-%m-%d)"
```

---

## Task 14: Final cleanup and self-doc

**Files:**
- Modify: `CLAUDE.md` (add audit pointer)

- [ ] **Step 1: Add audit pointer to CLAUDE.md Module Map**

In `CLAUDE.md`, find the "Module Map" table and append a row:

```markdown
| `bot/audit/` | Walk-forward validation framework (sub-project A). Reuses `PortfolioBacktestEngine`. CLI in `scripts/audit/run_walk_forward.py`. Spec: `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md`. Reports under `docs/audits/`. |
```

- [ ] **Step 2: Add a brief Gotcha entry for audit reproducibility**

Append to the "Key Gotchas" section:

```markdown
### 32. Audit configs are SPEC-LOCKED — never tweak mid-run

`scripts/audit/run_walk_forward.py` hardcodes `CONFIG_C1_BASELINE` and `CONFIG_C2_PROD`. These mirror the audit spec (section 5) and MUST NOT drift from production silently — that would invalidate prior reports. If you need to test a different config, add a new constant and a new CLI flag, never mutate C1/C2 in place. The verdict thresholds in `bot/audit/verdict.py` follow the same lock.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(audit): add bot/audit pointer + gotcha #32 (spec-locked configs)"
```

---

## Spec coverage self-review

Verifying each requirement in `docs/superpowers/specs/2026-05-14-walk-forward-audit-design.md` is covered:

| Spec section | Covered by |
|---|---|
| 1-3 Context, goal, non-goals | Plan-level intent (see plan goal + header) |
| 4.1 Per-config verdict thresholds | Task 6 (`evaluate_verdict`) + tests |
| 4.2 Comparative verdict | Task 7 (`compare_configs`) + Task 11 (`_comparison_section`) |
| 5 Frozen configs | Task 10 (`CONFIG_C1_BASELINE`, `CONFIG_C2_PROD`) + Task 14 gotcha |
| 6.1 Window structure | Task 3 (`split_windows`) defaults + Task 10 CLI defaults |
| 6.2 No retraining | Architectural: configs passed as constants to `run_all`, no fitting step exists |
| 6.3 Data window + cache extension | Task 13 step 1 (verify cache); Task 10 `fetch_and_cache` calls auto-extend |
| 6.4 Metric collection | Task 4 (`extract_window_metrics`) — pulls PF, Calmar, Sharpe, WR, DD, n_trades, PnL%, per-symbol |
| 6.5 Aggregate stats | Task 5 (`aggregate_metrics`) — mean/median/std/CI/hit_rate/worst/sparsity |
| 6.6 Comparison stats | Task 7 (`compare_configs`) — paired t-test, Cohen's d |
| 7.1 Module layout | Tasks 1-12 (deviation: metrics.py consolidated into walk_forward.py) |
| 7.2 Reuse boundary | All tasks only read `BacktestConfig` + `PortfolioBacktestEngine` + `fetch_and_cache` |
| 7.3 Public API | Task 12 (`__init__.py` re-exports) |
| 7.4 CLI | Task 10 |
| 8.1 Raw JSON | Task 10 `_to_jsonable` + `json.dump` |
| 8.2 Markdown report | Task 11 (`write_markdown_report`) |
| 8.3 Reusable code | Tasks 2-9 — package designed for B/D plug-in |
| 9.1 Unit tests per module | Tasks 3 / 4 / 5 / 6 / 7 (one test file per module/function group) |
| 9.2 Integration test | Task 8 (`tests/test_walk_forward_runner.py`) |
| 10 Anti-overfitting safeguards | Configs in Task 10 (hardcoded), thresholds in Task 6 (defaults from spec), automated report in Task 11 |
| 11 Risks | Reported by Task 11's report writer (sparsity flag + worst-DD column); survivorship documented in spec, not enforced in code |
| 13 Definition of Done | All TDD tests green (after Task 12), CLI runnable (Task 13), report generated (Task 13), JSON persisted (Task 13) |
| 15 Roadmap | This plan completes the "Implementation plan" + "Implementation" + "Run audit" rows |

**No spec gaps detected.**

**Deviation noted:** spec section 7.1 listed a separate `bot/audit/metrics.py` module. Plan consolidates metric extraction into `walk_forward.py`. Files-that-change-together-live-together rule. Test file naming kept flat per project convention.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-05-14-walk-forward-audit.md`.

Two execution options:

1. **Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session with batch checkpoints

Which approach?
