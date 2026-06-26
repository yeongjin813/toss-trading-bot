"""
Fractional-differencing momentum features (López de Prado style, research only).

Transforms log-price with order d in (0, 1) to preserve memory while improving
stationarity, then uses multi-horizon changes for cross-sectional Top3 ranking.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np
import pandas as pd

DEFAULT_FRACDIFF_D = 0.4
DEFAULT_WEIGHT_THRESHOLD = 1e-5
DEFAULT_MAX_WINDOW = 252


def fracdiff_weights(
    d: float,
    *,
    threshold: float = DEFAULT_WEIGHT_THRESHOLD,
    max_window: int = DEFAULT_MAX_WINDOW,
) -> np.ndarray:
    """Fixed-width fractional-difference weights (newest bar last)."""
    if not 0.0 < d < 1.0:
        raise ValueError(f"fracdiff order d must be in (0, 1), got {d}")
    weights = [1.0]
    k = 1
    while k < max_window:
        next_w = -weights[-1] * (d - k + 1.0) / float(k)
        if abs(next_w) < threshold:
            break
        weights.append(next_w)
        k += 1
    return np.array(list(reversed(weights)), dtype=float)


def fractional_diff_series(
    values: Sequence[float] | pd.Series,
    d: float = DEFAULT_FRACDIFF_D,
    *,
    threshold: float = DEFAULT_WEIGHT_THRESHOLD,
    max_window: int = DEFAULT_MAX_WINDOW,
) -> pd.Series:
    """Apply fixed-width fractional differentiation; index aligned to input."""
    series = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if series.empty:
        return pd.Series(dtype=float)

    weights = fracdiff_weights(d, threshold=threshold, max_window=max_window)
    width = len(weights)
    if len(series) < width:
        return pd.Series(np.nan, index=series.index)

    arr = series.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    for idx in range(width - 1, len(arr)):
        window = arr[idx - width + 1 : idx + 1]
        if np.any(np.isnan(window)):
            continue
        out[idx] = float(np.dot(weights, window))

    return pd.Series(out, index=series.index)


def fracdiff_period_change(fd: pd.Series, lookback: int) -> float | None:
    """Change in fractional-diff level over lookback bars."""
    clean = fd.dropna()
    if len(clean) <= lookback:
        return None
    start = float(clean.iloc[-lookback - 1])
    end = float(clean.iloc[-1])
    if math.isnan(start) or math.isnan(end):
        return None
    return end - start


def fracdiff_horizon_returns(
    closes: pd.Series,
    *,
    d: float = DEFAULT_FRACDIFF_D,
    lookbacks: tuple[int, int, int] = (63, 126, 252),
) -> tuple[float | None, float | None, float | None]:
    """3M/6M/12M style signals on the fractionally-differenced log-price series."""
    if closes.empty:
        return None, None, None
    positive = closes.astype(float)
    if (positive <= 0).any():
        return None, None, None
    fd = fractional_diff_series(np.log(positive), d=d)
    return (
        fracdiff_period_change(fd, lookbacks[0]),
        fracdiff_period_change(fd, lookbacks[1]),
        fracdiff_period_change(fd, lookbacks[2]),
    )
