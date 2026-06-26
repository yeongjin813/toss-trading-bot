"""
Alpha research: capital splits, Top3 ranking variants, pure Legacy vs Top3.

Compares full-period (2020-2026) and rolling OOS (6 folds) with prod friction.

Usage:
  python scripts/alpha_research.py
  python scripts/alpha_research.py --quick   # allocation sweep only
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from backtest_benchmarks import summarize_strategy_vs_benchmarks
from config import StrategyConfigMapper
from deployment_config import scaled_capital
from market_registry import BENCHMARK_TICKER, SECONDARY_BENCHMARK_TICKER, parse_watchlist
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
from walk_forward_research import ROLLING_OOS_FOLDS

START = "2020-01-01"
END = "2025-12-31"
CASH = 100_000.0
COMMISSION = 0.001
SLIPPAGE_BPS = 5.0

ALLOC_SPLITS = [
    (100, 0),
    (80, 20),
    (70, 30),
    (50, 50),
    (30, 70),
    (0, 100),
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Alpha research matrix")
    p.add_argument("--quick", action="store_true", help="Allocation sweep only")
    p.add_argument("--start", default=START)
    p.add_argument("--end", default=END)
    return p.parse_args()


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


def _metrics(combined: pd.Series, initial: float, years: float) -> dict[str, float]:
    if combined.empty or initial <= 0:
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "cagr_pct": 0.0,
        }
    final = float(combined.iloc[-1])
    return {
        "total_return_pct": (final / initial - 1.0) * 100.0,
        "max_drawdown_pct": compute_max_drawdown(combined),
        "sharpe_ratio": compute_sharpe_ratio(combined),
        "cagr_pct": ((final / initial) ** (1.0 / years) - 1.0) * 100.0 if years > 0 else 0.0,
    }


def _top3_settings(name: str) -> MomentumRankSettings:
    base = MomentumRankSettings.from_env().for_production().for_top3()
    base = replace(base, enabled=True, min_bars=min(base.min_bars, 60))
    if name == "composite":
        return base
    if name == "12m_only":
        return replace(
            base,
            weight_3m=0.0,
            weight_6m=0.0,
            weight_12m=1.0,
            weight_volume=0.0,
        )
    if name == "top5_equal":
        return replace(base, top_n=5)
    raise ValueError(name)


def run_dual(
    loaded: list[str],
    ohlcv_window: dict,
    full_ohlcv: dict,
    spy_df,
    qqq_df,
    vix_df,
    *,
    legacy_pct: float,
    top3_pct: float,
    top3_rank: str = "composite",
    window_start: str,
    window_end: str,
) -> dict[str, float | int]:
    total = legacy_pct + top3_pct
    if total <= 0:
        return {"total_return_pct": 0.0, "sharpe_ratio": 0.0, "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0}

    leg_cash = scaled_capital(CASH, legacy_pct / total)
    top_cash = scaled_capital(CASH, top3_pct / total)
    initial = leg_cash + top_cash

    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom = _top3_settings(top3_rank)

    leg_eq = pd.Series(dtype=float)
    top_eq = pd.Series(dtype=float)
    trades = 0

    if leg_cash > 0:
        leg_tickers = [t for t in loaded if t in ohlcv_window]
        legacy = run_portfolio_backtest(
            tickers=leg_tickers,
            ohlcv_by_ticker={t: ohlcv_window[t] for t in leg_tickers},
            initial_cash=leg_cash,
            commission_rate=COMMISSION,
            slippage_bps=SLIPPAGE_BPS,
            use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
            spy_df=spy_df,
            qqq_df=qqq_df,
            vix_df=vix_df,
            momentum_settings=legacy_mom,
        )
        leg_eq = _equity_series(legacy.equity_curve)
        trades += legacy.total_trades

    if top_cash > 0:
        top3 = run_top3_backtest(
            tickers=loaded,
            ohlcv_by_ticker=full_ohlcv,
            initial_cash=top_cash,
            commission_rate=COMMISSION,
            slippage_bps=SLIPPAGE_BPS,
            momentum_settings=top3_mom,
            window_start=window_start,
            window_end=window_end,
        )
        top_eq = _equity_series(top3.equity_curve)
        trades += top3.total_trades

    if leg_eq.empty and top_eq.empty:
        years = max((pd.Timestamp(window_end) - pd.Timestamp(window_start)).days / 365.25, 0.25)
        return {**_metrics(pd.Series(dtype=float), initial, years), "total_trades": 0}

    combined = pd.concat(
        [leg_eq.rename("legacy"), top_eq.rename("top3")],
        axis=1,
        sort=True,
    ).ffill().fillna({"legacy": leg_cash, "top3": top_cash}).sum(axis=1)

    years = max((pd.Timestamp(window_end) - pd.Timestamp(window_start)).days / 365.25, 0.25)
    m = _metrics(combined, initial, years)
    m["total_trades"] = trades
    return m


def _oos_mean(run_fn, folds: list) -> float:
    sharpes: list[float] = []
    for fold in folds:
        _, _, _, _, test_start, test_end = fold
        m = run_fn(test_start, test_end)
        sharpes.append(float(m["sharpe_ratio"]))
    return sum(sharpes) / len(sharpes) if sharpes else 0.0


def _print_table(title: str, rows: list[tuple[str, dict[str, float | int]]], prod_tag: str = "") -> None:
    width = 96
    print()
    print("=" * width)
    print(title.center(width))
    print("=" * width)
    print(f"{'Label':<28} {'Return%':>10} {'CAGR%':>8} {'Sharpe':>8} {'MaxDD%':>8} {'OOS Sh':>8}")
    print("-" * width)
    for label, m in rows:
        oos = m.get("oos_sharpe", 0.0)
        marker = " <-- prod" if label == prod_tag else ""
        print(
            f"{label:<28} {m['total_return_pct']:>+9.1f}% {m['cagr_pct']:>+7.1f}% "
            f"{m['sharpe_ratio']:>8.2f} {m['max_drawdown_pct']:>7.1f}% "
            f"{oos:>8.2f}{marker}"
        )
    print("=" * width)


def main() -> int:
    args = _parse_args()
    years_full = max((pd.Timestamp(args.end) - pd.Timestamp(args.start)).days / 365.25, 0.25)

    tickers = parse_watchlist(os.getenv("WATCHLIST"))
    print("Loading yfinance...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(tickers, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in tickers if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter()
        else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], args.start, args.end)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], args.start, args.end)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]

    def window_for(start: str, end: str) -> dict:
        return {
            t: slice_ohlcv_window(full_ohlcv[t], start, end)
            for t in loaded
            if len(slice_ohlcv_window(full_ohlcv[t], start, end)) >= 22
        }

    # --- Part 1: Capital allocation ---
    alloc_rows: list[tuple[str, dict]] = []
    for leg, top in ALLOC_SPLITS:
        label = f"Dual {leg}/{top}"

        def run_alloc(ws: str, we: str, _leg=leg, _top=top) -> dict[str, float | int]:
            w = window_for(ws, we)
            return run_dual(
                list(w.keys()),
                w,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                legacy_pct=float(_leg),
                top3_pct=float(_top),
                window_start=ws,
                window_end=we,
            )

        print(f"Running {label}...", flush=True)
        m = run_alloc(args.start, args.end)
        m["oos_sharpe"] = _oos_mean(lambda s, e: run_alloc(s, e), ROLLING_OOS_FOLDS)
        alloc_rows.append((label, m))

    _print_table(
        f"PART 1 - Capital split  {args.start} -> {args.end}  ${CASH:,.0f}",
        alloc_rows,
        prod_tag="Dual 70/30",
    )

    prod_return = float(next(m for l, m in alloc_rows if l == "Dual 70/30")["total_return_pct"])
    bench = summarize_strategy_vs_benchmarks(
        prod_return,
        {t: full_window[t] for t in loaded},
        spy_df=spy_df,
        window_start=args.start,
        window_end=args.end,
    )
    print(
        f"Benchmarks (equal-weight 25): B&H {bench.get('buy_hold_return_pct', 0):+.1f}%  "
        f"SPY {bench.get('spy_return_pct', 0):+.1f}%  "
        f"Alpha vs B&H {bench.get('alpha_vs_buy_hold_pct', 0):+.1f}pp (Dual 70/30 return)"
    )

    if args.quick:
        return 0

    # --- Part 2: Top3 ranking variants at 70/30 ---
    rank_rows: list[tuple[str, dict]] = []
    for rank_name in ("composite", "12m_only", "top5_equal"):
        label = f"70/30 rank={rank_name}"

        def run_rank(ws: str, we: str, _rn=rank_name) -> dict[str, float | int]:
            w = window_for(ws, we)
            return run_dual(
                list(w.keys()),
                w,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                legacy_pct=70,
                top3_pct=30,
                top3_rank=_rn,
                window_start=ws,
                window_end=we,
            )

        print(f"Running {label}...", flush=True)
        m = run_rank(args.start, args.end)
        m["oos_sharpe"] = _oos_mean(lambda s, e: run_rank(s, e), ROLLING_OOS_FOLDS)
        rank_rows.append((label, m))

    _print_table(
        f"PART 2 - Top3 ranking @ 70/30  {args.start} -> {args.end}",
        rank_rows,
        prod_tag="70/30 rank=composite",
    )

    # --- Part 3: Pure 100% archetypes ---
    pure_rows: list[tuple[str, dict]] = []
    pure_specs = [
        ("100% Legacy breakout", 100, 0, "composite"),
        ("100% Top3 composite", 0, 100, "composite"),
        ("100% Top3 12m_only", 0, 100, "12m_only"),
        ("100% Top3 top5", 0, 100, "top5_equal"),
    ]
    for label, leg, top, rank in pure_specs:

        def run_pure(ws: str, we: str, _leg=leg, _top=top, _rank=rank) -> dict[str, float | int]:
            w = window_for(ws, we)
            return run_dual(
                list(w.keys()),
                w,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                legacy_pct=float(_leg),
                top3_pct=float(_top),
                top3_rank=_rank,
                window_start=ws,
                window_end=we,
            )

        print(f"Running {label}...", flush=True)
        m = run_pure(args.start, args.end)
        m["oos_sharpe"] = _oos_mean(lambda s, e: run_pure(s, e), ROLLING_OOS_FOLDS)
        pure_rows.append((label, m))

    _print_table(
        f"PART 3 - Pure strategies  {args.start} -> {args.end}",
        pure_rows,
    )

    # Recommendation
    all_labeled = alloc_rows + rank_rows + pure_rows
    best_full = max(all_labeled, key=lambda x: float(x[1]["sharpe_ratio"]))
    best_oos = max(all_labeled, key=lambda x: float(x[1].get("oos_sharpe", 0)))
    best_ret = max(all_labeled, key=lambda x: float(x[1]["total_return_pct"]))

    print()
    print("RESEARCH SUMMARY")
    print("-" * 60)
    print(f"Best full-period Sharpe : {best_full[0]} ({best_full[1]['sharpe_ratio']:.2f})")
    print(f"Best mean OOS Sharpe    : {best_oos[0]} ({best_oos[1].get('oos_sharpe', 0):.2f})")
    print(f"Best full-period return : {best_ret[0]} ({best_ret[1]['total_return_pct']:+.1f}%)")
    print(f"Current prod            : Dual 70/30 composite")
    prod = next((m for l, m in alloc_rows if l == "Dual 70/30"), None)
    if prod and best_oos[0] != "Dual 70/30":
        print(
            f"OOS gap vs prod         : {float(best_oos[1].get('oos_sharpe', 0)) - float(prod.get('oos_sharpe', 0)):+.2f} Sharpe ({best_oos[0]})"
        )
    print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
