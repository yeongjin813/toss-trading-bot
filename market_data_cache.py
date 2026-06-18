"""
Live OHLCV cache with completed-bar persistence and intraday forming-bar overlay.

Fixes three production risks:
  1. Incomplete RTH bars are never written to disk (no CSV pollution).
  2. Parallel per-cycle refresh removes 15-ticker sequential API skew.
  3. In-place row updates avoid per-tick pd.concat memory fragmentation.
"""

from __future__ import annotations

import gc
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Protocol

import pandas as pd

from analytics import is_us_regular_market_hours, ny_session_date_str

_OHLCV_COLS = ("Open", "High", "Low", "Close", "Volume")


class DailyPriceClient(Protocol):
    def fetch_us_daily_latest(self, ticker: str, excd: str) -> pd.DataFrame: ...

    def fetch_us_daily_bars(
        self,
        ticker: str,
        excd: str,
        target_bars: int = ...,
    ) -> pd.DataFrame: ...


def normalize_market_frame(df: pd.DataFrame, *, min_bars: int) -> pd.DataFrame:
    frame = df.copy()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    for column in _OHLCV_COLS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Date", *_OHLCV_COLS]).copy()
    frame = frame[(frame["Close"] > 0) & (frame["Volume"] >= 0)]
    frame = frame.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    if len(frame) < min_bars:
        raise ValueError(f"Insufficient bar count after normalization: {len(frame)}")
    return frame


def persist_market_data(df: pd.DataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)


def read_market_csv(path: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    frame = pd.read_csv(path)
    if frame.empty:
        return None
    if "Date" not in frame.columns and "Price" in frame.columns:
        frame = frame.rename(columns={"Price": "Date"})
    if "Date" not in frame.columns:
        return None
    while len(frame) > 0:
        first_cell = str(frame.iloc[0, 0]).strip()
        if first_cell.lower() in {"ticker", "date"}:
            frame = frame.iloc[1:].copy()
            continue
        if first_cell and not first_cell[:4].isdigit():
            frame = frame.iloc[1:].copy()
            continue
        break
    if frame.empty:
        return None
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
    for column in _OHLCV_COLS:
        if column not in frame.columns:
            return None
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Date", *_OHLCV_COLS])
    if frame.empty:
        return None
    return frame.sort_values("Date").reset_index(drop=True)


def _bar_date_key(ts: Any) -> pd.Timestamp:
    return pd.Timestamp(ts).normalize()


class MarketDataCache:
    """
    Bootstrap full historical windows once, then refresh with:
      - ``_completed``: finalized EOD bars (persisted to CSV)
      - ``_forming``: today's in-progress bar (memory only during RTH)
    """

    def __init__(
        self,
        client: DailyPriceClient,
        *,
        market_meta: dict[str, dict[str, str]],
        data_dir: str,
        target_bars: int,
        min_data_bars: int,
        max_refresh_workers: int | None = None,
    ) -> None:
        self._client = client
        self._market_meta = market_meta
        self._data_dir = data_dir
        self._target_bars = target_bars
        self._min_data_bars = min_data_bars
        self._completed: dict[str, pd.DataFrame] = {}
        self._forming: dict[str, pd.Series] = {}
        self._data_sources: dict[str, str] = {}
        self._cycle_count = 0
        default_workers = max(1, min(4, int(os.getenv("TICKER_REFRESH_WORKERS", "3"))))
        self._max_refresh_workers = max_refresh_workers or default_workers
        # VTS mock often 500s under burst parallel dailyprice — default sequential.
        self._parallel_refresh = os.getenv("PARALLEL_TICKER_REFRESH", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._refresh_stagger_seconds = float(os.getenv("KIS_REFRESH_STAGGER_SECONDS", "1.0"))
        self._min_refresh_seconds = float(os.getenv("OHLCV_MIN_REFRESH_SECONDS", "120"))
        self._last_bulk_refresh_at: datetime | None = None

    def data_path_for_ticker(self, ticker: str) -> str:
        return os.path.join(self._data_dir, f"{ticker.lower()}_daily.csv")

    def get_data_source(self, ticker: str) -> str:
        return self._data_sources.get(ticker, "kis")

    def has_forming_bars(self) -> bool:
        return bool(self._forming)

    def bootstrap(self, tickers: list[str]) -> None:
        for ticker in tickers:
            completed, forming = self._load_or_fetch_full_history(ticker)
            self._completed[ticker] = completed
            if forming is not None:
                self._forming[ticker] = forming
            self.persist_completed(ticker)
            bar_count = len(completed) + (1 if ticker in self._forming else 0)
            print(
                f"  -> Bootstrap {ticker}: {bar_count} bars "
                f"({len(completed)} completed"
                f"{', 1 forming' if ticker in self._forming else ''}) "
                f"cached to {self.data_path_for_ticker(ticker)}"
            )

    def get_frame(self, ticker: str) -> pd.DataFrame:
        return self.evaluation_frame(ticker).copy()

    def evaluation_frame(self, ticker: str) -> pd.DataFrame:
        if ticker not in self._completed:
            raise KeyError(f"Ticker {ticker} is not bootstrapped in MarketDataCache.")
        completed = self._completed[ticker]
        forming = self._forming.get(ticker)
        if forming is None:
            return completed
        overlay = forming.to_frame().T
        return pd.concat([completed, overlay], ignore_index=True)

    def refresh_latest(
        self,
        ticker: str,
        *,
        now: datetime | None = None,
    ) -> tuple[pd.DataFrame, bool]:
        excd = self._market_meta[ticker]["excd"]
        try:
            latest_batch = self._client.fetch_us_daily_latest(ticker, excd)
        except Exception as exc:
            print(
                f"[CACHE/WARN] {ticker} KIS refresh failed ({exc}) — "
                "using last good bars"
            )
            return self.evaluation_frame(ticker), False

        if ticker not in self._completed:
            completed, forming = self._load_or_fetch_full_history(ticker)
            self._completed[ticker] = completed
            if forming is not None:
                self._forming[ticker] = forming
            return self.evaluation_frame(ticker), True

        if latest_batch.empty:
            return self.evaluation_frame(ticker), False

        latest_row = self._parse_latest_row(latest_batch)
        if latest_row is None:
            return self.evaluation_frame(ticker), False

        latest_date = _bar_date_key(latest_row["Date"])
        session_date = _bar_date_key(ny_session_date_str(now))
        during_rth = is_us_regular_market_hours(now)

        existing_forming = self._forming.get(ticker)
        if existing_forming is not None:
            forming_date = _bar_date_key(existing_forming["Date"])
            if latest_date > forming_date:
                self._upsert_completed_row(ticker, existing_forming)
                self._forming.pop(ticker, None)
                self.persist_completed(ticker)

        is_new_session_bar = latest_date not in {
            _bar_date_key(d) for d in self._completed[ticker]["Date"]
        }

        if during_rth and latest_date >= session_date:
            prev = self._forming.get(ticker)
            self._forming[ticker] = latest_row.copy()
            is_new = prev is None or _bar_date_key(prev["Date"]) != latest_date
            return self.evaluation_frame(ticker), is_new

        self._upsert_completed_row(ticker, latest_row)
        self._forming.pop(ticker, None)
        self.persist_completed(ticker)
        return self.evaluation_frame(ticker), is_new_session_bar

    def refresh_latest_parallel(
        self,
        tickers: list[str],
        *,
        now: datetime | None = None,
    ) -> dict[str, tuple[pd.DataFrame, bool]]:
        unique = list(dict.fromkeys(tickers))
        if not unique:
            return {}

        now_dt = now or datetime.now()
        if (
            self._min_refresh_seconds > 0
            and self._last_bulk_refresh_at is not None
            and (now_dt - self._last_bulk_refresh_at).total_seconds()
            < self._min_refresh_seconds
        ):
            print(
                f"[CACHE] Skipping KIS OHLCV refresh "
                f"(last {int((now_dt - self._last_bulk_refresh_at).total_seconds())}s ago, "
                f"min interval {self._min_refresh_seconds:.0f}s)"
            )
            return {t: (self.evaluation_frame(t), False) for t in unique}

        if not self._parallel_refresh or len(unique) == 1:
            results = self._refresh_sequential(unique, now=now)
        else:
            results: dict[str, tuple[pd.DataFrame, bool]] = {}
            workers = min(self._max_refresh_workers, len(unique))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self.refresh_latest, ticker, now=now): ticker
                    for ticker in unique
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        results[ticker] = future.result()
                    except Exception as exc:
                        print(
                            f"[CACHE/WARN] {ticker} parallel refresh failed ({exc}) — "
                            "using last good bars"
                        )
                        results[ticker] = (self.evaluation_frame(ticker), False)

        self._last_bulk_refresh_at = now_dt
        return results

    def _refresh_sequential(
        self,
        tickers: list[str],
        *,
        now: datetime | None = None,
    ) -> dict[str, tuple[pd.DataFrame, bool]]:
        results: dict[str, tuple[pd.DataFrame, bool]] = {}
        for index, ticker in enumerate(tickers):
            results[ticker] = self.refresh_latest(ticker, now=now)
            if index + 1 < len(tickers) and self._refresh_stagger_seconds > 0:
                time.sleep(self._refresh_stagger_seconds)
        return results

    def finalize_intraday_bars(self, *, now: datetime | None = None) -> int:
        """Commit in-memory forming bars to completed history (EOD / post-RTH)."""
        if is_us_regular_market_hours(now):
            return 0
        finalized = 0
        for ticker in list(self._forming.keys()):
            row = self._forming.pop(ticker)
            self._upsert_completed_row(ticker, row)
            self.persist_completed(ticker)
            finalized += 1
        return finalized

    def persist_completed(self, ticker: str) -> None:
        if ticker not in self._completed:
            return
        persist_market_data(self._completed[ticker], self.data_path_for_ticker(ticker))

    def maybe_collect_garbage(self) -> None:
        self._cycle_count += 1
        interval = max(1, int(os.getenv("CACHE_GC_EVERY_N_CYCLES", "60")))
        if self._cycle_count % interval == 0:
            gc.collect()

    def _parse_latest_row(self, batch: pd.DataFrame) -> pd.Series | None:
        if batch.empty:
            return None
        frame = batch.copy()
        frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce")
        for column in _OHLCV_COLS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["Date", *_OHLCV_COLS])
        if frame.empty:
            return None
        row = frame.sort_values("Date").iloc[-1]
        if float(row["Close"]) <= 0:
            return None
        return row

    def _upsert_completed_row(self, ticker: str, row: pd.Series) -> None:
        completed = self._completed[ticker]
        row_date = _bar_date_key(row["Date"])
        dates = completed["Date"].map(_bar_date_key)
        matches = completed.index[dates == row_date]
        if len(matches):
            idx = int(matches[-1])
            for col in _OHLCV_COLS:
                completed.at[idx, col] = float(row[col])
            self._completed[ticker] = completed
            return

        new_row = row.to_frame().T
        merged = pd.concat([completed, new_row], ignore_index=True)
        merged = (
            merged.drop_duplicates(subset=["Date"], keep="last")
            .sort_values("Date")
            .tail(self._target_bars)
            .reset_index(drop=True)
        )
        self._completed[ticker] = merged

    def _split_forming_from_tail(
        self,
        frame: pd.DataFrame,
        *,
        now: datetime | None = None,
    ) -> tuple[pd.DataFrame, pd.Series | None]:
        if frame.empty:
            return frame, None
        if not is_us_regular_market_hours(now):
            return frame, None
        last_date = _bar_date_key(frame.iloc[-1]["Date"])
        session_date = _bar_date_key(ny_session_date_str(now))
        if last_date < session_date:
            return frame, None
        if last_date > session_date:
            return frame, None
        forming = frame.iloc[-1].copy()
        completed = frame.iloc[:-1].reset_index(drop=True)
        if len(completed) < self._min_data_bars:
            return frame, None
        return completed, forming

    def _load_or_fetch_full_history(
        self,
        ticker: str,
    ) -> tuple[pd.DataFrame, pd.Series | None]:
        path = self.data_path_for_ticker(ticker)
        if os.path.exists(path):
            cached = read_market_csv(path)
            if cached is not None and len(cached) >= self._min_data_bars:
                try:
                    frame = normalize_market_frame(cached, min_bars=self._min_data_bars)
                    completed, forming = self._split_forming_from_tail(frame)
                    self._data_sources[ticker] = "kis"
                    return completed, forming
                except ValueError as exc:
                    print(
                        f"[WARN] Cached CSV for {ticker} failed normalization "
                        f"({exc}) - refetching from KIS"
                    )

        excd = self._market_meta[ticker]["excd"]
        try:
            df = self._client.fetch_us_daily_bars(
                ticker=ticker,
                excd=excd,
                target_bars=self._target_bars,
            )
            frame = normalize_market_frame(df, min_bars=self._min_data_bars)
            self._data_sources[ticker] = "kis"
        except Exception as exc:
            print(
                f"[WARN] KIS history fetch failed for {ticker} ({excd}): {exc} "
                "- using yfinance fallback"
            )
            frame = _fetch_yfinance_daily_bars(ticker, self._target_bars)
            self._data_sources[ticker] = "yfinance"
            frame = normalize_market_frame(frame, min_bars=self._min_data_bars)
        completed, forming = self._split_forming_from_tail(frame)
        return completed, forming


def _fetch_yfinance_daily_bars(ticker: str, target_bars: int) -> pd.DataFrame:
    import yfinance as yf

    lookback_days = int(target_bars * 1.6) + 30
    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    raw = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if raw is None or raw.empty:
        raise RuntimeError(f"yfinance returned no data for {ticker}")

    frame = raw.reset_index()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(level[0]).capitalize() for level in frame.columns]
    else:
        frame.columns = [str(column).capitalize() for column in frame.columns]

    if "Date" not in frame.columns:
        date_col = frame.columns[0]
        frame = frame.rename(columns={date_col: "Date"})

    rename_map = {
        "Open": "Open",
        "High": "High",
        "Low": "Low",
        "Close": "Close",
        "Volume": "Volume",
    }
    for src, dst in rename_map.items():
        if src not in frame.columns:
            raise RuntimeError(f"yfinance frame missing {src} for {ticker}")
    out = frame[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    return out.tail(target_bars).reset_index(drop=True)
