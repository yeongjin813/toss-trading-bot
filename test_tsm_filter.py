"""Tests for absolute momentum (TSM) gate."""

from __future__ import annotations

import pandas as pd

from entry_filters import passes_entry_filters
from tsm_filter import passes_tsm_gate


def _trend_up(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    closes = [100 + i * 0.5 for i in range(n)]
    return pd.DataFrame({"Close": closes}, index=idx)


def _trend_down(n: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2019-01-01", periods=n, freq="B")
    closes = [200 - i * 0.5 for i in range(n)]
    return pd.DataFrame({"Close": closes}, index=idx)


def test_tsm_passes_uptrend() -> None:
    ok, reason = passes_tsm_gate(_trend_up(), "2020-06-01", lookback=252, min_return=0.0)
    assert ok is True
    assert reason is None


def test_tsm_blocks_downtrend() -> None:
    ok, reason = passes_tsm_gate(_trend_down(), "2020-06-01", lookback=252, min_return=0.0)
    assert ok is False
    assert reason and "TSM" in reason


def test_entry_filters_tsm_integration() -> None:
    settings = {
        "weekly_trend": False,
        "near_52w_high": False,
        "tsm_gate": True,
        "tsm_lookback": 252,
        "tsm_min_return": 0.0,
    }
    ok, _ = passes_entry_filters(_trend_up(), "2020-06-01", settings=settings)
    assert ok is True
    ok2, reason2 = passes_entry_filters(_trend_down(), "2020-06-01", settings=settings)
    assert ok2 is False
    assert reason2
