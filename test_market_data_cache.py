"""Tests for completed vs forming bar cache semantics."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd

from market_data_cache import MarketDataCache, read_market_csv


def _history(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class _MockClient:
    def __init__(self, latest: pd.DataFrame) -> None:
        self._latest = latest

    def fetch_us_daily_latest(self, ticker: str, excd: str) -> pd.DataFrame:
        return self._latest.copy()

    def fetch_us_daily_bars(
        self,
        ticker: str,
        excd: str,
        target_bars: int = 756,
    ) -> pd.DataFrame:
        raise RuntimeError("not used in these tests")


class MarketDataCacheTests(unittest.TestCase):
    def _cache_with_history(
        self,
        client: _MockClient,
        history: pd.DataFrame,
        *,
        tmpdir: str,
    ) -> MarketDataCache:
        path = os.path.join(tmpdir, "nvda_daily.csv")
        history.to_csv(path, index=False)
        cache = MarketDataCache(
            client,
            market_meta={"NVDA": {"excd": "NAS"}},
            data_dir=tmpdir,
            target_bars=30,
            min_data_bars=5,
            max_refresh_workers=2,
        )
        completed, forming = cache._load_or_fetch_full_history("NVDA")
        cache._completed["NVDA"] = completed
        if forming is not None:
            cache._forming["NVDA"] = forming
        return cache

    def test_intraday_forming_not_persisted(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=10)
        history = _history(
            {
                "Date": dates,
                "Open": [100.0] * 10,
                "High": [101.0] * 10,
                "Low": [99.0] * 10,
                "Close": [100.5] * 10,
                "Volume": [1_000_000.0] * 10,
            }
        )
        session = dates[-1].strftime("%Y-%m-%d")
        latest = _history(
            {
                "Date": [session],
                "Open": [100.0],
                "High": [105.0],
                "Low": [98.0],
                "Close": [104.0],
                "Volume": [500_000.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            client = _MockClient(latest)
            cache = self._cache_with_history(client, history, tmpdir=tmpdir)
            ny_open = datetime(2024, 6, 18, 10, 0, 0)

            with patch("market_data_cache.is_us_regular_market_hours", return_value=True):
                with patch("market_data_cache.ny_session_date_str", return_value=session):
                    frame, is_new = cache.refresh_latest("NVDA", now=ny_open)

            self.assertTrue(is_new)
            self.assertEqual(len(cache._completed["NVDA"]), 10)
            self.assertIn("NVDA", cache._forming)
            persisted = read_market_csv(os.path.join(tmpdir, "nvda_daily.csv"))
            assert persisted is not None
            self.assertEqual(len(persisted), 10)
            self.assertEqual(float(frame.iloc[-1]["Close"]), 104.0)

    def test_finalize_commits_forming_to_disk(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=10)
        history = _history(
            {
                "Date": dates,
                "Open": [100.0] * 10,
                "High": [101.0] * 10,
                "Low": [99.0] * 10,
                "Close": [100.5] * 10,
                "Volume": [1_000_000.0] * 10,
            }
        )
        session = (dates[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        latest = _history(
            {
                "Date": [session],
                "Open": [100.0],
                "High": [105.0],
                "Low": [98.0],
                "Close": [104.0],
                "Volume": [900_000.0],
            }
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            client = _MockClient(latest)
            cache = self._cache_with_history(client, history, tmpdir=tmpdir)
            ny_open = datetime(2024, 6, 18, 10, 0, 0)
            with patch("market_data_cache.is_us_regular_market_hours", return_value=True):
                with patch("market_data_cache.ny_session_date_str", return_value=session):
                    cache.refresh_latest("NVDA", now=ny_open)

            with patch("market_data_cache.is_us_regular_market_hours", return_value=False):
                count = cache.finalize_intraday_bars(now=datetime(2024, 6, 18, 17, 0, 0))

            self.assertEqual(count, 1)
            self.assertNotIn("NVDA", cache._forming)
            self.assertEqual(len(cache._completed["NVDA"]), 11)
            persisted = read_market_csv(os.path.join(tmpdir, "nvda_daily.csv"))
            assert persisted is not None
            self.assertEqual(len(persisted), 11)
            self.assertEqual(float(persisted.iloc[-1]["Close"]), 104.0)

    def test_parallel_refresh_returns_all_tickers(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=8)
        history = _history(
            {
                "Date": dates,
                "Open": [50.0] * 8,
                "High": [51.0] * 8,
                "Low": [49.0] * 8,
                "Close": [50.5] * 8,
                "Volume": [100_000.0] * 8,
            }
        )
        latest = history.tail(1)
        with tempfile.TemporaryDirectory() as tmpdir:
            client = _MockClient(latest)
            meta = {"AAPL": {"excd": "NAS"}, "MSFT": {"excd": "NAS"}}
            cache = MarketDataCache(
                client,
                market_meta=meta,
                data_dir=tmpdir,
                target_bars=20,
                min_data_bars=5,
            )
            for ticker in ("AAPL", "MSFT"):
                path = os.path.join(tmpdir, f"{ticker.lower()}_daily.csv")
                history.to_csv(path, index=False)
                completed, _ = cache._load_or_fetch_full_history(ticker)
                cache._completed[ticker] = completed

            with patch("market_data_cache.is_us_regular_market_hours", return_value=False):
                results = cache.refresh_latest_parallel(["AAPL", "MSFT"])

            self.assertEqual(set(results), {"AAPL", "MSFT"})


if __name__ == "__main__":
    unittest.main()
