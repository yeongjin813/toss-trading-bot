"""
Capital-at-risk sweep for Phase 4 dual (Legacy 70% + Top4 30%).

Compares strategy efficiency across capital levels with live-parity risk
limits scaled proportionally (25% per-ticker cap, 5% daily loss cap).

Usage:
  python scripts/capital_sweep.py
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
FULL_END = "2025-12-31"
OOS_START = "2023-01-01"
OOS_END = "2025-12-31"
LEG_PCT = 70.0
TOP_PCT = 30.0
COMMISSION = 0.001
SLIPPAGE = 5.0

# VTS sandbox orderable USD (~100k). Live account may be higher.
VTS_ORDERABLE_USD = 100_000.0
# User-reported total assets ~3.8억 KRW @ ~1375 FX ≈ $276k deployable ceiling (informative).
ACCOUNT_CEILING_USD = 276_000.0

CAPITAL_LEVELS = (
    50_000,
    75_000,
    100_000,
    125_000,
    150_000,
    200_000,
    250_000,
    300_000,
)


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


def _metrics(series: pd.Series, start: str, end: str, initial: float) -> dict:
    window = series.loc[pd.Timestamp(start) : pd.Timestamp(end)]
    if len(window) < 2:
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0, ret=0.0, ann_profit=0.0)
    s0, sf = float(window.iloc[0]), float(window.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    ret = (sf / s0 - 1.0) * 100.0 if s0 > 0 else 0.0
    ann_profit = initial * (cagr / 100.0)
    return dict(
        cagr=cagr,
        sharpe=compute_sharpe_ratio(window),
        maxdd=compute_max_drawdown(window),
        ret=ret,
        ann_profit=ann_profit,
    )


def _run_dual(
    loaded: list[str],
    full_ohlcv: dict[str, pd.DataFrame],
    full_window: dict[str, pd.DataFrame],
    spy_df: pd.DataFrame | None,
    qqq_df: pd.DataFrame | None,
    vix_df: pd.DataFrame | None,
    total_cash: float,
) -> pd.Series:
    leg_cash = scaled_capital(total_cash, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(total_cash, TOP_PCT / (LEG_PCT + TOP_PCT))

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
    top_mom = replace(
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
    top = run_top3_backtest(
        tickers=loaded,
        ohlcv_by_ticker=full_ohlcv,
        initial_cash=top_cash,
        commission_rate=COMMISSION,
        slippage_bps=SLIPPAGE,
        momentum_settings=top_mom,
        window_start=FULL_START,
        window_end=FULL_END,
    )

    leg_eq = _equity_series(legacy.equity_curve, leg_cash)
    top_eq = _equity_series(top.equity_curve, top_cash)
    return (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill()
        .fillna({"leg": leg_cash, "top": top_cash})
        .sum(axis=1)
    )


def _live_limits(capital: float) -> dict[str, float]:
    """Proportional live env limits (25% ticker, 5% daily loss, portfolio = capital)."""
    return {
        "max_ticker_usd": capital * 0.25,
        "max_daily_loss_usd": capital * 0.05,
        "max_portfolio_usd": capital,
        "legacy_pool": capital * 0.70,
        "top4_pool": capital * 0.30,
        "top4_per_name": capital * 0.30 / 4.0,
    }


def _constraint_score(capital: float, max_open: int = 5) -> dict[str, float]:
    lim = _live_limits(capital)
    # Theoretical full deploy if max_open positions each at ticker cap.
    max_by_ticker_cap = min(lim["max_portfolio_usd"], lim["max_ticker_usd"] * max_open)
    # Top4 slice fully invested = 30% of capital.
    top4_deploy = lim["top4_pool"]
    # Legacy can use remainder up to portfolio cap.
    legacy_headroom = lim["max_portfolio_usd"] - top4_deploy
    efficiency = min(1.0, max_by_ticker_cap / capital) * 100.0
    return {
        "max_deploy_pct": efficiency,
        "top4_per_name_usd": lim["top4_per_name"],
        "max_ticker_usd": lim["max_ticker_usd"],
        "max_daily_loss_usd": lim["max_daily_loss_usd"],
        "legacy_headroom_usd": legacy_headroom,
    }


def main() -> int:
    print("Loading yfinance data...", flush=True)
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(full_watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in full_watchlist if t in full_ohlcv]

    use_spy = StrategyConfigMapper.use_spy_market_filter()
    spy_df = (
        fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START) if use_spy else None
    )
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

    width = 120
    print()
    print("=" * width)
    print(
        f"CAPITAL SWEEP  Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}  Top4+band=1  {FULL_START}->{FULL_END}".center(
            width
        )
    )
    print("=" * width)
    hdr = (
        f"{'Capital':>10}  {'CAGR%':>7}  {'Sharpe':>7}  {'MDD%':>7}  {'C/D':>5}  "
        f"{'OOS CAGR%':>9}  {'$/yr est':>10}  {'VTS':>4}  {'Acct':>4}"
    )
    print(hdr)
    print("-" * width)

    rows: list[dict] = []
    for cash in CAPITAL_LEVELS:
        print(f"  Running ${cash:,.0f}...", flush=True)
        combined = _run_dual(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df, cash
        )
        full_m = _metrics(combined, FULL_START, FULL_END, cash)
        oos_m = _metrics(combined, OOS_START, OOS_END, cash)
        cons = _constraint_score(cash)
        cd = full_m["cagr"] / full_m["maxdd"] if full_m["maxdd"] > 0 else 99.9
        vts_ok = "OK" if cash <= VTS_ORDERABLE_USD else "OVER"
        acct_ok = "OK" if cash <= ACCOUNT_CEILING_USD else "OVER"
        rows.append(
            {
                "capital": cash,
                "full": full_m,
                "oos": oos_m,
                "constraints": cons,
                "cd": cd,
                "vts_ok": vts_ok == "OK",
                "acct_ok": acct_ok == "OK",
            }
        )
        print(
            f"${cash:>9,.0f}  {full_m['cagr']:>+7.1f}  {full_m['sharpe']:>7.2f}  "
            f"{full_m['maxdd']:>7.1f}  {cd:>5.2f}  {oos_m['cagr']:>+9.1f}  "
            f"${full_m['ann_profit']:>9,.0f}  {vts_ok:>4}  {acct_ok:>4}"
        )

    print("-" * width)
    print()
    print("CONSTRAINT TABLE (proportional limits: MAX_TICKER=25%, MAX_DAILY_LOSS=5%)")
    print(
        f"{'Capital':>10}  {'MaxTicker$':>10}  {'Top4/name$':>10}  {'DailyLoss$':>10}  "
        f"{'MaxDeploy%':>10}"
    )
    print("-" * 70)
    for row in rows:
        c = row["constraints"]
        print(
            f"${row['capital']:>9,.0f}  ${c['max_ticker_usd']:>9,.0f}  "
            f"${c['top4_per_name_usd']:>9,.0f}  ${c['max_daily_loss_usd']:>9,.0f}  "
            f"{c['max_deploy_pct']:>9.1f}%"
        )

    # Pick winner: max $/yr among VTS-feasible; tie-break Sharpe / C/D.
    feasible = [r for r in rows if r["vts_ok"]]
    if not feasible:
        feasible = rows
    best = max(
        feasible,
        key=lambda r: (
            r["full"]["ann_profit"],
            r["full"]["sharpe"],
            r["cd"],
        ),
    )
    print()
    print("RECOMMENDATION")
    print("-" * 70)
    print(
        f"  VTS sandbox ceiling     : ${VTS_ORDERABLE_USD:,.0f} orderable USD"
    )
    print(
        f"  Account ceiling (est.)  : ${ACCOUNT_CEILING_USD:,.0f} (~3.8억 KRW @ 1375)"
    )
    print(
        f"  Backtest CAGR invariant : returns scale linearly with capital (same % metrics)"
    )
    print(
        f"  Adopt for VTS now       : ${best['capital']:,.0f} "
        f"(CAGR {best['full']['cagr']:+.1f}%, Sharpe {best['full']['sharpe']:.2f}, "
        f"~${best['full']['ann_profit']:,.0f}/yr on {FULL_START}-{FULL_END})"
    )
    live_candidate = max(
        (r for r in rows if r["acct_ok"]),
        key=lambda r: (r["full"]["ann_profit"], r["full"]["sharpe"], r["cd"]),
        default=best,
    )
    if live_candidate["capital"] > best["capital"]:
        print(
            f"  When on LIVE account    : consider ${live_candidate['capital']:,.0f} "
            f"(~${live_candidate['full']['ann_profit']:,.0f}/yr est) with scaled .env limits"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
