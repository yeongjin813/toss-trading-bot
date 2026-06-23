"""
Exit parameter sweep: ATR multiplier + min_hold_days.

Findings from exit reason audit:
- 100% of all backtest exits are 'hard_stop' (ATR trailing stop)
- profit_trail / trend_exit / scale_out NEVER fire -- ATR stop always fires first
  because ATR_mult * ATR ≈ profit_trail_drawdown_pct (by design)
- The REAL 'let winners run' parameter is atr_multiplier (wider = trailing stop farther from peak)
- min_hold_days blocks premature exits after entry

Sweep:
  Part 1: ATR multiplier grid for HIGH_BETA (NVDA/META/AVGO/NFLX) x MOMENTUM (PLTR/TSLA/CRWD/AMD)
           MEGA stays at 2.5; DEFAULT stays at 2.0
  Part 2: min_hold_days sweep (same across all regimes)

Dual 70/30. 2020-2025 full + 2023-2025 OOS check.

Usage:
  python scripts/exit_sweep.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from config import StrategyConfigMapper, TickerConfig
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
OOS_START  = "2023-01-01"
OOS_END    = "2025-12-31"

CASH       = 100_000.0
LEG_PCT    = 70.0
TOP_PCT    = 30.0
COMMISSION = 0.001
SLIPPAGE   = 5.0

# Baseline ATR multipliers (current prod)
BASE_HB_ATR  = 3.0   # HIGH_BETA (NVDA, META, AVGO, NFLX)
BASE_MO_ATR  = 3.5   # MOMENTUM (PLTR, TSLA, CRWD, AMD)
BASE_HOLD    = 5

# Sweep values
HB_ATR_VALUES = [2.0, 2.5, 3.0, 3.5, 4.5]
MO_ATR_VALUES = [2.5, 3.0, 3.5, 4.5, 6.0]
HOLD_VALUES   = [3, 5, 10, 15]


def _patch_atr(hb_atr: float, mo_atr: float, hold: int) -> None:
    """Patch HIGH_BETA and MOMENTUM configs in-place (class variables)."""
    orig_hb = StrategyConfigMapper._HIGH_BETA
    orig_mo = StrategyConfigMapper._MOMENTUM
    hb_new = replace(orig_hb, atr_multiplier=hb_atr, min_hold_days=hold)
    mo_new = replace(orig_mo, atr_multiplier=mo_atr, min_hold_days=hold)
    StrategyConfigMapper._HIGH_BETA = hb_new
    StrategyConfigMapper._MOMENTUM  = mo_new
    orig_ex = dict(StrategyConfigMapper._EXPLICIT)
    StrategyConfigMapper._EXPLICIT = {
        k: (hb_new if v is orig_hb else (mo_new if v is orig_mo else v))
        for k, v in orig_ex.items()
    }
    return orig_hb, orig_mo, orig_ex


def _restore_atr(orig_hb, orig_mo, orig_ex) -> None:
    StrategyConfigMapper._HIGH_BETA = orig_hb
    StrategyConfigMapper._MOMENTUM  = orig_mo
    StrategyConfigMapper._EXPLICIT  = orig_ex


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
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0)
    s0, sf = float(s.iloc[0]), float(s.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    return dict(cagr=cagr, sharpe=compute_sharpe_ratio(s), maxdd=compute_max_drawdown(s))


def _run(loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
         *, hb_atr: float, mo_atr: float, hold: int) -> pd.Series:
    orig_hb, orig_mo, orig_ex = _patch_atr(hb_atr, mo_atr, hold)
    try:
        leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
        top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))
        features = TradingFeatureFlags(
            use_vol_adjusted_risk=True, vol_target_pct=0.015,
            use_regime_golden_cross=True, regime_cautious_max_positions=2,
            use_scale_in=True, use_scale_out=True,
            use_weekly_trend_filter=True, use_52w_high_filter=False,
            near_52w_high_pct=0.05,
        )
        legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)
        top3_mom = replace(
            MomentumRankSettings.from_env().for_production().for_top3(),
            enabled=True, top_n=4, dynamic_rebalance_only=False, top_n_hold_band=1,
        )
        leg_tickers = [t for t in loaded if t in full_window]
        legacy = run_portfolio_backtest(
            tickers=leg_tickers,
            ohlcv_by_ticker={t: full_window[t] for t in leg_tickers},
            initial_cash=leg_cash, commission_rate=COMMISSION, slippage_bps=SLIPPAGE,
            use_spy_market_filter=True,
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
    finally:
        _restore_atr(orig_hb, orig_mo, orig_ex)
    return combined


def main() -> int:
    print("Loading data...", flush=True)
    watchlist = parse_watchlist(os.getenv("WATCHLIST"))
    full_ohlcv, skipped = load_yfinance_watchlist_data(watchlist, start=YFINANCE_WARMUP_START)
    if skipped:
        print("Skipped:", ", ".join(skipped))
    loaded = [t for t in watchlist if t in full_ohlcv]

    spy_df = fetch_yfinance_ohlcv(BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
    qqq_df = fetch_yfinance_ohlcv(SECONDARY_BENCHMARK_TICKER, start=YFINANCE_WARMUP_START)
    vix_df = load_vix_frame(start=YFINANCE_WARMUP_START)

    full_window = {
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]

    # ── SWEEP 1: ATR multiplier grid ─────────────────────────────────────────
    width = 130
    print()
    print("=" * width)
    print("EXIT SWEEP 1 -- ATR multiplier grid  HIGH_BETA x MOMENTUM  (hold=5)  [52w=OFF, Top4, band=1]".center(width))
    print("  Note: 100% of exits are ATR trailing stop; profit_trail/trend_exit are redundant.")
    print("  Wider multiplier = stop tracks farther from peak = winners run longer / bigger drawdowns.")
    print("=" * width)
    header = "  {:>18}".format("HB_ATR \\ MO_ATR")
    for mo in MO_ATR_VALUES:
        header += f"  MO={mo:.1f}(CAGR/Sh/MDD|OOS-CAGR)"
    print(header)
    print("-" * width)

    grid: dict[tuple, dict] = {}
    base_key = (BASE_HB_ATR, BASE_MO_ATR)

    for hb_atr in HB_ATR_VALUES:
        print(f"  HB={hb_atr:.1f}      ", end="", flush=True)
        for mo_atr in MO_ATR_VALUES:
            series = _run(loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
                         hb_atr=hb_atr, mo_atr=mo_atr, hold=5)
            mf   = _metrics(series, FULL_START, FULL_END)
            moos = _metrics(series, OOS_START, OOS_END)
            grid[(hb_atr, mo_atr)] = dict(full=mf, oos=moos)
            cd = mf["cagr"] / mf["maxdd"] if mf["maxdd"] > 0 else 99.9
            marker = "*" if (hb_atr, mo_atr) == base_key else " "
            print(f"  {mf['cagr']:>+5.1f}/{mf['sharpe']:.2f}/{mf['maxdd']:.1f}|{moos['cagr']:>+5.1f}{marker}", end="")
        print()

    # ── SWEEP 2: min_hold_days ────────────────────────────────────────────────
    print()
    print("=" * width)
    print(f"EXIT SWEEP 2 -- min_hold_days  (HB_ATR={BASE_HB_ATR}, MO_ATR={BASE_MO_ATR} fixed)".center(width))
    print("=" * width)
    print(f"  {'hold_days':>10}  {'CAGR':>7}  {'Sharpe':>7}  {'MaxDD':>6}  {'C/D':>4}  {'OOS CAGR':>9}  {'OOS Sh':>7}  vs prod")
    print("-" * width)

    hold_results = []
    for hold in HOLD_VALUES:
        series = _run(loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
                     hb_atr=BASE_HB_ATR, mo_atr=BASE_MO_ATR, hold=hold)
        mf   = _metrics(series, FULL_START, FULL_END)
        moos = _metrics(series, OOS_START, OOS_END)
        hold_results.append((hold, mf, moos))

    base_hold = next((mf, moos) for h, mf, moos in hold_results if h == BASE_HOLD)
    for hold, mf, moos in hold_results:
        cd = mf["cagr"] / mf["maxdd"] if mf["maxdd"] > 0 else 99.9
        marker = " <-- prod" if hold == BASE_HOLD else ""
        dcagr = mf["cagr"] - base_hold[0]["cagr"]
        print(
            f"  {hold:>10}  {mf['cagr']:>+6.1f}%  {mf['sharpe']:>7.2f}  "
            f"{mf['maxdd']:>5.1f}%  {cd:>4.2f}  "
            f"{moos['cagr']:>+8.1f}%  {moos['sharpe']:>7.2f}  "
            f"DCAGR={dcagr:>+5.2f}pp{marker}"
        )

    print()
    best_k = max(grid, key=lambda k: grid[k]["full"]["cagr"] / max(grid[k]["full"]["maxdd"], 0.01))
    bf = grid[best_k]["full"]
    bo = grid[best_k]["oos"]
    base_f = grid[base_key]["full"]
    print(f"  Best MAR: HB_ATR={best_k[0]} MO_ATR={best_k[1]}")
    print(f"    Full CAGR={bf['cagr']:+.2f}% Sh={bf['sharpe']:.2f} MDD={bf['maxdd']:.1f}%")
    print(f"    OOS  CAGR={bo['cagr']:+.2f}% Sh={bo['sharpe']:.2f}")
    print(f"    vs prod: DCAGR={bf['cagr']-base_f['cagr']:>+.2f}pp  DSharpe={bf['sharpe']-base_f['sharpe']:>+.2f}")
    print("=" * width)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
