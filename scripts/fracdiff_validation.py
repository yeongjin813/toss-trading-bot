"""
Fractional-diff Top3 ranking validation vs legacy (dual 70/30 + Top3-only).

Usage:
  python scripts/fracdiff_validation.py
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
from top3_backtest import analytics_for_top3_result, run_top3_backtest
from walk_forward_research import ROLLING_OOS_FOLDS

START = "2020-01-01"
END = "2025-12-31"
CASH = 100_000.0
LEGACY_PCT = 70.0
TOP3_PCT = 30.0
COMMISSION = 0.001
SLIPPAGE_BPS = 5.0

VARIANTS = ("baseline", "fracdiff_top3")


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


def _top3_momentum(rank_mode: str) -> MomentumRankSettings:
    base = MomentumRankSettings.from_env().for_production().for_top3()
    return replace(
        base,
        enabled=True,
        ranking_mode="legacy" if rank_mode == "baseline" else "fracdiff",
        min_bars=min(base.min_bars, 60),
        dynamic_rebalance_only=False,
        inverse_vol_weighting=False,
    )


def _dual_metrics(
    legacy_equity: pd.Series,
    top3_equity: pd.Series,
    *,
    leg_cash: float,
    top_cash: float,
) -> dict[str, float]:
    initial = leg_cash + top_cash
    combined = pd.concat(
        [legacy_equity.rename("legacy"), top3_equity.rename("top3")],
        axis=1,
        sort=True,
    ).ffill().fillna({"legacy": leg_cash, "top3": top_cash}).sum(axis=1)
    if combined.empty:
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "cagr_pct": 0.0,
        }
    final = float(combined.iloc[-1])
    years = max((pd.Timestamp(END) - pd.Timestamp(START)).days / 365.25, 0.25)
    return {
        "total_return_pct": (final / initial - 1.0) * 100.0,
        "max_drawdown_pct": compute_max_drawdown(combined),
        "sharpe_ratio": compute_sharpe_ratio(combined),
        "cagr_pct": ((final / initial) ** (1.0 / years) - 1.0) * 100.0,
    }


def _run_dual_window(
    loaded: list[str],
    ohlcv_window: dict,
    full_ohlcv: dict,
    spy_df,
    qqq_df,
    vix_df,
    *,
    rank_mode: str,
    window_start: str,
    window_end: str,
) -> dict[str, float]:
    leg_cash = scaled_capital(CASH, LEGACY_PCT / (LEGACY_PCT + TOP3_PCT))
    top_cash = scaled_capital(CASH, TOP3_PCT / (LEGACY_PCT + TOP3_PCT))
    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom = _top3_momentum(rank_mode)

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
    return _dual_metrics(
        _equity_series(legacy.equity_curve),
        _equity_series(top3.equity_curve),
        leg_cash=leg_cash,
        top_cash=top_cash,
    )


def _run_top3_only(
    loaded: list[str],
    full_ohlcv: dict,
    *,
    rank_mode: str,
    window_start: str,
    window_end: str,
) -> dict[str, float]:
    result = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=CASH,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE_BPS,
        momentum_settings=_top3_momentum(rank_mode),
        window_start=window_start,
        window_end=window_end,
    )
    return analytics_for_top3_result(result)


def main() -> int:
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    print("Loading yfinance...")
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter()
        else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], START, END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], START, END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]

    width = 98
    print()
    print("=" * width)
    print(f"FRACDIFF VALIDATION  Dual {LEGACY_PCT:.0f}/{TOP3_PCT:.0f}  {START} -> {END}".center(width))
    print("=" * width)
    print(f"{'Variant':<16} {'Return%':>10} {'CAGR%':>8} {'Sharpe':>8} {'MaxDD%':>8}")
    print("-" * width)

    full_results: dict[str, dict[str, float]] = {}
    for name in VARIANTS:
        rank_mode = "baseline" if name == "baseline" else "fracdiff"
        print(f"Running {name} (dual full period)...", flush=True)
        m = _run_dual_window(
            loaded,
            full_window,
            full_ohlcv,
            spy_df,
            qqq_df,
            vix_df,
            rank_mode=rank_mode,
            window_start=START,
            window_end=END,
        )
        full_results[name] = m
        marker = " <-- prod" if name == "baseline" else ""
        print(
            f"{name:<16} {m['total_return_pct']:>+9.1f}% {m['cagr_pct']:>+7.1f}% "
            f"{m['sharpe_ratio']:>8.2f} {m['max_drawdown_pct']:>7.1f}%{marker}"
        )

    print()
    print("TOP3-ONLY (100% momentum slice)".center(width))
    print("-" * width)
    top3_results: dict[str, dict[str, float]] = {}
    for name in VARIANTS:
        rank_mode = "baseline" if name == "baseline" else "fracdiff"
        print(f"Running {name} (top3 only)...", flush=True)
        m = _run_top3_only(loaded, full_ohlcv, rank_mode=rank_mode, window_start=START, window_end=END)
        top3_results[name] = m
        print(
            f"{name:<16} {m['total_return_pct']:>+9.1f}% {m['cagr_pct']:>+7.1f}% "
            f"{m['sharpe']:>8.2f} {m['max_drawdown_pct']:>7.1f}%"
        )

    print()
    print("=" * width)
    print("ROLLING OOS (mean test-window Sharpe)".center(width))
    print("=" * width)
    oos_by_variant: dict[str, list[dict[str, float]]] = {k: [] for k in VARIANTS}
    for fold in ROLLING_OOS_FOLDS:
        _, _, _, test_label, test_start, test_end = fold
        window_ohlcv = {
            t: slice_ohlcv_window(full_ohlcv[t], test_start, test_end)
            for t in loaded
            if len(slice_ohlcv_window(full_ohlcv[t], test_start, test_end)) >= 22
        }
        if len(window_ohlcv) < 3:
            continue
        for name in VARIANTS:
            rank_mode = "baseline" if name == "baseline" else "fracdiff"
            m = _run_dual_window(
                list(window_ohlcv.keys()),
                window_ohlcv,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                rank_mode=rank_mode,
                window_start=test_start,
                window_end=test_end,
            )
            oos_by_variant[name].append(m)
            print(f"  {name:<14} test {test_label}: Sharpe {m['sharpe_ratio']:.2f}")

    base_rows = oos_by_variant["baseline"]
    base_mean = sum(r["sharpe_ratio"] for r in base_rows) / max(len(base_rows), 1)
    frac_rows = oos_by_variant["fracdiff_top3"]
    frac_mean = sum(r["sharpe_ratio"] for r in frac_rows) / max(len(frac_rows), 1)

    print("-" * width)
    print(f"Mean OOS Sharpe baseline     : {base_mean:.2f}")
    print(f"Mean OOS Sharpe fracdiff_top3: {frac_mean:.2f}  (delta {frac_mean - base_mean:+.2f})")
    print("=" * width)

    baseline_wins_full = (
        full_results["baseline"]["sharpe_ratio"] >= full_results["fracdiff_top3"]["sharpe_ratio"]
    )
    oos_delta = frac_mean - base_mean
    if baseline_wins_full and oos_delta < 0.05:
        print("Recommendation: keep legacy Top3 ranking in production.")
    else:
        print(f"Recommendation: review fracdiff_top3 after manual check (research only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
