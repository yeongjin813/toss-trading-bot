"""Time-series (absolute) momentum gate — OSS dual-momentum TSM layer."""

from __future__ import annotations

import pandas as pd

TRADING_DAYS_12M = 252


def _asof_closes(df: pd.DataFrame, as_of_date: str | None) -> pd.Series:
    work = df.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"])
        work = work.sort_values("Date")
        if as_of_date:
            work = work[work["Date"] <= pd.Timestamp(as_of_date)]
        closes = work.set_index("Date")["Close"]
    elif isinstance(work.index, pd.DatetimeIndex):
        work = work.sort_index()
        if as_of_date:
            work = work.loc[: pd.Timestamp(as_of_date)]
        closes = work["Close"]
    else:
        closes = work["Close"]
    return closes.astype(float)


def period_return(closes: pd.Series, lookback: int) -> float | None:
    if len(closes) <= lookback:
        return None
    start = float(closes.iloc[-lookback - 1])
    end = float(closes.iloc[-1])
    if start <= 0:
        return None
    return (end / start) - 1.0


def passes_tsm_gate(
    df: pd.DataFrame,
    as_of_date: str | None = None,
    *,
    lookback: int = TRADING_DAYS_12M,
    min_return: float = 0.0,
) -> tuple[bool, str | None]:
    """True when lookback total return exceeds min_return (default: positive 12M)."""
    closes = _asof_closes(df, as_of_date)
    ret = period_return(closes, lookback)
    if ret is None:
        return False, "insufficient history for TSM"
    if ret <= min_return:
        return False, f"TSM {lookback}d return {ret:.2%} <= {min_return:.2%}"
    return True, None
