"""Dual capital-split sweep + walk-forward (legacy + Top3 pools)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import pandas as pd

from config import StrategyConfigMapper
from deployment_config import scaled_capital
from market_registry import BENCHMARK_TICKER, DEFAULT_WATCHLIST, parse_watchlist
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import run_portfolio_backtest
from run_backtest import (
    WALK_FORWARD_WINDOWS,
    YFINANCE_WARMUP_START,
    fetch_yfinance_ohlcv,
    load_daily_csv,
    load_watchlist_data,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)
from top3_backtest import run_top3_backtest, summarize_dual_combined

SPLITS = [
    (70, 30),
    (60, 40),
    (50, 50),
    (40, 60),
    (30, 70),
    (100, 0),
    (0, 100),
]


def _load_backtest_data(
    tickers: list[str],
    start: str,
    end: str,
    data_dir: Path,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame | None]:
    ohlcv, _skipped = load_watchlist_data(tickers, data_dir)
    full = {t: df.copy() for t, df in ohlcv.items()}
    windowed = {
        t: slice_ohlcv_window(df, start, end)
        for t, df in ohlcv.items()
        if not slice_ohlcv_window(df, start, end).empty
    }
    spy_df = None
    if StrategyConfigMapper.use_spy_market_filter():
        spy_path = data_dir / f"{BENCHMARK_TICKER.lower()}_daily.csv"
        spy_df = load_daily_csv(spy_path)
        if spy_df is None:
            spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
    return windowed, full, spy_df


def _run_dual_slice(
    loaded: list[str],
    ohlcv: dict[str, pd.DataFrame],
    full_ohlcv: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    *,
    total_cash: float,
    legacy_pct: float,
    top3_pct: float,
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, float | int]:
    total = legacy_pct + top3_pct
    leg_frac = legacy_pct / total if total > 0 else 0.0
    top_frac = top3_pct / total if total > 0 else 0.0
    leg_cash = scaled_capital(total_cash, leg_frac)
    top_cash = scaled_capital(total_cash, top_frac)

    legacy_momentum = MomentumRankSettings(
        enabled=False,
        top_n=3,
        rebalance_weekday=4,
        weight_3m=0.4,
        weight_6m=0.35,
        weight_12m=0.25,
        weight_volume=0.0,
        require_above_sma50=True,
        require_above_sma200=False,
        min_bars=60,
    )
    top3_settings = MomentumRankSettings(
        enabled=True,
        top_n=3,
        rebalance_weekday=4,
        weight_3m=0.4,
        weight_6m=0.35,
        weight_12m=0.25,
        weight_volume=0.0,
        require_above_sma50=True,
        require_above_sma200=False,
        min_bars=60,
    )

    legacy = None
    top3 = None
    if leg_cash > 0:
        legacy = run_portfolio_backtest(
            tickers=loaded,
            ohlcv_by_ticker=ohlcv,
            initial_cash=leg_cash,
            use_spy_market_filter=spy_df is not None,
            spy_df=spy_df,
            momentum_settings=legacy_momentum,
        )
    if top_cash > 0:
        top3 = run_top3_backtest(
            tickers=loaded,
            ohlcv_by_ticker=full_ohlcv,
            initial_cash=top_cash,
            momentum_settings=top3_settings,
            window_start=window_start,
            window_end=window_end,
        )

    if legacy and top3:
        m = summarize_dual_combined(legacy, top3, legacy_pct=legacy_pct, top3_pct=top3_pct)
    elif legacy:
        m = {
            "total_return_pct": legacy.total_return_pct,
            "max_drawdown_pct": legacy.max_drawdown_pct,
            "sharpe_ratio": legacy.sharpe_ratio,
            "total_trades": legacy.total_trades,
            "final_equity": legacy.final_equity,
        }
    elif top3:
        m = {
            "total_return_pct": top3.total_return_pct,
            "max_drawdown_pct": top3.max_drawdown_pct,
            "sharpe_ratio": top3.sharpe_ratio,
            "total_trades": top3.total_trades,
            "final_equity": top3.final_equity,
        }
    else:
        m = {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "total_trades": 0,
            "final_equity": total_cash,
        }
    return m


def main() -> int:
    tickers = parse_watchlist(os.getenv("WATCHLIST", ",".join(DEFAULT_WATCHLIST)))
    data_dir = ROOT / "data"
    start = "2025-06-01"
    end = "2026-06-18"
    cash = float(os.getenv("CAPITAL_AT_RISK", "100000"))

    ohlcv, full, spy_df = _load_backtest_data(tickers, start, end, data_dir)
    loaded = [t for t in ohlcv if t in ohlcv]
    if len(loaded) < 2:
        print("Insufficient ticker data for sweep.")
        return 1

    print("=" * 88)
    print(f"DUAL SPLIT SWEEP  {start} -> {end}  ${cash:,.0f}  ({len(loaded)} tickers)".center(88))
    print("=" * 88)
    print(f"{'Legacy/Top3':<12} {'Return%':>10} {'MaxDD%':>10} {'Sharpe':>8} {'Trades':>8} {'Final$':>14}")
    print("-" * 88)

    rows: list[tuple[str, dict]] = []
    for leg, top in SPLITS:
        label = f"{leg}/{top}"
        metrics = _run_dual_slice(
            loaded,
            ohlcv,
            full,
            spy_df,
            total_cash=cash,
            legacy_pct=float(leg),
            top3_pct=float(top),
            window_start=start,
            window_end=end,
        )
        rows.append((label, metrics))
        print(
            f"{label:<12} {metrics['total_return_pct']:>+9.2f}% "
            f"{metrics['max_drawdown_pct']:>9.2f}% "
            f"{metrics['sharpe_ratio']:>8.2f} "
            f"{int(metrics['total_trades']):>8} "
            f"${metrics['final_equity']:>12,.2f}"
        )

    best_ret = max(rows, key=lambda x: x[1]["total_return_pct"])
    best_sharpe = max(rows, key=lambda x: x[1]["sharpe_ratio"])
    print("-" * 88)
    print(f"Best return : {best_ret[0]} ({best_ret[1]['total_return_pct']:+.2f}%)")
    print(f"Best Sharpe : {best_sharpe[0]} ({best_sharpe[1]['sharpe_ratio']:.2f})")
    print()

    print("=" * 88)
    print("WALK-FORWARD DUAL (60/40) - yfinance history".center(88))
    print("=" * 88)
    print(f"{'Window':<12} {'Return%':>10} {'MaxDD%':>10} {'Sharpe':>8} {'Trades':>8}")
    print("-" * 88)

    print("Fetching yfinance history (may take a minute)...")
    yf_full, yf_skipped = load_yfinance_watchlist_data(loaded, start=YFINANCE_WARMUP_START)
    if yf_skipped:
        print("Skipped:", ", ".join(yf_skipped))
    yf_spy = None
    if StrategyConfigMapper.use_spy_market_filter():
        try:
            yf_spy = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        except Exception:
            pass

    for label, wstart, wend in WALK_FORWARD_WINDOWS:
        window_ohlcv = {
            t: slice_ohlcv_window(yf_full[t], wstart, wend)
            for t in loaded
            if t in yf_full and len(slice_ohlcv_window(yf_full[t], wstart, wend)) >= 22
        }
        if len(window_ohlcv) < 2:
            print(f"{label:<12}  (skipped — insufficient data)")
            continue
        m = _run_dual_slice(
            list(window_ohlcv.keys()),
            window_ohlcv,
            yf_full,
            yf_spy,
            total_cash=cash,
            legacy_pct=60,
            top3_pct=40,
            window_start=wstart,
            window_end=wend,
        )
        print(
            f"{label:<12} {m['total_return_pct']:>+9.2f}% "
            f"{m['max_drawdown_pct']:>9.2f}% "
            f"{m['sharpe_ratio']:>8.2f} "
            f"{int(m['total_trades']):>8}"
        )
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
