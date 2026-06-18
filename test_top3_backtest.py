"""Smoke tests for Top3 momentum backtest engine."""

from __future__ import annotations

import numpy as np
import pandas as pd

from momentum_ranker import MomentumRankSettings
from top3_backtest import run_top3_backtest


def _synthetic_universe(tickers: list[str], bars: int = 300) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range(end="2025-06-01", periods=bars)
    out: dict[str, pd.DataFrame] = {}
    for idx, ticker in enumerate(tickers):
        drift = 0.0004 + idx * 0.0002
        rets = rng.normal(drift, 0.02, size=bars)
        closes = 100 * np.cumprod(1 + rets)
        out[ticker] = pd.DataFrame(
            {
                "Open": closes,
                "High": closes * 1.01,
                "Low": closes * 0.99,
                "Close": closes,
                "Volume": rng.integers(1_000_000, 5_000_000, size=bars),
            },
            index=dates,
        )
    return out


def test_top3_backtest_runs_and_rebalances():
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    ohlcv = _synthetic_universe(tickers)
    settings = MomentumRankSettings(
        enabled=True,
        top_n=3,
        min_bars=60,
        require_above_sma50=False,
        require_above_sma200=False,
    )
    result = run_top3_backtest(
        tickers=tickers,
        ohlcv_by_ticker=ohlcv,
        initial_cash=10_000,
        momentum_settings=settings,
    )
    assert result.final_equity > 0
    assert result.rebalance_count >= 1
    assert result.total_trades >= 1
