"""
Compare legacy vs enhanced Top3 momentum ranking on the same OHLCV window.

Usage:
  python scripts/compare_momentum_ranking.py --yfinance
  python scripts/compare_momentum_ranking.py --yfinance --start 2020-01-01 --end 2025-12-31
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from momentum_ranker import MomentumRankSettings
from momentum_selection import print_momentum_ranking_comparison_table
from top3_backtest import analytics_for_top3_result, run_top3_backtest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Legacy vs enhanced Top3 momentum ranking")
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--commission", type=float, default=0.001)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--yfinance", action="store_true")
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers (default: WATCHLIST env or built-in sample)",
    )
    return parser.parse_args()


def _load_ohlcv(args: argparse.Namespace) -> tuple[list[str], dict]:
    from run_backtest import (
        YFINANCE_WARMUP_START,
        fetch_yfinance_ohlcv,
        load_watchlist_data,
        load_yfinance_watchlist_data,
    )
    from market_registry import parse_watchlist

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = parse_watchlist(os.getenv("WATCHLIST"))

    if args.yfinance:
        ohlcv, skipped = load_yfinance_watchlist_data(
            tickers,
            start=args.start or YFINANCE_WARMUP_START,
            end=args.end,
        )
        if skipped:
            print("Skipped:", ", ".join(skipped))
        loaded = [t for t in tickers if t in ohlcv]
        return loaded, ohlcv

    ohlcv, skipped = load_watchlist_data(tickers)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in tickers if t in ohlcv]
    return loaded, ohlcv


def _base_settings() -> MomentumRankSettings:
    base = MomentumRankSettings.from_env()
    return replace(
        base,
        enabled=True,
        require_near_52w_high=base.require_near_52w_high,
        sector_diversify=base.sector_diversify,
        max_per_sector=base.max_per_sector,
        min_bars=min(base.min_bars, 60),
    )


def main() -> int:
    args = _parse_args()
    loaded, ohlcv = _load_ohlcv(args)
    if len(loaded) < 3:
        print("Need at least 3 tickers with OHLCV data.")
        return 1

    base = _base_settings()
    legacy_cfg = replace(base, ranking_mode="legacy", dynamic_rebalance_only=False, inverse_vol_weighting=False)
    enhanced_cfg = replace(
        base,
        ranking_mode="enhanced",
        dynamic_rebalance_only=True,
        inverse_vol_weighting=True,
    )

    window_label = ""
    if args.start or args.end:
        window_label = f"{args.start or '...'} → {args.end or '...'}"

    print(f"Tickers ({len(loaded)}): {', '.join(loaded)}")
    print(f"Capital: ${args.cash:,.2f} | Commission: {args.commission * 100:.2f}%")
    print()

    legacy_result = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=args.cash,
        commission_rate=args.commission,
        momentum_settings=legacy_cfg,
        window_start=args.start,
        window_end=args.end,
    )
    enhanced_result = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=ohlcv,
        initial_cash=args.cash,
        commission_rate=args.commission,
        momentum_settings=enhanced_cfg,
        window_start=args.start,
        window_end=args.end,
    )

    legacy_metrics = analytics_for_top3_result(legacy_result)
    enhanced_metrics = analytics_for_top3_result(enhanced_result)
    print_momentum_ranking_comparison_table(
        legacy_metrics,
        enhanced_metrics,
        window_label=window_label,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
