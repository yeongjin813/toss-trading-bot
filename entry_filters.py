"""Optional entry confirmation filters (weekly trend, 52-week high proximity)."""

from __future__ import annotations

import os

import pandas as pd

TRADING_DAYS_52W = 252


def _asof_frame(df: pd.DataFrame, as_of_date: str | None) -> pd.DataFrame:
    work = df.copy()
    if "Date" in work.columns:
        work["Date"] = pd.to_datetime(work["Date"])
        work = work.sort_values("Date")
        if as_of_date:
            work = work[work["Date"] <= pd.Timestamp(as_of_date)]
        work = work.set_index("Date")
    elif isinstance(work.index, pd.DatetimeIndex):
        work = work.sort_index()
        if as_of_date:
            work = work.loc[: pd.Timestamp(as_of_date)]
    return work


def weekly_trend_bullish(
    df: pd.DataFrame,
    as_of_date: str | None = None,
    *,
    sma_period: int = 20,
) -> bool:
    """True when the latest weekly close is above its SMA (default 20 weeks)."""
    work = _asof_frame(df, as_of_date)
    if work.empty or len(work) < sma_period * 5:
        return True

    weekly = work.resample("W-FRI").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Close"])
    if len(weekly) < sma_period:
        return True

    closes = weekly["Close"].astype(float)
    sma = closes.rolling(window=sma_period, min_periods=sma_period).mean()
    if pd.isna(sma.iloc[-1]):
        return True
    return float(closes.iloc[-1]) > float(sma.iloc[-1])


def near_52_week_high(
    df: pd.DataFrame,
    as_of_date: str | None = None,
    *,
    proximity_pct: float = 0.05,
) -> bool:
    """True when price is within proximity_pct of the 52-week high."""
    work = _asof_frame(df, as_of_date)
    if work.empty:
        return True

    closes = work["Close"].astype(float)
    if len(closes) < 20:
        return True

    window = min(len(closes), TRADING_DAYS_52W)
    high_52w = float(closes.tail(window).max())
    close = float(closes.iloc[-1])
    if high_52w <= 0:
        return True
    return close >= high_52w * (1.0 - proximity_pct)


def entry_filters_enabled() -> dict[str, bool | float | int]:
    from trading_features import TradingFeatureFlags

    flags = TradingFeatureFlags.from_env()
    return flags.entry_filter_settings()


def passes_entry_filters(
    df: pd.DataFrame,
    as_of_date: str | None = None,
    *,
    settings: dict[str, bool | float | int] | None = None,
) -> tuple[bool, str | None]:
    """Return (ok, block_reason). Empty reason when ok."""
    cfg = settings or entry_filters_enabled()
    if cfg.get("weekly_trend") and not weekly_trend_bullish(
        df,
        as_of_date,
        sma_period=int(cfg.get("weekly_sma_period", 20)),
    ):
        return False, "weekly trend bearish"
    if cfg.get("near_52w_high") and not near_52_week_high(
        df,
        as_of_date,
        proximity_pct=float(cfg.get("near_52w_high_pct", 0.05)),
    ):
        return False, "not near 52-week high"
    return True, None
