"""Live per-symbol capital allocation must match PortfolioBacktestEngine.

The backtest sizes every entry off `capital / N`, where `capital` is REALIZED
equity (it only moves on closed PnL — opening a trade does not reduce it).
Live used FREE USDT / N, so each extra open position shrank the next entry:
on 2026-10-01 BTC was sized off 8182 / 3 = 2727 instead of ~12646 / 3 = 4215,
risking $40.9 instead of the intended ~$63 (-35 %). Gotcha #40's "÷N parity"
only held while nothing else was open.
"""
import pytest

import main as main_module


def test_allocation_uses_trading_equity_not_free_cash():
    # 2 of 3 slots deployed: free cash 8182, realized equity 12646
    assert main_module._allocate_capital(
        trading_equity=12646.41, free_usdt=8182.35, n_symbols=3,
    ) == pytest.approx(12646.41 / 3)


def test_allocation_never_exceeds_free_cash():
    # Spot cannot buy with money it does not have
    assert main_module._allocate_capital(
        trading_equity=12000.0, free_usdt=3000.0, n_symbols=3,
    ) == pytest.approx(3000.0)


def test_allocation_falls_back_to_free_cash_without_baseline():
    assert main_module._allocate_capital(
        trading_equity=None, free_usdt=9000.0, n_symbols=3,
    ) == pytest.approx(3000.0)


def test_single_symbol_gets_everything_up_to_free_cash():
    assert main_module._allocate_capital(
        trading_equity=10000.0, free_usdt=10000.0, n_symbols=1,
    ) == pytest.approx(10000.0)
