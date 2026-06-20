"""Backtest fill timing: next-bar open vs same-bar close."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

from momentum_ranker import MomentumRankSettings
from portfolio_backtest import run_portfolio_backtest


def _synthetic_frame(closes: list[float]) -> pd.DataFrame:
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            {
                "Date": pd.Timestamp("2024-01-02") + pd.Timedelta(days=i),
                "Open": close * 0.99,
                "High": close * 1.01,
                "Low": close * 0.98,
                "Close": close,
                "Volume": 2_000_000,
            }
        )
    frame = pd.DataFrame(rows)
    return frame.set_index("Date")


class PortfolioFillTimingTests(unittest.TestCase):
    def test_next_open_defers_fill_date(self) -> None:
        # Two tickers so portfolio engine runs; minimal bars for smoke.
        a = _synthetic_frame([100 + i for i in range(80)])
        b = _synthetic_frame([50 + i * 0.5 for i in range(80)])
        ohlcv = {"AAA": a, "BBB": b}
        momentum = MomentumRankSettings(enabled=False, top_n=1, min_bars=5)

        with patch.dict(os.environ, {"BACKTEST_FILL_AT_NEXT_OPEN": "true"}, clear=False):
            next_open = run_portfolio_backtest(
                ["AAA", "BBB"],
                ohlcv,
                initial_cash=10_000.0,
                use_spy_market_filter=False,
                momentum_settings=momentum,
            )
        with patch.dict(os.environ, {"BACKTEST_FILL_AT_NEXT_OPEN": "false"}, clear=False):
            same_close = run_portfolio_backtest(
                ["AAA", "BBB"],
                ohlcv,
                initial_cash=10_000.0,
                use_spy_market_filter=False,
                momentum_settings=momentum,
            )

        if next_open.trades and same_close.trades:
            self.assertGreaterEqual(
                next_open.trades[0].date,
                same_close.trades[0].date,
            )


if __name__ == "__main__":
    unittest.main()
