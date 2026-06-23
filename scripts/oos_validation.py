"""
Out-of-sample (OOS) validation for all Phase 21-22 improvements.

Split: IS = 2020-2022, OOS = 2023-2025
(All param decisions were made looking at 2020-2025 full period,
 so 2023-2025 is the fairest forward test available.)

For each config variant, reports:
  Full (2020-2025) / IS (2020-2022) / OOS (2023-2025)

Key question: do the improvements (52w=OFF, Top4, band=1) hold OOS,
or do they overfit the 2020-2025 window?

Usage:
  python scripts/oos_validation.py
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
IS_START   = "2020-01-01"
IS_END     = "2022-12-31"
OOS_START  = "2023-01-01"
OOS_END    = "2025-12-31"

CASH       = 100_000.0
LEG_PCT    = 70.0
TOP_PCT    = 30.0
COMMISSION = 0.001
SLIPPAGE   = 5.0

# (label, use_52w, top_n, band, description)
CONFIGS = [
    ("baseline",       True,  3, 0, "All defaults pre-Ph21"),
    ("52w=OFF",        False, 3, 0, "Phase 21"),
    ("52w=OFF+Top4",   False, 4, 0, "Phase 21+22a"),
    ("prod (all)",     False, 4, 1, "Phase 21+22a+22b (current prod)"),
    # Regression checks
    ("52w=ON+Top4",    True,  4, 0, "Top4 without 52w change"),
    ("52w=OFF+band=2", False, 3, 2, "band=2 without Top4"),
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


def _metrics(series: pd.Series, start: str, end: str) -> dict:
    s = series.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if len(s) < 2:
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0, ret=0.0)
    s0, sf = float(s.iloc[0]), float(s.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1/365.25)
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    ret  = (sf / s0 - 1.0) * 100.0 if s0 > 0 else 0.0
    return dict(cagr=cagr, sharpe=compute_sharpe_ratio(s),
                maxdd=compute_max_drawdown(s), ret=ret)


def _run_config(loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
                *, use_52w: bool, top_n: int, band: int) -> pd.Series:
    leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))

    features = TradingFeatureFlags(
        use_vol_adjusted_risk=True, vol_target_pct=0.015,
        use_regime_golden_cross=True, regime_cautious_max_positions=2,
        use_scale_in=True, use_scale_out=True,
        use_weekly_trend_filter=True,
        use_52w_high_filter=use_52w,
        near_52w_high_pct=0.05,
    )
    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom = replace(
        MomentumRankSettings.from_env().for_production().for_top3(),
        enabled=True,
        top_n=top_n,
        dynamic_rebalance_only=False,
        top_n_hold_band=band,
    )

    leg_tickers = [t for t in loaded if t in full_window]
    legacy = run_portfolio_backtest(
        tickers=leg_tickers,
        ohlcv_by_ticker={t: full_window[t] for t in leg_tickers},
        initial_cash=leg_cash, commission_rate=COMMISSION, slippage_bps=SLIPPAGE,
        use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
        spy_df=spy_df, qqq_df=qqq_df, vix_df=vix_df,
        momentum_settings=legacy_mom, features=features,
    )
    top3 = run_top3_backtest(
        tickers=loaded, ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash, commission_rate=COMMISSION, slippage_bps=SLIPPAGE,
        momentum_settings=top3_mom,
        window_start=FULL_START, window_end=FULL_END,
    )

    leg_eq = _equity_series(legacy.equity_curve, leg_cash)
    top_eq = _equity_series(top3.equity_curve, top_cash)
    combined = (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill().fillna({"leg": leg_cash, "top": top_cash}).sum(axis=1)
    )
    return combined


def _row(m: dict) -> str:
    cd = m["cagr"] / m["maxdd"] if m["maxdd"] > 0 else 99.9
    return f"CAGR={m['cagr']:>+6.1f}%  Sh={m['sharpe']:>5.2f}  MDD={m['maxdd']:>5.1f}%  C/D={cd:>4.2f}"


def main() -> int:
    print("Loading data...", flush=True)
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

    width = 108
    print()
    print("=" * width)
    print(f"OOS VALIDATION  IS=2020-22  OOS=2023-25  Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}".center(width))
    print("=" * width)
    print(f"  {'Config':<22}  {'FULL 2020-25':^38}  {'IS 2020-22':^38}  {'OOS 2023-25':^38}")
    print(f"  {'':<22}  {'CAGR  Sh   MDD   C/D':^38}  {'CAGR  Sh   MDD   C/D':^38}  {'CAGR  Sh   MDD   C/D':^38}")
    print("-" * width)

    results = []
    for label, use_52w, top_n, band, desc in CONFIGS:
        print(f"  Running {label}...", flush=True)
        series = _run_config(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
            use_52w=use_52w, top_n=top_n, band=band,
        )
        mf  = _metrics(series, FULL_START, FULL_END)
        mis = _metrics(series, IS_START,   IS_END)
        moos= _metrics(series, OOS_START,  OOS_END)
        results.append((label, desc, mf, mis, moos))
        print(f"  {label:<22}  {_row(mf)}  {_row(mis)}  {_row(moos)}")

    print()
    print("OOS DELTA vs baseline (key question: do improvements hold OOS?)".center(width))
    print("-" * width)
    base_f, base_is, base_oos = next((f, i, o) for l, _, f, i, o in results if l == "baseline")
    for label, desc, mf, mis, moos in results:
        if label == "baseline":
            continue
        df_cagr   = mf["cagr"]   - base_f["cagr"]
        dis_cagr  = mis["cagr"]  - base_is["cagr"]
        doos_cagr = moos["cagr"] - base_oos["cagr"]
        df_sh     = mf["sharpe"]   - base_f["sharpe"]
        doos_sh   = moos["sharpe"] - base_oos["sharpe"]
        # OOS holds if doos_cagr > 0 and roughly proportional to df_cagr
        holds = "OK" if doos_cagr > 0 else "FAIL"
        print(
            f"  {label:<22}  Full DCAGR={df_cagr:>+5.2f}pp  "
            f"IS DCAGR={dis_cagr:>+5.2f}pp  "
            f"OOS DCAGR={doos_cagr:>+5.2f}pp  "
            f"OOS DSh={doos_sh:>+5.2f}  [{holds}]  ({desc})"
        )

    print()
    prod = next((f, i, o) for l, _, f, i, o in results if l == "prod (all)")
    print(f"  Prod config OOS: CAGR={prod[2]['cagr']:+.1f}%  Sharpe={prod[2]['sharpe']:.2f}  MaxDD={prod[2]['maxdd']:.1f}%")
    print("=" * width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
