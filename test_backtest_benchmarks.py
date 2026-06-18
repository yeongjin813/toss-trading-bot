"""Tests for buy-and-hold benchmark helpers."""

from __future__ import annotations

import pandas as pd

from backtest_benchmarks import (
    equal_weight_buy_hold_return_pct,
    spy_buy_hold_return_pct,
    summarize_strategy_vs_benchmarks,
)


def _make_ohlcv(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=len(closes), freq="B")
    return pd.DataFrame({"Close": closes}, index=dates)


def test_equal_weight_buy_hold_return_pct():
    aapl = _make_ohlcv([100.0, 110.0])
    nvda = _make_ohlcv([200.0, 180.0])
    result = equal_weight_buy_hold_return_pct({"AAPL": aapl, "NVDA": nvda})
    assert abs(result - 0.0) < 1e-6


def test_spy_buy_hold_return_pct_window():
    spy = _make_ohlcv([400.0, 410.0, 420.0, 430.0])
    pct = spy_buy_hold_return_pct(
        spy,
        start="2024-01-02",
        end="2024-01-05",
    )
    assert pct is not None
    assert abs(pct - 7.5) < 1e-6


def test_summarize_strategy_vs_benchmarks():
    aapl = _make_ohlcv([100.0, 120.0])
    spy = _make_ohlcv([400.0, 440.0])
    summary = summarize_strategy_vs_benchmarks(
        15.0,
        {"AAPL": aapl},
        spy_df=spy,
        window_start="2024-01-02",
        window_end="2024-01-03",
    )
    assert summary["strategy_return_pct"] == 15.0
    assert abs(summary["buy_hold_return_pct"] - 20.0) < 1e-6
    assert abs(summary["alpha_vs_buy_hold_pct"] - (-5.0)) < 1e-6
    assert abs(summary["spy_return_pct"] - 10.0) < 1e-6
    assert abs(summary["alpha_vs_spy_pct"] - 5.0) < 1e-6