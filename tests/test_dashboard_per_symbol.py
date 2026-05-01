"""Lightweight signature checks for per-symbol dashboard sections.

These do not exercise Streamlit rendering — they verify that section
functions accept a `symbol` argument.
"""

import inspect

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")
pytest.importorskip("binance")


def test_live_price_section_accepts_symbol_param():
    from dashboard.sections.live_price import live_price_section
    sig = inspect.signature(live_price_section)
    params = list(sig.parameters.keys())
    assert "symbol" in params, f"missing 'symbol' param; got {params}"


def test_open_position_section_accepts_symbol_param():
    from dashboard.sections.open_position import open_position_section
    sig = inspect.signature(open_position_section)
    params = list(sig.parameters.keys())
    assert "symbol" in params, f"missing 'symbol' param; got {params}"


def test_signal_log_section_accepts_symbol_param():
    from dashboard.sections.signal_log import signal_log_section
    sig = inspect.signature(signal_log_section)
    params = list(sig.parameters.keys())
    assert "symbol" in params, f"missing 'symbol' param; got {params}"


def test_performance_section_accepts_symbol_param():
    from dashboard.sections.performance import performance_section
    sig = inspect.signature(performance_section)
    params = list(sig.parameters.keys())
    assert "symbol" in params, f"missing 'symbol' param; got {params}"


def test_adaptive_params_section_exists():
    from dashboard.sections.performance import adaptive_params_section
    sig = inspect.signature(adaptive_params_section)
    params = list(sig.parameters.keys())
    assert params == ["db"], f"expected ['db'] got {params}"
