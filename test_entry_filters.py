"""Tests for weekly trend and 52-week high entry filters."""

from __future__ import annotations

import numpy as np
import pandas as pd

from entry_filters import near_52_week_high, passes_entry_filters, weekly_trend_bullish


def _make_df(closes: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "Date": dates.strftime("%Y-%m-%d"),
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Volume": [1_000_000] * len(closes),
        }
    )


def test_weekly_trend_bullish_uptrend() -> None:
    closes = (100 * (1.002 ** np.arange(300))).tolist()
    df = _make_df(closes)
    assert weekly_trend_bullish(df) is True


def test_near_52_week_high_at_peak() -> None:
    closes = [100.0] * 200 + [120.0] * 60
    df = _make_df(closes)
    assert near_52_week_high(df, proximity_pct=0.05) is True


def test_passes_entry_filters_blocks_far_from_high() -> None:
    closes = [100.0] * 200 + [120.0, 90.0]
    df = _make_df(closes)
    ok, reason = passes_entry_filters(
        df,
        settings={
            "weekly_trend": False,
            "near_52w_high": True,
            "near_52w_high_pct": 0.05,
            "weekly_sma_period": 20,
        },
    )
    assert ok is False
    assert reason == "not near 52-week high"


def main() -> int:
    test_weekly_trend_bullish_uptrend()
    test_near_52_week_high_at_peak()
    test_passes_entry_filters_blocks_far_from_high()
    print("ALL ENTRY FILTER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
