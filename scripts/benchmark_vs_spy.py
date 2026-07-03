"""Prod dual strategy vs SPY buy-and-hold (focused benchmark)."""

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
FULL_END = "2025-12-31"
OOS_START = "2023-01-01"
OOS_END = "2025-12-31"
CASH = 100_000.0
LEG_PCT = 70.0
TOP_PCT = 30.0
COMMISSION = 0.001
SLIPPAGE = 5.0


def _equity_series(frame: pd.DataFrame, initial: float) -> pd.Series:
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


def _metrics(series: pd.Series, start: str, end: str) -> dict[str, float]:
    window = series.loc[pd.Timestamp(start) : pd.Timestamp(end)]
    if len(window) < 2:
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0, ret=0.0)
    start_val, end_val = float(window.iloc[0]), float(window.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
    cagr = ((end_val / start_val) ** (1.0 / years) - 1.0) * 100.0 if start_val > 0 else 0.0
    ret = (end_val / start_val - 1.0) * 100.0 if start_val > 0 else 0.0
    return dict(
        cagr=cagr,
        sharpe=compute_sharpe_ratio(window),
        maxdd=compute_max_drawdown(window),
        ret=ret,
    )


def _spy_buy_hold(spy_df: pd.DataFrame, initial: float, start: str, end: str) -> pd.Series:
    window = slice_ohlcv_window(spy_df, start, end)
    if window.empty:
        return pd.Series(dtype=float)
    close_col = "Close" if "Close" in window.columns else "close"
    if close_col not in window.columns:
        return pd.Series(dtype=float)
    closes = window[close_col].astype(float)
    shares = initial / float(closes.iloc[0])
    return closes * shares


def _run_prod_dual(
    loaded: list[str],
    full_ohlcv: dict[str, pd.DataFrame],
    full_window: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    qqq_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
) -> pd.Series:
    leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))
    features = TradingFeatureFlags(
        use_vol_adjusted_risk=True,
        vol_target_pct=0.015,
        use_regime_golden_cross=False,
        regime_cautious_max_positions=2,
        use_scale_in=True,
        use_scale_out=True,
        use_weekly_trend_filter=True,
        use_52w_high_filter=False,
        near_52w_high_pct=0.05,
    )
    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
    top3_mom = replace(
        MomentumRankSettings.from_env().for_production().for_top3(),
        enabled=True,
        top_n=4,
        dynamic_rebalance_only=False,
        top_n_hold_band=1,
    )
    leg_tickers = [t for t in loaded if t in full_window]
    legacy = run_portfolio_backtest(
        tickers=leg_tickers,
        ohlcv_by_ticker={t: full_window[t] for t in leg_tickers},
        initial_cash=leg_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE,
        use_spy_market_filter=StrategyConfigMapper.use_spy_market_filter(),
        spy_df=spy_df,
        qqq_df=qqq_df,
        vix_df=vix_df,
        momentum_settings=legacy_mom,
        features=features,
    )
    top3 = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE,
        momentum_settings=top3_mom,
        window_start=FULL_START,
        window_end=FULL_END,
    )
    leg_eq = _equity_series(legacy.equity_curve, leg_cash)
    top_eq = _equity_series(top3.equity_curve, top_cash)
    return (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill()
        .fillna({"leg": leg_cash, "top": top_cash})
        .sum(axis=1)
    )


def _row(label: str, metrics: dict[str, float]) -> str:
    cd = metrics["cagr"] / metrics["maxdd"] if metrics["maxdd"] > 0 else 99.9
    return (
        f"{label:<16}  CAGR={metrics['cagr']:>+6.1f}%  "
        f"Sh={metrics['sharpe']:>5.2f}  MDD={metrics['maxdd']:>5.1f}%  "
        f"C/D={cd:>4.2f}  Ret={metrics['ret']:>+6.1f}%"
    )


def main() -> int:
    print("Loading data...", flush=True)
    watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in watchlist if t in full_ohlcv]
    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    qqq_df = (
        fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
        if use_spy and StrategyConfigMapper.use_qqq_regime_filter()
        else None
    )
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)
    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]
    if spy_df is None:
        print("ERROR: SPY data required for benchmark")
        return 1

    print("Running prod dual backtest...", flush=True)
    prod_eq = _run_prod_dual(loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df)
    spy_eq = _spy_buy_hold(spy_df, CASH, FULL_START, FULL_END)

    prod_full = _metrics(prod_eq, FULL_START, FULL_END)
    prod_oos = _metrics(prod_eq, OOS_START, OOS_END)
    spy_full = _metrics(spy_eq, FULL_START, FULL_END)
    spy_oos = _metrics(spy_eq, OOS_START, OOS_END)

    width = 88
    print()
    print("=" * width)
    print("PROD DUAL (70/30) vs SPY BUY-AND-HOLD".center(width))
    print("=" * width)
    print(_row("Prod FULL", prod_full))
    print(_row("SPY  FULL", spy_full))
    print(_row("Prod OOS", prod_oos))
    print(_row("SPY  OOS", spy_oos))
    print("-" * width)
    print(
        f"Alpha FULL (prod - SPY): {prod_full['cagr'] - spy_full['cagr']:+.1f}pp CAGR, "
        f"{prod_full['sharpe'] - spy_full['sharpe']:+.2f} Sharpe"
    )
    print(
        f"Alpha OOS  (prod - SPY): {prod_oos['cagr'] - spy_oos['cagr']:+.1f}pp CAGR, "
        f"{prod_oos['sharpe'] - spy_oos['sharpe']:+.2f} Sharpe"
    )
    print("=" * width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
