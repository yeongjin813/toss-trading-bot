"""
TopN sweep: Top2 / Top3(prod) / Top4 / Top5 x min_score cash variants.

- TopN sweep: hold top N tickers regardless of score
- Cash variant: exclude tickers with composite score < 0 (holds fewer when market weak)

Dual 70/30 fixed. 2020-2025. Full period + sub-periods.

Usage:
  python scripts/topn_cash_sweep.py
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
from top3_backtest import run_top3_backtest
from trading_features import TradingFeatureFlags

FULL_START = "2020-01-01"
FULL_END   = "2025-12-31"
CASH       = 100_000.0
LEG_PCT    = 70.0
TOP_PCT    = 30.0
COMMISSION = 0.001
SLIPPAGE   = 5.0

SUB_PERIODS = [
    ("bull20-21", "2020-01-01", "2021-12-31"),
    ("bear2022",  "2022-01-01", "2022-12-31"),
    ("bull23-24", "2023-01-01", "2024-12-31"),
    ("trans2025", "2025-01-01", "2025-12-31"),
]

# (label, top_n, min_momentum_score)
SCENARIOS: list[tuple[str, int, float]] = [
    ("Top2",            2, float("-inf")),
    ("Top3 (prod)",     3, float("-inf")),
    ("Top4",            4, float("-inf")),
    ("Top5",            5, float("-inf")),
    ("Top3+cash(s>0)",  3, 0.0),
    ("Top4+cash(s>0)",  4, 0.0),
    ("Top5+cash(s>0)",  5, 0.0),
    ("Top3+cash(s>0.2)",3, 0.2),
    ("Top5+cash(s>0.2)",5, 0.2),
]


def _equity_series(frame: pd.DataFrame, initial: float) -> pd.Series:
    if frame.empty or "equity" not in frame.columns:
        return pd.Series(dtype=float)
    if isinstance(frame.index, pd.DatetimeIndex):
        return frame["equity"].astype(float)
    if "date" in frame.columns:
        return pd.Series(frame["equity"].astype(float).values,
                         index=pd.to_datetime(frame["date"]))
    return frame["equity"].astype(float)


def _window_metrics(combined: pd.Series, start: str, end: str) -> dict:
    sliced = combined.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if sliced.empty:
        return dict(ret=0.0, sharpe=0.0, maxdd=0.0, cagr=0.0)
    s0, sf = float(sliced.iloc[0]), float(sliced.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1/365.25)
    ret = (sf / s0 - 1.0) * 100.0
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    return dict(ret=ret, sharpe=compute_sharpe_ratio(sliced),
                maxdd=compute_max_drawdown(sliced), cagr=cagr)


def _run_one(loaded, full_ohlcv, ohlcv_window, spy_df, qqq_df, vix_df,
             *, top_n: int, min_score: float,
             window_start: str, window_end: str) -> tuple[pd.Series, int]:
    leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))
    features = TradingFeatureFlags(
        use_vol_adjusted_risk=True, vol_target_pct=0.015,
        use_regime_golden_cross=True, regime_cautious_max_positions=2,
        use_scale_in=True, use_scale_out=True,
        use_weekly_trend_filter=True, use_52w_high_filter=False,  # Phase 21: 52w=OFF
        near_52w_high_pct=0.05,
    )

    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom = replace(
        MomentumRankSettings.from_env().for_production().for_top3(),
        enabled=True,
        top_n=top_n,
        min_bars=min(MomentumRankSettings.from_env().min_bars, 60),
        dynamic_rebalance_only=False,
        min_momentum_score=min_score,
    )

    leg_tickers = [t for t in loaded if t in ohlcv_window]
    legacy = run_portfolio_backtest(
        tickers=leg_tickers,
        ohlcv_by_ticker={t: ohlcv_window[t] for t in leg_tickers},
        initial_cash=leg_cash, commission_rate=COMMISSION, slippage_bps=SLIPPAGE,
        use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
        spy_df=spy_df, qqq_df=qqq_df, vix_df=vix_df,
        momentum_settings=legacy_mom, features=features,
    )
    top3 = run_top3_backtest(
        tickers=loaded, ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash, commission_rate=COMMISSION, slippage_bps=SLIPPAGE,
        momentum_settings=top3_mom,
        window_start=window_start, window_end=window_end,
    )
    leg_eq = _equity_series(legacy.equity_curve, leg_cash)
    top_eq = _equity_series(top3.equity_curve, top_cash)
    combined = (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill().fillna({"leg": leg_cash, "top": top_cash}).sum(axis=1)
    )
    return combined, legacy.total_trades + top3.total_trades


def main() -> int:
    print("Loading yfinance data...", flush=True)
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df  = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df  = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter() else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]

    width = 118
    print()
    print("=" * width)
    print(f"TOP-N + CASH THRESHOLD SWEEP  Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}  {FULL_START} -> {FULL_END}  [52w=OFF]".center(width))
    print("=" * width)
    sub_hdr = "  ".join(f"{lbl[:8]:>8}" for lbl, *_ in SUB_PERIODS)
    print(f"  {'Scenario':<24} {'Return':>8}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>6}  {'C/D':>4}  {'Tr':>4}  sub_sharpe[{sub_hdr}]")
    print("-" * width)

    results = []
    for label, top_n, min_score in SCENARIOS:
        print(f"  Running {label}...", flush=True)
        combined, trades = _run_one(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
            top_n=top_n, min_score=min_score,
            window_start=FULL_START, window_end=FULL_END,
        )
        mf = _window_metrics(combined, FULL_START, FULL_END)
        sub_ms = [_window_metrics(combined, s, e) for _, s, e in SUB_PERIODS]
        results.append((label, mf, sub_ms, trades))
        cd = mf["cagr"] / mf["maxdd"] if mf["maxdd"] > 0 else 99.9
        sub_str = "  ".join(f"{m['sharpe']:>8.2f}" for m in sub_ms)
        marker = " <-- prod" if label == "Top3 (prod)" else ""
        print(
            f"  {label:<24} {mf['ret']:>+7.1f}%  {mf['cagr']:>+6.1f}%  "
            f"{mf['sharpe']:>7.2f}  {mf['maxdd']:>5.1f}%  {cd:>4.2f}  {trades:>4d}  "
            f"sub_sharpe[{sub_str}]{marker}"
        )

    print()
    print("DELTA vs Top3 (prod)".center(width))
    print("-" * width)
    base_m, base_sub, base_tr = next((m, s, t) for l, m, s, t in results if l == "Top3 (prod)")
    for label, mf, sub_ms, trades in results:
        if label == "Top3 (prod)":
            continue
        sub_d = "  ".join(f"{(m['sharpe']-bs['sharpe']):>+8.2f}" for m, bs in zip(sub_ms, base_sub))
        print(
            f"  {label:<24} "
            f"DCAGR={mf['cagr']-base_m['cagr']:>+6.2f}pp  "
            f"DSharpe={mf['sharpe']-base_m['sharpe']:>+5.2f}  "
            f"DMaxDD={mf['maxdd']-base_m['maxdd']:>+5.2f}pp  "
            f"DTr={trades-base_tr:>+4d}  "
            f"Dsub[{sub_d}]"
        )

    print("=" * width)
    best = max(results, key=lambda x: x[1]["cagr"] / max(x[1]["maxdd"], 0.01))
    print(f"Best MAR: '{best[0]}' = {best[1]['cagr']/max(best[1]['maxdd'],0.01):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
