"""VIX index frame for regime gating (^VIX via yfinance)."""

from __future__ import annotations

import pandas as pd


def fetch_vix_daily_yfinance(
    *,
    start: str = "2015-01-01",
    end: str | None = None,
) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download("^VIX", start=start, end=end, progress=False, auto_adjust=False)
    if raw is None or raw.empty:
        raise ValueError("No yfinance data for ^VIX")
    frame = raw.reset_index()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(c[0]) if isinstance(c, tuple) else str(c) for c in frame.columns]
    rename = {col: col.capitalize() for col in frame.columns}
    frame = frame.rename(columns=rename)
    if "Date" not in frame.columns and "Datetime" in frame.columns:
        frame = frame.rename(columns={"Datetime": "Date"})
    frame["Date"] = pd.to_datetime(frame["Date"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col not in frame.columns:
            if col == "Volume":
                frame["Volume"] = 0.0
            elif col == "Close" and "Adj Close" in frame.columns:
                frame["Close"] = frame["Adj Close"]
    return frame.sort_values("Date").reset_index(drop=True)
