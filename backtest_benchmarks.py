"""Buy-and-hold benchmark helpers for walk-forward strategy comparison."""

from __future__ import annotations

from typing import Mapping

import pandas as pd


def _normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if "Date" in frame.columns:
        frame["Date"] = pd.to_datetime(frame["Date"])
        frame = frame.sort_values("Date").set_index("Date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
        frame = frame.sort_index()
    return frame


def equal_weight_buy_hold_return_pct(ohlcv_by_ticker: Mapping[str, pd.DataFrame]) -> float:
    """
    Equal-weight portfolio: invest 1/N of notional in each ticker at window open,
    hold to window close. Returns total return % (ignores commission).
    """
    if not ohlcv_by_ticker:
        return 0.0

    returns: list[float] = []
    for df in ohlcv_by_ticker.values():
        if df is None or df.empty or len(df) < 2:
            continue
        frame = _normalize_frame(df)
        start = float(frame["Close"].iloc[0])
        end = float(frame["Close"].iloc[-1])
        if start <= 0:
            continue
        returns.append((end / start) - 1.0)

    if not returns:
        return 0.0
    avg = sum(returns) / len(returns)
    return avg * 100.0


def equal_weight_buy_hold_final_equity(
    ohlcv_by_ticker: Mapping[str, pd.DataFrame],
    initial_cash: float,
) -> float:
    if not ohlcv_by_ticker:
        return initial_cash

    per_name = initial_cash / len(ohlcv_by_ticker)
    total = 0.0
    for df in ohlcv_by_ticker.values():
        if df is None or df.empty:
            continue
        frame = _normalize_frame(df)
        start = float(frame["Close"].iloc[0])
        end = float(frame["Close"].iloc[-1])
        if start <= 0:
            continue
        shares = per_name / start
        total += shares * end
    return total


def ticker_buy_hold_return_pct(df: pd.DataFrame) -> float:
    if df is None or df.empty or len(df) < 2:
        return 0.0
    frame = _normalize_frame(df)
    start = float(frame["Close"].iloc[0])
    end = float(frame["Close"].iloc[-1])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def spy_buy_hold_return_pct(
    spy_df: pd.DataFrame | None,
    *,
    start: str,
    end: str,
) -> float | None:
    if spy_df is None or spy_df.empty:
        return None
    frame = _normalize_frame(spy_df)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    window = frame.loc[(frame.index >= start_ts) & (frame.index <= end_ts)]
    if len(window) < 2:
        return None
    first = float(window["Close"].iloc[0])
    last = float(window["Close"].iloc[-1])
    if first <= 0:
        return None
    return (last / first - 1.0) * 100.0


def compare_legacy_vs_top3(
    legacy_return_pct: float,
    top3_return_pct: float,
    *,
    window_label: str = "",
) -> dict[str, float | str]:
    """Lightweight comparison payload for walk-forward / benchmark tables."""
    delta = top3_return_pct - legacy_return_pct
    if abs(delta) < 0.01:
        winner = "tie"
    elif delta > 0:
        winner = "top3"
    else:
        winner = "legacy"
    label = window_label or "window"
    return {
        "window": label,
        "legacy_return_pct": legacy_return_pct,
        "top3_return_pct": top3_return_pct,
        "delta_pct": delta,
        "winner": winner,
    }


def summarize_strategy_vs_benchmarks(
    strategy_return_pct: float,
    ohlcv_by_ticker: Mapping[str, pd.DataFrame],
    *,
    spy_df: pd.DataFrame | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, float | None]:
    bh = equal_weight_buy_hold_return_pct(ohlcv_by_ticker)
    spy = None
    if spy_df is not None and window_start and window_end:
        spy = spy_buy_hold_return_pct(spy_df, start=window_start, end=window_end)
    alpha_bh = strategy_return_pct - bh
    alpha_spy = strategy_return_pct - spy if spy is not None else None
    return {
        "strategy_return_pct": strategy_return_pct,
        "buy_hold_return_pct": bh,
        "spy_return_pct": spy,
        "alpha_vs_buy_hold_pct": alpha_bh,
        "alpha_vs_spy_pct": alpha_spy,
    }
