"""
Rolling train/test walk-forward for Top3 momentum variants.

Example:
  python scripts/walk_forward_momentum.py --yfinance
  python scripts/walk_forward_momentum.py --yfinance --cash 40000 --slippage-bps 5
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_registry import parse_watchlist
from walk_forward_research import (
    ROLLING_OOS_FOLDS,
    print_oos_summary_table,
    run_rolling_oos_fold,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rolling OOS walk-forward for Top3 momentum variants",
    )
    parser.add_argument("--cash", type=float, default=10_000.0)
    parser.add_argument("--commission", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--yfinance", action="store_true")
    parser.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated tickers (default: WATCHLIST env)",
    )
    return parser.parse_args()


def _load_ohlcv(args: argparse.Namespace) -> tuple[list[str], dict]:
    from run_backtest import YFINANCE_WARMUP_START, load_yfinance_watchlist_data

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = parse_watchlist(os.getenv("WATCHLIST"))

    if not args.yfinance:
        print("This script requires --yfinance for extended history.")
        return [], {}

    ohlcv, skipped = load_yfinance_watchlist_data(
        tickers,
        start="2016-01-01",
    )
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in tickers if t in ohlcv]
    return loaded, ohlcv


def main() -> int:
    args = _parse_args()
    loaded, ohlcv = _load_ohlcv(args)
    if len(loaded) < 3:
        print("Need at least 3 tickers with OHLCV data.")
        return 1

    print(f"Tickers ({len(loaded)}): {', '.join(loaded)}")
    print(f"Capital: ${args.cash:,.2f} | Commission: {args.commission * 100:.2f}%")
    print(f"Slippage: {args.slippage_bps:.1f} bps | Folds: {len(ROLLING_OOS_FOLDS)}")
    print()

    rows: list[dict] = []
    for fold in ROLLING_OOS_FOLDS:
        train_label, _, _, test_label, _, _ = fold
        row = run_rolling_oos_fold(
            loaded,
            ohlcv,
            fold,
            initial_cash=args.cash,
            commission_rate=args.commission,
            slippage_bps=args.slippage_bps,
        )
        rows.append(row)
        win = row["winner"]
        tm = row["test_metrics"][win]
        bm = row["test_metrics"]["legacy_equal"]
        print(
            f"{train_label} -> {test_label}: winner={win} | "
            f"OOS Sharpe {tm['sharpe']:.2f} (baseline {bm['sharpe']:.2f})"
        )

    print()
    print_oos_summary_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
