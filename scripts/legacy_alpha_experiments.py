"""
Legacy alpha experiments vs baseline (Dual 70/30, 2020-2026).

  1. baseline     — Legacy 25 + Top3 25 (momentum gate OFF)
  2. legacy_10    — Legacy 10 high-beta/mega only + Top3 25
  3. top5_gate    — Legacy 25 with momentum Top-5 new-BUY gate + Top3 25

Usage:
  python scripts/legacy_alpha_experiments.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config import StrategyConfigMapper
from deployment_config import scaled_capital
from market_registry import (
    BENCHMARK_TICKER,
    SECONDARY_BENCHMARK_TICKER,
    parse_watchlist,
)
from momentum_ranker import MomentumRankSettings
from portfolio_backtest import compute_max_drawdown, compute_sharpe_ratio, run_portfolio_backtest
from run_backtest import (
    YFINANCE_WARMUP_START,
    fetch_yfinance_ohlcv,
    load_vix_frame,
    load_yfinance_watchlist_data,
    slice_ohlcv_window,
)
from top3_backtest import run_top3_backtest

# Explicit MEGA + HIGH_BETA + MOMENTUM regimes (skip DEFAULT-only names for Legacy)
LEGACY_FOCUS_10 = [
    "AAPL",
    "MSFT",
    "NVDA",
    "META",
    "AVGO",
    "NFLX",
    "AMD",
    "TSLA",
    "PLTR",
    "CRWD",
]

START = "2020-01-01"
END = "2025-12-31"
CASH = 100_000.0
LEGACY_PCT = 70.0
TOP3_PCT = 30.0
COMMISSION = 0.001
SLIPPAGE_BPS = 5.0


def _equity_series(frame: pd.DataFrame) -> pd.Series:
    if frame.empty or "equity" not in frame.columns:
        return pd.Series(dtype=float)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame["equity"].astype(float)
    if "date" in frame.columns:
        return pd.Series(
            frame["equity"].astype(float).values,
            index=pd.to_datetime(frame["date"]),
        )
    return frame["equity"].astype(float)


def _combine_dual(
    legacy_equity: pd.Series,
    top3_equity: pd.Series,
    *,
    leg_cash: float,
    top_cash: float,
) -> pd.Series:
    """Sum pre-sized pool equity curves (legacy already at leg_cash, top3 at top_cash)."""
    combined = pd.concat(
        [legacy_equity.rename("legacy"), top3_equity.rename("top3")],
        axis=1,
        sort=True,
    )
    combined = combined.ffill().fillna({"legacy": leg_cash, "top3": top_cash})
    return combined.sum(axis=1)


def _dual_metrics(
    legacy_equity: pd.Series,
    top3_equity: pd.Series,
    *,
    leg_cash: float,
    top_cash: float,
    legacy_trades: int,
    top3_trades: int,
) -> dict[str, float]:
    initial = leg_cash + top_cash
    combined = _combine_dual(
        legacy_equity,
        top3_equity,
        leg_cash=leg_cash,
        top_cash=top_cash,
    )
    if combined.empty:
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "cagr_pct": 0.0,
            "final_equity": initial,
            "total_trades": 0,
        }
    final = float(combined.iloc[-1])
    years = max((pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25, 0.25)
    return {
        "total_return_pct": (final / initial - 1.0) * 100.0,
        "max_drawdown_pct": compute_max_drawdown(combined),
        "sharpe_ratio": compute_sharpe_ratio(combined),
        "cagr_pct": ((final / initial) ** (1.0 / years) - 1.0) * 100.0,
        "final_equity": final,
        "total_trades": legacy_trades + top3_trades,
    }


def _run_scenario(
    label: str,
    *,
    legacy_tickers: list[str],
    top3_tickers: list[str],
    legacy_momentum: MomentumRankSettings,
    ohlcv_window: dict,
    full_ohlcv: dict,
    spy_df,
    qqq_df,
    vix_df,
) -> tuple[str, dict[str, float]]:
    leg_cash = scaled_capital(CASH, LEGACY_PCT / (LEGACY_PCT + TOP3_PCT))
    top_cash = scaled_capital(CASH, TOP3_PCT / (LEGACY_PCT + TOP3_PCT))
    top3_mom = MomentumRankSettings.from_env().for_production().for_top3()
    top3_mom = replace(top3_mom, enabled=True, min_bars=min(top3_mom.min_bars, 60))

    legacy = run_portfolio_backtest(
        tickers=legacy_tickers,
        ohlcv_by_ticker={t: ohlcv_window[t] for t in legacy_tickers if t in ohlcv_window},
        initial_cash=leg_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE_BPS,
        use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
        spy_df=spy_df,
        qqq_df=qqq_df,
        vix_df=vix_df,
        momentum_settings=legacy_momentum,
    )
    top3 = run_top3_backtest(
        tickers=top3_tickers,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE_BPS,
        momentum_settings=top3_mom,
        window_start=START,
        window_end=END,
    )
    metrics = _dual_metrics(
        _equity_series(legacy.equity_curve),
        _equity_series(top3.equity_curve),
        leg_cash=leg_cash,
        top_cash=top_cash,
        legacy_trades=legacy.total_trades,
        top3_trades=top3.total_trades,
    )
    metrics["legacy_return_pct"] = legacy.total_return_pct
    metrics["top3_return_pct"] = top3.total_return_pct
    return label, metrics


def main() -> int:
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    print("Loading yfinance...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]
    ohlcv_window = {
        t: slice_ohlcv_window(full_ohlcv[t], START, END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], START, END)) >= 22
    }
    loaded = [t for t in loaded if t in ohlcv_window]

    focus = [t for t in LEGACY_FOCUS_10 if t in ohlcv_window]
    if len(focus) < 8:
        print("Insufficient focus tickers.")
        return 1

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter()
        else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    base_mom = MomentumRankSettings.from_env().for_production()
    legacy_off = replace(base_mom, enabled=False)
    legacy_top5 = replace(base_mom, enabled=True, top_n=5)

    scenarios = [
        ("baseline (L25 gate OFF)", loaded, loaded, legacy_off),
        (f"legacy_10 ({','.join(focus[:3])}...)", focus, loaded, legacy_off),
        ("top5_gate (L25 Top5 BUY)", loaded, loaded, legacy_top5),
        (f"legacy_10+top5", focus, loaded, legacy_top5),
    ]

    print()
    width = 100
    print("=" * width)
    print(f"LEGACY ALPHA EXPERIMENTS  {START} -> {END}  Dual {LEGACY_PCT:.0f}/{TOP3_PCT:.0f}  ${CASH:,.0f}".center(width))
    print("=" * width)
    print(
        f"{'Scenario':<28} {'Dual Ret%':>10} {'CAGR%':>8} {'Sharpe':>8} "
        f"{'MaxDD%':>8} {'Trades':>8} {'Leg Ret%':>10} {'Top3 Ret%':>10}"
    )
    print("-" * width)

    rows: list[tuple[str, dict]] = []
    for label, leg_tickers, top_tickers, leg_mom in scenarios:
        print(f"Running {label}...", flush=True)
        tag, m = _run_scenario(
            label,
            legacy_tickers=leg_tickers,
            top3_tickers=top_tickers,
            legacy_momentum=leg_mom,
            ohlcv_window=ohlcv_window,
            full_ohlcv=full_ohlcv,
            spy_df=spy_df,
            qqq_df=qqq_df,
            vix_df=vix_df,
        )
        rows.append((tag, m))
        prod = " <-- prod" if tag.startswith("baseline") else ""
        print(
            f"{tag:<28} {m['total_return_pct']:>+9.1f}% {m['cagr_pct']:>+7.1f}% "
            f"{m['sharpe_ratio']:>8.2f} {m['max_drawdown_pct']:>7.1f}% "
            f"{int(m['total_trades']):>8} {m['legacy_return_pct']:>+9.1f}% "
            f"{m['top3_return_pct']:>+9.1f}%{prod}"
        )

    print("-" * width)
    best = max(rows, key=lambda x: x[1]["sharpe_ratio"])
    best_ret = max(rows, key=lambda x: x[1]["total_return_pct"])
    print(f"Best Sharpe : {best[0]} ({best[1]['sharpe_ratio']:.2f})")
    print(f"Best return : {best_ret[0]} ({best_ret[1]['total_return_pct']:+.1f}%)")
    print("=" * width)
    print(f"Legacy focus 10: {', '.join(focus)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
