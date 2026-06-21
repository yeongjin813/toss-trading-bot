"""
Dual Legacy/Top3 capital split sweep — production-realistic settings.

Usage:
  python scripts/dual_split_sweep_2020.py
  python scripts/dual_split_sweep_2020.py --start 2020-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
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

SPLITS = [
    (100, 0),
    (70, 30),
    (60, 40),
    (50, 50),
    (40, 60),
    (30, 70),
    (0, 100),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dual split sweep with prod backtest settings")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--commission", type=float, default=0.001)
    p.add_argument("--slippage-bps", type=float, default=5.0)
    return p.parse_args()


def _metrics_from_combined(combined: pd.Series, initial: float) -> dict[str, float]:
    if combined.empty or initial <= 0:
        return {"total_return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0, "final_equity": initial}
    final = float(combined.iloc[-1])
    return {
        "total_return_pct": (final / initial - 1.0) * 100.0,
        "max_drawdown_pct": compute_max_drawdown(combined),
        "sharpe_ratio": compute_sharpe_ratio(combined),
        "final_equity": final,
    }


def _equity_series(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float)
    if "equity" not in frame.columns:
        return pd.Series(dtype=float)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame["equity"].astype(float)
    if "date" in frame.columns:
        idx = pd.to_datetime(frame["date"])
        return pd.Series(frame["equity"].astype(float).values, index=idx)
    return frame["equity"].astype(float)


def main() -> int:
    args = _parse_args()
    tickers = parse_watchlist(os.getenv("WATCHLIST"))
    cash = args.cash

    print("Loading yfinance history...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(tickers, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in tickers if t in full_ohlcv]
    if len(loaded) < 3:
        print("Need at least 3 tickers.")
        return 1

    ohlcv = {
        t: slice_ohlcv_window(full_ohlcv[t], args.start, args.end)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], args.start, args.end)) >= 22
    }
    loaded = list(ohlcv.keys())

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = None
    if use_spy and StrategyConfigMapper.use_qqq_regime_filter():
        qqq_df = fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    mom = MomentumRankSettings.from_env().for_production()
    legacy_mom = replace(mom, enabled=False)
    top3_mom = replace(mom.for_top3(), enabled=True, min_bars=min(mom.min_bars, 60))

    print(f"Running Legacy + Top3 once (${cash:,.0f} each pool reference)...")
    legacy = run_portfolio_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=cash,
        commission_rate=args.commission,
        slippage_bps=args.slippage_bps,
        use_spy_market_filter=use_spy,
        spy_df=spy_df,
        qqq_df=qqq_df,
        vix_df=vix_df,
        momentum_settings=legacy_mom,
    )
    top3 = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=cash,
        commission_rate=args.commission,
        slippage_bps=args.slippage_bps,
        momentum_settings=top3_mom,
        window_start=args.start,
        window_end=args.end,
    )

    leg_series = _equity_series(legacy.equity_curve)
    top_series = _equity_series(top3.equity_curve)

    width = 92
    label = f"{args.start} -> {args.end}"
    print()
    print("=" * width)
    print(f"DUAL SPLIT SWEEP (prod settings)  {label}  ${cash:,.0f}".center(width))
    print(f"Legacy only: {legacy.total_return_pct:+.1f}% Sharpe {legacy.sharpe_ratio:.2f} MaxDD {legacy.max_drawdown_pct:.1f}%".center(width))
    print(f"Top3 only  : {top3.total_return_pct:+.1f}% Sharpe {top3.sharpe_ratio:.2f} MaxDD {top3.max_drawdown_pct:.1f}%".center(width))
    print("=" * width)
    print(f"{'Legacy/Top3':<12} {'Return%':>10} {'MaxDD%':>10} {'Sharpe':>8} {'Final$':>14} {'CAGR%':>8}")
    print("-" * width)

    rows: list[tuple[str, dict]] = []
    years = max(
        (pd.Timestamp(args.end) - pd.Timestamp(args.start)).days / 365.25,
        0.25,
    )

    for leg_pct, top_pct in SPLITS:
        total = leg_pct + top_pct
        if total == 0:
            continue
        leg_cash = scaled_capital(cash, leg_pct / total)
        top_cash = scaled_capital(cash, top_pct / total)
        leg_scale = leg_cash / cash if cash > 0 else 0.0
        top_scale = top_cash / cash if cash > 0 else 0.0

        if leg_pct == 0:
            combined = top_series * top_scale
            initial = top_cash
            trades = top3.total_trades
        elif top_pct == 0:
            combined = leg_series * leg_scale
            initial = leg_cash
            trades = legacy.total_trades
        else:
            combined = pd.concat(
                [
                    (leg_series * leg_scale).rename("legacy"),
                    (top_series * top_scale).rename("top3"),
                ],
                axis=1,
            ).sort_index()
            combined = combined.ffill().fillna({"legacy": leg_cash, "top3": top_cash}).sum(axis=1)
            initial = leg_cash + top_cash
            trades = legacy.total_trades + top3.total_trades

        m = _metrics_from_combined(combined, initial)
        cagr = ((m["final_equity"] / initial) ** (1.0 / years) - 1.0) * 100.0 if initial > 0 else 0.0
        m["cagr_pct"] = cagr
        m["total_trades"] = trades
        tag = f"{leg_pct}/{top_pct}"
        rows.append((tag, m))
        marker = " <-- prod" if tag == "70/30" else ""
        print(
            f"{tag:<12} {m['total_return_pct']:>+9.1f}% "
            f"{m['max_drawdown_pct']:>9.1f}% "
            f"{m['sharpe_ratio']:>8.2f} "
            f"${m['final_equity']:>12,.0f} "
            f"{cagr:>+7.1f}%{marker}"
        )

    print("-" * width)
    best_ret = max(rows, key=lambda x: x[1]["total_return_pct"])
    best_sharpe = max(rows, key=lambda x: x[1]["sharpe_ratio"])
    best_cagr = max(rows, key=lambda x: x[1]["cagr_pct"])
    lowest_dd = min(rows, key=lambda x: x[1]["max_drawdown_pct"])
    print(f"Best total return : {best_ret[0]} ({best_ret[1]['total_return_pct']:+.1f}%)")
    print(f"Best CAGR         : {best_cagr[0]} ({best_cagr[1]['cagr_pct']:+.1f}%)")
    print(f"Best Sharpe       : {best_sharpe[0]} ({best_sharpe[1]['sharpe_ratio']:.2f})")
    print(f"Lowest MaxDD      : {lowest_dd[0]} ({lowest_dd[1]['max_drawdown_pct']:.1f}%)")
    print("=" * width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
