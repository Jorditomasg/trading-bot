"""T21 [TEST] — Failing tests for bot/audit/kill_switch.py.

TDD: these tests MUST be RED before T22 implements the module.

REQ-ADX-4 (audit layer) coverage:
  - evaluate_adx_kill_switch triggers when total_trades < min_trades
  - evaluate_adx_kill_switch clears (not triggered) when total_trades >= min_trades
  - returns a dict with the expected keys
  - uses rolling window of results (most-recent windows, not all)
  - min_trades kwarg is respected
  - single window result: trades counted from that window alone
"""
from __future__ import annotations

from datetime import datetime, timezone


def _make_window_result(
    window_index: int,
    total_trades: int,
    config_name: str = "C3",
) -> object:
    """Build a minimal WindowResult for kill-switch testing."""
    from bot.audit.walk_forward import Window, WindowResult

    base_year = 2023
    month = (window_index % 11) + 1
    test_start = datetime(base_year, month, 1, tzinfo=timezone.utc)
    train_start = datetime(base_year - 1, month, 1, tzinfo=timezone.utc)
    w = Window(
        index=window_index,
        train_start=train_start,
        train_end=test_start,
        test_start=test_start,
        test_end=datetime(base_year, month + 1 if month < 12 else 1, 1, tzinfo=timezone.utc),
    )
    return WindowResult(
        window=w, config_name=config_name,
        pf=1.4, calmar=2.0, sharpe=1.0,
        win_rate_pct=40.0, max_drawdown_pct=15.0,
        total_trades=total_trades, final_pnl_pct=5.0,
    )


class TestEvaluateAdxKillSwitch:
    def test_returns_dict_with_required_keys(self) -> None:
        """evaluate_adx_kill_switch must return a dict with triggered, total_trades, threshold."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(0, total_trades=40)]
        verdict = evaluate_adx_kill_switch(results)

        assert isinstance(verdict, dict), "Must return a dict"
        assert "triggered" in verdict, "Must have 'triggered' key"
        assert "total_trades" in verdict, "Must have 'total_trades' key"
        assert "threshold" in verdict, "Must have 'threshold' key"

    def test_triggered_when_below_min_trades(self) -> None:
        """triggered=True when sum of total_trades across recent windows < min_trades."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        # 2 windows with 12 + 10 = 22 trades < 30 (default min_trades)
        results = [
            _make_window_result(0, total_trades=12),
            _make_window_result(1, total_trades=10),
        ]
        verdict = evaluate_adx_kill_switch(results)
        assert verdict["triggered"] is True, (
            f"Expected triggered=True with {verdict['total_trades']} trades < 30"
        )
        assert verdict["total_trades"] == 22

    def test_not_triggered_when_above_min_trades(self) -> None:
        """triggered=False when sum of total_trades >= min_trades."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        # 3 windows with 12 + 10 + 15 = 37 trades >= 30
        results = [
            _make_window_result(0, total_trades=12),
            _make_window_result(1, total_trades=10),
            _make_window_result(2, total_trades=15),
        ]
        verdict = evaluate_adx_kill_switch(results)
        assert verdict["triggered"] is False, (
            f"Expected triggered=False with {verdict['total_trades']} trades >= 30"
        )
        assert verdict["total_trades"] == 37

    def test_exact_boundary_not_triggered(self) -> None:
        """Exactly min_trades → triggered=False (>= is not triggered)."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(0, total_trades=30)]
        verdict = evaluate_adx_kill_switch(results, min_trades=30)
        assert verdict["triggered"] is False
        assert verdict["total_trades"] == 30

    def test_exactly_one_below_boundary_triggered(self) -> None:
        """min_trades - 1 trades → triggered=True."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(0, total_trades=29)]
        verdict = evaluate_adx_kill_switch(results, min_trades=30)
        assert verdict["triggered"] is True
        assert verdict["total_trades"] == 29

    def test_min_trades_kwarg_respected(self) -> None:
        """Custom min_trades overrides the default 30."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(0, total_trades=10)]
        # With min_trades=5, 10 trades is enough → not triggered
        verdict = evaluate_adx_kill_switch(results, min_trades=5)
        assert verdict["triggered"] is False

        # With min_trades=15, 10 trades is too few → triggered
        verdict2 = evaluate_adx_kill_switch(results, min_trades=15)
        assert verdict2["triggered"] is True

    def test_empty_results_is_triggered(self) -> None:
        """No results at all → 0 trades < any threshold → triggered=True."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        verdict = evaluate_adx_kill_switch([])
        assert verdict["triggered"] is True
        assert verdict["total_trades"] == 0

    def test_threshold_matches_min_trades_param(self) -> None:
        """verdict['threshold'] must equal the min_trades kwarg used."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(0, total_trades=40)]
        verdict = evaluate_adx_kill_switch(results, min_trades=25)
        assert verdict["threshold"] == 25

    def test_many_windows_sums_all_trades(self) -> None:
        """Sum covers all provided windows (no hidden truncation by default)."""
        from bot.audit.kill_switch import evaluate_adx_kill_switch

        results = [_make_window_result(i, total_trades=5) for i in range(10)]
        # 10 × 5 = 50 trades >= 30
        verdict = evaluate_adx_kill_switch(results)
        assert verdict["triggered"] is False
        assert verdict["total_trades"] == 50
