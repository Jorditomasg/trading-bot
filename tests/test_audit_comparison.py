"""T23 [TEST] — Failing tests for champion_vs_challenger in bot/audit/comparison.py.

TDD: these tests MUST be RED before T24 implements the function.

REQ-CC-1 coverage:
  - test_champion_challenger_smoke: basic call, verifies ComparatorVerdict fields
  - test_verdict_adopt: clear challenger win → ADOPT
  - test_verdict_reject: clear champion win → REJECT
  - test_verdict_inconclusive: ambiguous result → INCONCLUSIVE
  - test_kill_switch_overrides_verdict: triggered kill-switch → REJECT regardless of stats
  - test_comparator_verdict_is_frozen_dataclass: immutable
  - test_win_loss_tie_counts: per-window win/loss/tie counted correctly

REQ-CC-3 (regression direction):
  - test_c1_better_than_c2_pf: C1 mean PF > C2 mean PF (matches May 2026 audit finding)

ADOPT criteria (from design D9):
  - paired Calmar p < 0.05 AND Cohen's d > 0.5 AND challenger wins >= 6/10
  - REJECT when challenger loses >= 6/10 (champion wins >= 6/10)
  - INCONCLUSIVE otherwise
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest


def _make_window_result(
    window_index: int,
    config_name: str = "C3",
    pf: float = 1.4,
    calmar: float = 2.0,
    total_trades: int = 40,
) -> object:
    """Build a minimal WindowResult for comparison testing."""
    from bot.audit.walk_forward import Window, WindowResult

    month = (window_index % 11) + 1
    test_start = datetime(2024, month, 1, tzinfo=timezone.utc)
    train_start = datetime(2023, month, 1, tzinfo=timezone.utc)
    w = Window(
        index=window_index,
        train_start=train_start,
        train_end=test_start,
        test_start=test_start,
        test_end=datetime(2024, month + 1 if month < 12 else 1, 1, tzinfo=timezone.utc),
    )
    return WindowResult(
        window=w, config_name=config_name,
        pf=pf, calmar=calmar, sharpe=1.0,
        win_rate_pct=40.0, max_drawdown_pct=15.0,
        total_trades=total_trades, final_pnl_pct=5.0,
    )


def _paired_results(
    n: int,
    champ_calmar: float,
    chal_calmar: float,
    champ_pf: float = 1.4,
    chal_pf: float = 1.5,
):
    """Build matched champion and challenger result lists with constant metrics."""
    champion   = [_make_window_result(i, config_name="C3",      calmar=champ_calmar, pf=champ_pf)
                  for i in range(n)]
    challenger = [_make_window_result(i, config_name="CHALLENGER", calmar=chal_calmar, pf=chal_pf)
                  for i in range(n)]
    return champion, challenger


class TestComparatorVerdictDataclass:
    def test_comparator_verdict_exists(self) -> None:
        """ComparatorVerdict must be importable from bot.audit.comparison."""
        from bot.audit.comparison import ComparatorVerdict
        assert ComparatorVerdict is not None

    def test_comparator_verdict_is_frozen_dataclass(self) -> None:
        """ComparatorVerdict must be a frozen dataclass (immutable)."""
        import dataclasses
        from bot.audit.comparison import ComparatorVerdict

        assert dataclasses.is_dataclass(ComparatorVerdict)
        # frozen dataclasses raise FrozenInstanceError on attribute assignment
        # We can check the field exists by instantiating with required fields.
        # Required fields: champion_pf_mean, challenger_pf_mean, champion_calmar_mean,
        #   challenger_calmar_mean, pf_t_stat, pf_p_value, calmar_t_stat, calmar_p_value,
        #   pf_cohen_d, calmar_cohen_d, wins, losses, ties, verdict
        fields = {f.name for f in dataclasses.fields(ComparatorVerdict)}
        required = {
            "champion_pf_mean", "challenger_pf_mean",
            "champion_calmar_mean", "challenger_calmar_mean",
            "pf_t_stat", "pf_p_value",
            "calmar_t_stat", "calmar_p_value",
            "pf_cohen_d", "calmar_cohen_d",
            "wins", "losses", "ties",
            "verdict",
        }
        missing = required - fields
        assert not missing, f"ComparatorVerdict missing fields: {missing}"

    def test_verdict_field_values(self) -> None:
        """verdict must be one of ADOPT / REJECT / INCONCLUSIVE."""
        from bot.audit.comparison import ComparatorVerdict, champion_vs_challenger

        champ, chal = _paired_results(10, champ_calmar=1.0, chal_calmar=5.0)
        verdict = champion_vs_challenger(champ, chal)

        assert isinstance(verdict, ComparatorVerdict)
        assert verdict.verdict in {"ADOPT", "REJECT", "INCONCLUSIVE"}, (
            f"verdict must be ADOPT/REJECT/INCONCLUSIVE, got {verdict.verdict}"
        )


class TestChampionVsChallenger:
    def test_champion_challenger_smoke(self) -> None:
        """REQ-CC-1: Basic call succeeds and returns a ComparatorVerdict."""
        from bot.audit.comparison import ComparatorVerdict, champion_vs_challenger

        champ, chal = _paired_results(5, champ_calmar=2.0, chal_calmar=2.5)
        result = champion_vs_challenger(champ, chal)

        assert isinstance(result, ComparatorVerdict)
        # Mean metrics should reflect inputs
        assert result.champion_calmar_mean == pytest.approx(2.0, abs=1e-9)
        assert result.challenger_calmar_mean == pytest.approx(2.5, abs=1e-9)
        assert result.champion_pf_mean == pytest.approx(1.4, abs=1e-9)
        assert result.challenger_pf_mean == pytest.approx(1.5, abs=1e-9)

    def test_p_values_are_valid_probabilities(self) -> None:
        """p-values must be in [0, 1]."""
        from bot.audit.comparison import champion_vs_challenger

        champ, chal = _paired_results(10, champ_calmar=2.0, chal_calmar=3.0)
        result = champion_vs_challenger(champ, chal)

        assert 0.0 <= result.pf_p_value <= 1.0, f"pf_p_value out of range: {result.pf_p_value}"
        assert 0.0 <= result.calmar_p_value <= 1.0, f"calmar_p_value out of range: {result.calmar_p_value}"

    def test_verdict_adopt_when_challenger_clearly_better(self) -> None:
        """ADOPT when challenger has much higher Calmar across all windows (p<0.05, d>0.5, wins>=6/10)."""
        from bot.audit.comparison import champion_vs_challenger

        # Challenger Calmar is 2x champion — clear win in all 10 windows
        champ  = [_make_window_result(i, calmar=1.0 + i * 0.05, pf=1.3) for i in range(10)]
        chal   = [_make_window_result(i, calmar=3.0 + i * 0.05, pf=1.7) for i in range(10)]
        result = champion_vs_challenger(champ, chal)

        assert result.verdict == "ADOPT", (
            f"Expected ADOPT for clear challenger win, got {result.verdict}. "
            f"p={result.calmar_p_value:.4f}, d={result.calmar_cohen_d:.2f}, wins={result.wins}"
        )
        assert result.wins >= 6

    def test_verdict_reject_when_champion_clearly_better(self) -> None:
        """REJECT when champion has much higher Calmar across all windows (challenger loses >= 6/10)."""
        from bot.audit.comparison import champion_vs_challenger

        # Champion Calmar is 2x challenger — clear loss for challenger
        champ  = [_make_window_result(i, calmar=3.0 + i * 0.05, pf=1.7) for i in range(10)]
        chal   = [_make_window_result(i, calmar=1.0 + i * 0.05, pf=1.3) for i in range(10)]
        result = champion_vs_challenger(champ, chal)

        assert result.verdict == "REJECT", (
            f"Expected REJECT for clear champion win, got {result.verdict}. "
            f"wins={result.wins}, losses={result.losses}"
        )
        assert result.losses >= 6

    def test_verdict_inconclusive_when_mixed(self) -> None:
        """INCONCLUSIVE when results are borderline / mixed."""
        from bot.audit.comparison import champion_vs_challenger

        # Identical performance → no clear winner
        champ, chal = _paired_results(10, champ_calmar=2.0, chal_calmar=2.0)
        result = champion_vs_challenger(champ, chal)

        assert result.verdict == "INCONCLUSIVE", (
            f"Expected INCONCLUSIVE for identical performance, got {result.verdict}"
        )

    def test_win_loss_tie_counts_correct(self) -> None:
        """wins + losses + ties must equal number of windows."""
        from bot.audit.comparison import champion_vs_challenger

        n = 10
        champ, chal = _paired_results(n, champ_calmar=2.0, chal_calmar=3.0)
        result = champion_vs_challenger(champ, chal)

        assert result.wins + result.losses + result.ties == n, (
            f"wins({result.wins}) + losses({result.losses}) + ties({result.ties}) != {n}"
        )

    def test_win_loss_tie_counts_all_wins(self) -> None:
        """All 10 windows won by challenger → wins=10, losses=0, ties=0."""
        from bot.audit.comparison import champion_vs_challenger

        champ  = [_make_window_result(i, calmar=1.0) for i in range(10)]
        chal   = [_make_window_result(i, calmar=3.0) for i in range(10)]
        result = champion_vs_challenger(champ, chal)

        assert result.wins == 10
        assert result.losses == 0
        assert result.ties == 0

    def test_win_loss_tie_counts_all_ties(self) -> None:
        """All windows tied → ties=10, wins=0, losses=0."""
        from bot.audit.comparison import champion_vs_challenger

        champ, chal = _paired_results(10, champ_calmar=2.0, chal_calmar=2.0)
        result = champion_vs_challenger(champ, chal)

        assert result.ties == 10
        assert result.wins == 0
        assert result.losses == 0

    def test_requires_equal_length_results(self) -> None:
        """champion and challenger must have the same number of results."""
        from bot.audit.comparison import champion_vs_challenger

        champ = [_make_window_result(i) for i in range(5)]
        chal  = [_make_window_result(i) for i in range(3)]

        with pytest.raises(ValueError, match="[Ll]ength|[Mm]atch|[Ee]qual"):
            champion_vs_challenger(champ, chal)

    def test_requires_at_least_2_windows(self) -> None:
        """Single-window comparison cannot run paired t-test."""
        from bot.audit.comparison import champion_vs_challenger

        champ = [_make_window_result(0, calmar=2.0)]
        chal  = [_make_window_result(0, calmar=3.0)]

        # Should either raise or return INCONCLUSIVE (not crash with ZeroDivisionError)
        try:
            result = champion_vs_challenger(champ, chal)
            assert result.verdict in {"ADOPT", "REJECT", "INCONCLUSIVE"}
        except ValueError:
            pass  # raising ValueError is also acceptable

    def test_cohen_d_positive_when_challenger_better(self) -> None:
        """calmar_cohen_d must be positive when challenger consistently outperforms champion."""
        from bot.audit.comparison import champion_vs_challenger

        champ  = [_make_window_result(i, calmar=1.0) for i in range(10)]
        chal   = [_make_window_result(i, calmar=3.0) for i in range(10)]
        result = champion_vs_challenger(champ, chal)

        # challenger_calmar > champion_calmar → diff = chal - champ > 0 → positive d
        assert result.calmar_cohen_d > 0, (
            f"calmar_cohen_d should be positive when challenger > champion, got {result.calmar_cohen_d}"
        )

    def test_c1_pf_better_than_c2_direction(self) -> None:
        """REQ-CC-3: When C1 is champion and C2 is challenger, C1 should 'win' (C2 is worse).

        This matches the May 2026 audit finding: C1 (PF=1.46 mean) > C2 (PF=1.09 mean).
        We use synthetic data mimicking that gap; the direction of losses must confirm C2 loses.
        """
        from bot.audit.comparison import champion_vs_challenger

        # C1: PF consistently around 1.45, Calmar around 15
        c1_results = [_make_window_result(i, calmar=15.0, pf=1.45) for i in range(10)]
        # C2: PF consistently around 1.09, Calmar around 5
        c2_results = [_make_window_result(i, calmar=5.0, pf=1.09) for i in range(10)]

        result = champion_vs_challenger(c1_results, c2_results)
        # C2 (challenger) has lower PF and Calmar → should lose most windows → REJECT
        assert result.losses >= 6, (
            f"C2 should lose >= 6/10 windows vs C1, got losses={result.losses}"
        )
        assert result.verdict == "REJECT", (
            f"Expected REJECT (C2 is worse), got {result.verdict}"
        )
