"""T12 — Tests for C3_LIVE audit constant and C1/C2 regression guard.
T19 — Tests for regime tagging in walk_forward (REQ-REGIME-1, REQ-REGIME-2).

TDD: test_c3_live_constant_matches_live_seed and test_c1_c2_constants_unchanged
must FAIL before T13 implementation.

T19 tests must FAIL before T20 wires regime_classifier into walk_forward.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest


class TestC3LiveConstant:
    def test_c3_live_constant_matches_live_seed(self) -> None:
        """CONFIG_C3_LIVE must exist and have the correct live-seeded values.

        This guards REQ-PARITY-5: the C3_LIVE audit constant must reflect the
        Phase 1 production config (0.08 neutral band, 5.0 TP, 1.5 SL, 1.5% risk).
        """
        from scripts.audit.run_walk_forward import CONFIG_C3_LIVE

        assert CONFIG_C3_LIVE.momentum_neutral_band == 0.08, (
            f"C3_LIVE must use 0.08 momentum_neutral_band (live default), "
            f"got {CONFIG_C3_LIVE.momentum_neutral_band}"
        )
        assert CONFIG_C3_LIVE.ema_tp_mult == 5.0, (
            f"C3_LIVE must use ema_tp_mult=5.0 (B-pick audit), got {CONFIG_C3_LIVE.ema_tp_mult}"
        )
        assert CONFIG_C3_LIVE.ema_stop_mult == 1.5, (
            f"C3_LIVE must use ema_stop_mult=1.5, got {CONFIG_C3_LIVE.ema_stop_mult}"
        )
        assert CONFIG_C3_LIVE.risk_per_trade == 0.015, (
            f"C3_LIVE must use risk_per_trade=0.015 (1.5%), got {CONFIG_C3_LIVE.risk_per_trade}"
        )

    def test_c1_c2_constants_unchanged(self) -> None:
        """CONFIG_C1_BASELINE and CONFIG_C2_PROD must still use momentum_neutral_band=0.05.

        Regression guard per gotcha #32: spec-locked configs must NEVER drift.
        """
        from scripts.audit.run_walk_forward import CONFIG_C1_BASELINE, CONFIG_C2_PROD

        assert CONFIG_C1_BASELINE.momentum_neutral_band == 0.05, (
            f"C1 is spec-locked at 0.05; got {CONFIG_C1_BASELINE.momentum_neutral_band}"
        )
        assert CONFIG_C2_PROD.momentum_neutral_band == 0.05, (
            f"C2 is spec-locked at 0.05; got {CONFIG_C2_PROD.momentum_neutral_band}"
        )


# ── T19: Walk-forward regime tagging tests ─────────────────────────────────────


def _make_window_result(
    window_index: int,
    config_name: str = "C3",
    pf: float = 1.4,
    calmar: float = 2.0,
    regime_label=None,
) -> object:
    """Build a WindowResult with optional regime_label for testing."""
    from bot.audit.walk_forward import Window, WindowResult

    test_start = datetime(2024, 1 + window_index % 12, 1, tzinfo=timezone.utc)
    w = Window(
        index=window_index,
        train_start=datetime(2023, 10, 1, tzinfo=timezone.utc),
        train_end=test_start,
        test_start=test_start,
        test_end=datetime(2024, 4 + window_index % 9, 1, tzinfo=timezone.utc),
    )
    kwargs = dict(
        window=w, config_name=config_name,
        pf=pf, calmar=calmar, sharpe=1.0,
        win_rate_pct=40.0, max_drawdown_pct=15.0,
        total_trades=30, final_pnl_pct=5.0,
    )
    if regime_label is not None:
        kwargs["regime_label"] = regime_label
    return WindowResult(**kwargs)


class TestWindowResultRegimeField:
    def test_window_result_has_regime_label_field(self) -> None:
        """REQ-REGIME-1: WindowResult must have regime_label field defaulting to None."""
        from bot.audit.walk_forward import WindowResult, Window

        w = Window(
            index=0,
            train_start=datetime(2023, 1, 1, tzinfo=timezone.utc),
            train_end=datetime(2024, 1, 1, tzinfo=timezone.utc),
            test_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            test_end=datetime(2024, 4, 1, tzinfo=timezone.utc),
        )
        result = WindowResult(
            window=w, config_name="C3",
            pf=1.4, calmar=2.0, sharpe=1.0,
            win_rate_pct=40.0, max_drawdown_pct=15.0,
            total_trades=30, final_pnl_pct=5.0,
        )
        # Must default to None for backward compat
        assert hasattr(result, "regime_label"), "WindowResult must have regime_label field"
        assert result.regime_label is None

    def test_window_result_accepts_regime_label_bull(self) -> None:
        """WindowResult must accept RegimeLabel values."""
        from bot.audit.regime_classifier import RegimeLabel
        from bot.audit.walk_forward import Window, WindowResult

        w = Window(
            index=0,
            train_start=datetime(2023, 1, 1, tzinfo=timezone.utc),
            train_end=datetime(2024, 1, 1, tzinfo=timezone.utc),
            test_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            test_end=datetime(2024, 4, 1, tzinfo=timezone.utc),
        )
        result = WindowResult(
            window=w, config_name="C3",
            pf=1.4, calmar=2.0, sharpe=1.0,
            win_rate_pct=40.0, max_drawdown_pct=15.0,
            total_trades=30, final_pnl_pct=5.0,
            regime_label=RegimeLabel.BULL,
        )
        assert result.regime_label == RegimeLabel.BULL
        assert result.regime_label == "BULL"  # str comparison per (str, Enum) convention


class TestAggregateByRegime:
    def test_aggregate_by_regime_false_returns_flat_dict(self) -> None:
        """aggregate_metrics(results, by_regime=False) must return the existing flat dict."""
        from bot.audit.walk_forward import aggregate_metrics
        from bot.audit.regime_classifier import RegimeLabel

        results = [
            _make_window_result(0, regime_label=RegimeLabel.BULL),
            _make_window_result(1, regime_label=RegimeLabel.BEAR),
            _make_window_result(2, regime_label=RegimeLabel.FLAT),
        ]
        agg = aggregate_metrics(results, by_regime=False)
        # Must have top-level keys (existing format)
        assert "pf" in agg
        assert "calmar" in agg
        # Must NOT have a by_regime key when by_regime=False
        assert "by_regime" not in agg

    def test_aggregate_by_regime_true_returns_regime_breakdown(self) -> None:
        """REQ-REGIME-2: aggregate_metrics(results, by_regime=True) must return 'by_regime' key."""
        from bot.audit.walk_forward import aggregate_metrics
        from bot.audit.regime_classifier import RegimeLabel

        bull_results = [_make_window_result(i, pf=1.6, calmar=3.0, regime_label=RegimeLabel.BULL)
                        for i in range(3)]
        bear_results = [_make_window_result(i + 3, pf=0.9, calmar=-1.0, regime_label=RegimeLabel.BEAR)
                        for i in range(2)]
        flat_results = [_make_window_result(i + 5, pf=1.2, calmar=1.5, regime_label=RegimeLabel.FLAT)
                        for i in range(2)]
        all_results  = bull_results + bear_results + flat_results

        agg = aggregate_metrics(all_results, by_regime=True)
        assert "by_regime" in agg, "by_regime key must be present when by_regime=True"

        by_regime = agg["by_regime"]
        assert "BULL" in by_regime, "BULL bucket must appear"
        assert "BEAR" in by_regime, "BEAR bucket must appear"
        assert "FLAT" in by_regime, "FLAT bucket must appear"

        # Each bucket must have pf and calmar sub-aggregates
        assert "pf" in by_regime["BULL"]
        assert "calmar" in by_regime["BULL"]
        # Mean PF for BULL windows should be ~1.6
        assert by_regime["BULL"]["pf"]["mean"] == pytest.approx(1.6, abs=1e-6)

    def test_aggregate_by_regime_missing_label_counted_as_none(self) -> None:
        """Windows with regime_label=None must not crash; they appear under a 'UNKNOWN' key or
        are simply excluded from the by_regime breakdown (implementation choice: exclude)."""
        from bot.audit.walk_forward import aggregate_metrics

        results = [
            _make_window_result(0, pf=1.4, regime_label=None),   # no label
            _make_window_result(1, pf=1.5, regime_label=None),
        ]
        # Must not raise regardless of regime_label being None
        agg = aggregate_metrics(results, by_regime=True)
        assert "by_regime" in agg
        # No label → either empty dict or UNKNOWN bucket; must not raise

    def test_aggregate_by_regime_default_is_false(self) -> None:
        """aggregate_metrics signature default must be by_regime=False (backward compat)."""
        import inspect
        from bot.audit.walk_forward import aggregate_metrics

        sig = inspect.signature(aggregate_metrics)
        param = sig.parameters.get("by_regime")
        assert param is not None, "aggregate_metrics must accept by_regime param"
        assert param.default is False
