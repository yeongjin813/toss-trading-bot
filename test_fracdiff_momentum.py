"""Tests for fractional-differencing momentum helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fracdiff_momentum import (
    fracdiff_horizon_returns,
    fracdiff_period_change,
    fracdiff_weights,
    fractional_diff_series,
)


def test_fracdiff_weights_length_and_sign() -> None:
    w = fracdiff_weights(0.4, threshold=1e-5, max_window=50)
    assert len(w) >= 2
    assert w[-1] == 1.0
    assert w[0] < 0


def test_fractional_diff_flat_series_stationary_tail() -> None:
    closes = pd.Series(np.full(600, 100.0))
    fd = fractional_diff_series(np.log(closes), d=0.4)
    tail = fd.dropna().tail(50)
    assert not tail.empty
    assert float(tail.std()) < 1e-6


def test_fracdiff_horizon_returns_trending_series() -> None:
    closes = pd.Series(np.linspace(50.0, 150.0, 600))
    r3, r6, r12 = fracdiff_horizon_returns(closes, d=0.4)
    assert r3 is not None and r6 is not None and r12 is not None
    assert r3 > 0 and r6 > 0 and r12 > 0


def test_fracdiff_period_change() -> None:
    fd = pd.Series(np.arange(100, dtype=float))
    assert fracdiff_period_change(fd, 10) == 10.0
