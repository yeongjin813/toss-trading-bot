"""
TSM (absolute momentum) validation: full-period + rolling OOS vs baseline.

Variants:
  baseline   — no TSM (current prod)
  legacy_tsm — USE_TSM_ENTRY_GATE (12M return > 0 on Legacy BUY)
  top3_tsm   — USE_TSM_TOP3_FILTER (Top3 rank pool)
  both_tsm   — both gates

Usage:
  python scripts/tsm_validation.py
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

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
from top3_backtest import run_top3_backtest
from walk_forward_research import ROLLING_OOS_FOLDS

START = "2020-01-01"
END = "2025-12-31"
CASH = 100_000.0
LEGACY_PCT = 70.0
TOP3_PCT = 30.0
COMMISSION = 0.001
SLIPPAGE_BPS = 5.0

VARIANTS: dict[str, dict[str, str]] = {
    "baseline": {"USE_TSM_ENTRY_GATE": "false", "USE_TSM_TOP3_FILTER": "false"},
    "legacy_tsm": {"USE_TSM_ENTRY_GATE": "true", "USE_TSM_TOP3_FILTER": "false"},
    "top3_tsm": {"USE_TSM_ENTRY_GATE": "false", "USE_TSM_TOP3_FILTER": "true"},
    "both_tsm": {"USE_TSM_ENTRY_GATE": "true", "USE_TSM_TOP3_FILTER": "true"},
}


@contextmanager
def tsm_env(overrides: dict[str, str]) -> Iterator[None]:
    prior = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, val in prior.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


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
    window_start: str,
    window_end: str,
) -> dict[str, float]:
    leg_cash = scaled_capital(CASH, LEGACY_PCT / (LEGACY_PCT + TOP3_PCT))
    top_cash = scaled_capital(CASH, TOP3_PCT / (LEGACY_PCT + TOP3_PCT))

    base_mom = MomentumRankSettings.from_env().for_production()
    legacy_mom = replace(base_mom, enabled=False)
    top3_mom = replace(
        base_mom.for_top3(),
        enabled=True,
        min_bars=min(base_mom.min_bars, 60),
        require_tsm=StrategyConfigMapper.use_tsm_top3_filter(),
        tsm_lookback=StrategyConfigMapper.tsm_lookback_days(),
        tsm_min_return=StrategyConfigMapper.tsm_min_return(),
    )

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
    print(f"TSM VALIDATION  Dual {LEGACY_PCT:.0f}/{TOP3_PCT:.0f}  {START} -> {END}".center(width))
    print("=" * width)
    print(f"{'Variant':<14} {'Return%':>10} {'CAGR%':>8} {'Sharpe':>8} {'MaxDD%':>8}")
    print("-" * width)

    full_results: dict[str, dict[str, float]] = {}
    for name, env_overrides in VARIANTS.items():
        print(f"Running {name} (full period)...", flush=True)
        with tsm_env(env_overrides):
            m = _run_dual_window(
                loaded,
                full_window,
                full_ohlcv,
                spy_df,
                qqq_df,
                vix_df,
                window_start=START,
                window_end=END,
            )
        full_results[name] = m
        marker = " <-- prod" if name == "baseline" else ""
        print(
            f"{name:<14} {m['total_return_pct']:>+9.1f}% {m['cagr_pct']:>+7.1f}% "
            f"{m['sharpe_ratio']:>8.2f} {m['max_drawdown_pct']:>7.1f}%{marker}"
        )

    print()
    print("=" * width)
    print("ROLLING OOS (mean test-window Sharpe / CAGR by variant)".center(width))
    print("=" * width)
    print(f"{'Variant':<14} {'Mean Sharpe':>12} {'Mean CAGR%':>12} {'vs base Sharpe':>16}")
    print("-" * width)

    oos_by_variant: dict[str, list[dict[str, float]]] = {k: [] for k in VARIANTS}
    for fold in ROLLING_OOS_FOLDS:
        _, train_start, train_end, test_label, test_start, test_end = fold
        window_ohlcv = {
            t: slice_ohlcv_window(full_ohlcv[t], test_start, test_end)
            for t in loaded
            if len(slice_ohlcv_window(full_ohlcv[t], test_start, test_end)) >= 22
        }
        if len(window_ohlcv) < 3:
            continue
        for name, env_overrides in VARIANTS.items():
            with tsm_env(env_overrides):
                m = _run_dual_window(
                    list(window_ohlcv.keys()),
                    window_ohlcv,
                    full_ohlcv,
                    spy_df,
                    qqq_df,
                    vix_df,
                    window_start=test_start,
                    window_end=test_end,
                )
            oos_by_variant[name].append(m)
            print(f"  {name:<12} test {test_label}: Sharpe {m['sharpe_ratio']:.2f}  CAGR {m['cagr_pct']:+.1f}%")

    base_mean_sharpe = sum(r["sharpe_ratio"] for r in oos_by_variant["baseline"]) / max(
        len(oos_by_variant["baseline"]), 1
    )
    for name in VARIANTS:
        rows = oos_by_variant[name]
        if not rows:
            continue
        mean_sh = sum(r["sharpe_ratio"] for r in rows) / len(rows)
        mean_cagr = sum(r["cagr_pct"] for r in rows) / len(rows)
        delta = mean_sh - base_mean_sharpe
        print(
            f"{name:<14} {mean_sh:>12.2f} {mean_cagr:>+11.1f}% {delta:>+16.2f}"
        )

    print("-" * width)
    best_full = max(full_results.items(), key=lambda x: x[1]["sharpe_ratio"])
    best_oos = max(
        ((n, sum(r["sharpe_ratio"] for r in rows) / len(rows)) for n, rows in oos_by_variant.items() if rows),
        key=lambda x: x[1],
    )
    print(f"Best full-period Sharpe : {best_full[0]} ({best_full[1]['sharpe_ratio']:.2f})")
    print(f"Best mean OOS Sharpe    : {best_oos[0]} ({best_oos[1]:.2f})")
    print("=" * width)
    if best_full[0] == "baseline" and best_oos[0] == "baseline":
        print("Recommendation: keep TSM OFF in production (baseline wins).")
    else:
        print(f"Recommendation: review enabling {best_oos[0]} after manual check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
