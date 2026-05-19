"""Runtime parity tests — dashboard backtest ↔ live bot.

These tests catch the class of bugs that gotchas #36 and #37 belonged to:
the dashboard's `BacktestConfig` builder reads DB `bot_config` keys, and the
live bot's `_apply_ema_config` reads the SAME keys. When the two paths
disagree on key names or fallback values, the dashboard reports trades the
live bot would never take (and vice versa).

The existing `test_dashboard_parity.py` uses AST inspection — it catches
literal key-name regressions but cannot catch semantic divergence (e.g.
warmup buffers being too short, like gotcha #37). These tests run the actual
config-building functions and compare runtime values.

Test surface:
- `test_dashboard_config_matches_live_ema_config`: assert every shared key
  produces equal values in the live `EMACrossoverConfig` and the
  `BacktestEngine`-built one. THIS is the test that would have caught #36.
- `test_fetch_plan_applies_filter_warmup`: assert `build_fetch_plan` widens
  the bias/weekly fetch windows by the documented buffers. THIS is what
  would have caught #37 at the config-mapping layer.
- `test_seeded_db_drives_all_critical_keys`: assert `_seed_optimized_defaults`
  covers every key the dashboard config builder reads — guards against the
  next "we forgot to seed X" bug.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.backtest.engine import BacktestConfig, BacktestEngine
from bot.backtest.portfolio_runner import (
    BIAS_WARMUP,
    MOMENTUM_WARMUP,
    BacktestRequest,
    build_backtest_config,
    build_fetch_plan,
    run_portfolio_backtest_core,
)
from bot.constants import StrategyName
from bot.orchestrator import StrategyOrchestrator
from bot.risk.manager import RiskConfig
from main import _apply_ema_config

from tests.conftest import uptrend


# DB-key → (live EMACrossoverConfig attr, backtest EMACrossoverConfig attr).
# Both columns name the SAME field on the SAME class — they only differ when
# the live or backtest path has a renaming/typo bug. Most pairs are identical.
_EMA_PARITY_FIELDS: dict[str, str] = {
    "ema_stop_mult":      "stop_atr_mult",
    "ema_tp_mult":        "tp_atr_mult",
    "ema_max_dist_atr":   "max_distance_atr",
    "ema_vol_mult":       "volume_multiplier",
    "ema_min_atr":        "min_atr_pct",
    "ema_bar_dir":        "require_bar_direction",
    "ema_momentum_req":   "require_ema_momentum",
    "long_only":          "long_only",
    "ema_min_entry_adx":  "min_entry_adx",
    "ema_require_ema200": "require_ema200_alignment",
}


def _make_request(symbols=("BTCUSDT",), use_bias=True, use_momentum=True, use_1m=False):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return BacktestRequest(
        symbols       = symbols,
        timeframe     = "4h",
        start_dt      = start,
        end_dt        = start + timedelta(days=180),
        capital       = 10_000.0,
        risk          = 0.015,
        cost_per_side = 0.001,
        use_bias      = use_bias,
        use_momentum  = use_momentum,
        use_1m        = use_1m,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Config parity (would have caught gotcha #36)
# ─────────────────────────────────────────────────────────────────────────────


class TestRuntimeConfigParity:
    def test_dashboard_config_matches_live_ema_config(self, seeded_db) -> None:
        """For every shared DB key, dashboard's BacktestConfig path and live's
        `_apply_ema_config` path must produce equal values on the EMA strategy.
        """
        runtime_cfg = seeded_db.get_runtime_config()

        # ── Live path ────────────────────────────────────────────────────────
        orch = StrategyOrchestrator(
            db=seeded_db, symbol="BTCUSDT", risk_config=RiskConfig(), timeframe="4h",
        )
        _apply_ema_config(seeded_db, orch)
        live_cfg = orch.get_strategy(StrategyName.EMA_CROSSOVER).config

        # ── Dashboard path ───────────────────────────────────────────────────
        req = _make_request()
        bt_cfg = build_backtest_config(req, runtime_cfg)
        bt_engine = BacktestEngine(bt_cfg)
        dash_cfg = bt_engine._strategies[StrategyName.EMA_CROSSOVER].config

        # ── Compare ──────────────────────────────────────────────────────────
        mismatches = []
        for db_key, attr in _EMA_PARITY_FIELDS.items():
            live_val = getattr(live_cfg, attr)
            dash_val = getattr(dash_cfg, attr)
            if live_val != dash_val:
                mismatches.append(
                    f"  {db_key} → {attr}: live={live_val!r}  dashboard={dash_val!r}"
                )
        assert not mismatches, (
            "Dashboard backtest config diverges from live EMA strategy.\n"
            "This is the class of bug gotchas #36/#37 belonged to.\n"
            + "\n".join(mismatches)
        )

    def test_seeded_db_drives_all_critical_keys(self, seeded_db) -> None:
        """Every key the dashboard config builder reads must be in the seed.

        If `_seed_optimized_defaults` forgets to seed a key, the dashboard
        falls back to its hardcoded literal — exactly the failure mode of
        gotcha #36 (ema_min_atr fallback was 0.0 vs the 4h preset 0.005).
        Live, in turn, reads from the 4h preset, so the two diverge silently.
        """
        cfg = seeded_db.get_runtime_config()
        missing = [k for k in _EMA_PARITY_FIELDS.keys() if k not in cfg]
        # `long_only` is shared so it's expected to be in the seed too.
        assert not missing, (
            "Seeded DB is missing keys the dashboard backtest reads:\n  "
            + "\n  ".join(missing)
            + "\nAdd them to _seed_optimized_defaults() in main.py."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fetch-plan warmup parity (would have caught gotcha #37)
# ─────────────────────────────────────────────────────────────────────────────


class TestFetchPlanWarmup:
    def test_bias_fetch_window_includes_warmup(self) -> None:
        """Bias filter needs ≥22 daily bars before backtest_start (gotcha #37).
        Without warmup the dashboard would silently disable BiasFilter for the
        first ~3 weeks of the backtest period.
        """
        req = _make_request(use_bias=True, use_momentum=False)
        plan = build_fetch_plan(req)
        assert len(plan.bias) == 1
        bias_job = plan.bias[0]
        assert bias_job.start == req.start_dt - BIAS_WARMUP, (
            f"Bias fetch must start at backtest_start - BIAS_WARMUP ({BIAS_WARMUP}). "
            f"Got: {bias_job.start} (gap: {req.start_dt - bias_job.start})"
        )
        assert bias_job.end == req.end_dt

    def test_weekly_fetch_window_includes_warmup(self) -> None:
        """Momentum filter needs ≥21 weekly bars before backtest_start
        (gotcha #37). Without warmup it falls into BULLISH (fail-open) for
        ~5 months of a 6-month backtest.
        """
        req = _make_request(use_bias=False, use_momentum=True)
        plan = build_fetch_plan(req)
        assert len(plan.weekly) == 1
        weekly_job = plan.weekly[0]
        assert weekly_job.start == req.start_dt - MOMENTUM_WARMUP, (
            f"Weekly fetch must start at backtest_start - MOMENTUM_WARMUP "
            f"({MOMENTUM_WARMUP}). Got: {weekly_job.start} "
            f"(gap: {req.start_dt - weekly_job.start})"
        )
        assert weekly_job.end == req.end_dt

    def test_primary_fetch_does_not_apply_warmup(self) -> None:
        """The primary timeframe gets no warmup — `BacktestEngine` handles its
        own indicator warmup internally (ATR/EMA on primary). Adding warmup
        here would silently mis-align the equity-curve start date.
        """
        req = _make_request()
        plan = build_fetch_plan(req)
        assert len(plan.primary) == 1
        primary_job = plan.primary[0]
        assert primary_job.start == req.start_dt
        assert primary_job.end == req.end_dt

    def test_warmup_constants_meet_filter_requirements(self) -> None:
        """Sanity floor: a regression that shortens BIAS_WARMUP or MOMENTUM_WARMUP
        below the filter requirements would silently re-introduce gotcha #37.
        """
        # BiasFilter needs 22 daily bars; 30 days gives ~30 daily bars + safety
        assert BIAS_WARMUP >= timedelta(days=22), (
            f"BIAS_WARMUP {BIAS_WARMUP} is below the 22-day floor needed by BiasFilter"
        )
        # MomentumFilter needs 21 weekly bars = 147 days
        assert MOMENTUM_WARMUP >= timedelta(days=147), (
            f"MOMENTUM_WARMUP {MOMENTUM_WARMUP} is below the 147-day floor needed by MomentumFilter"
        )


# ─────────────────────────────────────────────────────────────────────────────
# FetchPlan composition
# ─────────────────────────────────────────────────────────────────────────────


class TestFetchPlanComposition:
    def test_no_bias_jobs_when_bias_disabled(self) -> None:
        plan = build_fetch_plan(_make_request(use_bias=False, use_momentum=True))
        assert plan.bias == ()
        assert plan.weekly != ()

    def test_no_weekly_jobs_when_momentum_disabled(self) -> None:
        plan = build_fetch_plan(_make_request(use_bias=True, use_momentum=False))
        assert plan.weekly == ()
        assert plan.bias != ()

    def test_no_1m_jobs_when_1m_disabled(self) -> None:
        plan = build_fetch_plan(_make_request(use_1m=False))
        assert plan.minute == ()

    def test_1m_jobs_when_1m_enabled(self) -> None:
        plan = build_fetch_plan(_make_request(use_1m=True))
        assert len(plan.minute) == 1
        assert plan.minute[0].interval == "1m"

    def test_per_symbol_fanout(self) -> None:
        req = _make_request(symbols=("BTCUSDT", "ETHUSDT"))
        plan = build_fetch_plan(req)
        assert len(plan.primary) == 2
        assert {j.symbol for j in plan.primary} == {"BTCUSDT", "ETHUSDT"}
        assert len(plan.bias) == 2
        assert len(plan.weekly) == 2

    def test_bias_timeframe_derived_from_primary(self) -> None:
        # 4h primary → 1d bias (per BIAS_TIMEFRAME_MAP)
        req = _make_request()
        plan = build_fetch_plan(req)
        assert plan.bias[0].interval == "1d"

    def test_weekly_jobs_always_use_1w(self) -> None:
        plan = build_fetch_plan(_make_request())
        for job in plan.weekly:
            assert job.interval == "1w"


# ─────────────────────────────────────────────────────────────────────────────
# Runtime config fallback parity (sanity)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Runner end-to-end integration with a fake fetcher
# ─────────────────────────────────────────────────────────────────────────────


class TestRunnerEndToEndWithFakeFetcher:
    """Drive `run_portfolio_backtest_core` end-to-end with synthetic data.

    Uses an injected fake fetcher so the test runs offline — no network, no
    parquet cache. This locks in the contract: a non-trivial request produces
    a result, the engine actually runs, and the wiring between fetch → config →
    engine doesn't silently swallow exceptions. The shadow-run live-equivalence
    test (P1.4 full version) is a separate, heavier piece of work.
    """

    @staticmethod
    def _fake_fetcher(symbol: str, interval: str, start, end):
        # Return enough bars for the engine to run a meaningful sim regardless
        # of which timeframe the runner asks for. The engine's internal lookback
        # (≥150) drives the floor — we add headroom for filter warmups.
        return uptrend(n=400, start_price=40_000.0, end_price=50_000.0)

    def test_runner_returns_result_with_synthetic_data(self) -> None:
        req = BacktestRequest(
            symbols       = ("BTCUSDT",),
            timeframe     = "4h",
            start_dt      = datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_dt        = datetime(2024, 6, 30, tzinfo=timezone.utc),
            capital       = 10_000.0,
            risk          = 0.015,
            cost_per_side = 0.001,
            use_bias      = True,
            use_momentum  = True,
            use_1m        = False,
        )
        progress_msgs: list[str] = []
        warnings_seen: list[tuple[str, str]] = []
        result = run_portfolio_backtest_core(
            req, runtime_cfg={},
            fetcher=self._fake_fetcher,
            on_progress=progress_msgs.append,
            on_warning=lambda s, m: warnings_seen.append((s, m)),
        )
        assert result is not None, "Runner should produce a result with sufficient synthetic data"
        assert result.symbols == ["BTCUSDT"]
        assert result.timeframe == "4h"
        assert result.initial_capital == 10_000.0
        # No warnings expected when every fetch succeeds.
        assert warnings_seen == []
        # Progress callback was wired correctly.
        assert any("BTCUSDT" in m for m in progress_msgs)

    def test_runner_returns_none_when_primary_fetch_fails(self) -> None:
        """If the primary timeframe fetch fails, the runner must abort cleanly."""
        req = BacktestRequest(
            symbols       = ("BTCUSDT",),
            timeframe     = "4h",
            start_dt      = datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_dt        = datetime(2024, 6, 30, tzinfo=timezone.utc),
            capital       = 10_000.0,
            risk          = 0.015,
            cost_per_side = 0.001,
            use_bias      = False,
            use_momentum  = False,
            use_1m        = False,
        )

        def boom(*args, **kwargs):
            raise RuntimeError("network is down")

        warnings_seen: list[tuple[str, str]] = []
        result = run_portfolio_backtest_core(
            req, runtime_cfg={},
            fetcher=boom,
            on_warning=lambda s, m: warnings_seen.append((s, m)),
        )
        assert result is None
        # Exactly one warning per failing symbol.
        assert len(warnings_seen) == 1
        sym, msg = warnings_seen[0]
        assert sym == "BTCUSDT"
        assert "network is down" in msg

    def test_runner_continues_when_bias_fetch_fails(self) -> None:
        """Bias fetch is fail-soft per symbol — runner must continue, set None."""
        call_log: list[tuple[str, str]] = []

        def selective(symbol, interval, start, end):
            call_log.append((symbol, interval))
            if interval == "1d":
                raise RuntimeError("bias data missing")
            return uptrend(n=400)

        req = BacktestRequest(
            symbols       = ("BTCUSDT",),
            timeframe     = "4h",
            start_dt      = datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_dt        = datetime(2024, 6, 30, tzinfo=timezone.utc),
            capital       = 10_000.0,
            risk          = 0.015,
            cost_per_side = 0.001,
            use_bias      = True,
            use_momentum  = False,
            use_1m        = False,
        )

        warnings_seen: list[tuple[str, str]] = []
        result = run_portfolio_backtest_core(
            req, runtime_cfg={},
            fetcher=selective,
            on_warning=lambda s, m: warnings_seen.append((s, m)),
        )
        assert result is not None, "Runner should still produce a result when bias fetch fails"
        # Primary AND bias fetch were attempted, in that order.
        assert call_log[0] == ("BTCUSDT", "4h")
        assert ("BTCUSDT", "1d") in call_log
        # One warning for bias failure.
        assert len(warnings_seen) == 1
        assert "BiasFilter pass-through" in warnings_seen[0][1]


class TestBuildBacktestConfigFallbacks:
    """Empty runtime_cfg → build_backtest_config must use the documented baseline."""

    def test_empty_runtime_uses_validated_baseline(self) -> None:
        cfg = build_backtest_config(_make_request(), runtime_cfg={})
        # Validated baseline (CLAUDE.md → Validated Baseline section)
        assert cfg.ema_stop_mult        == 1.5
        assert cfg.ema_tp_mult          == 5.0
        assert cfg.ema_max_distance_atr == 1.0
        assert cfg.long_only is True
        assert cfg.ema_volume_mult      == 1.5
        assert cfg.ema_require_momentum is True
        assert cfg.ema_require_bar_dir  is True
        assert cfg.ema_min_atr_pct      == 0.005
        # Phase 2 — defaults off
        assert cfg.ema_min_entry_adx            == 0.0
        assert cfg.ema_require_ema200_alignment is False
