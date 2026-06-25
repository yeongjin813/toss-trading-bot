"""
Phase 25: Regime golden-cross ablation backtest.

Isolates the effect of USE_REGIME_GOLDEN_CROSS on strategy performance.
Phase 24 showed Config C (semi-aggressive, multiple changes) beats prod by +1.9pp CAGR.
The biggest single unknown: is USE_REGIME_GOLDEN_CROSS=false responsible for the gain?

Four scenarios -- everything else IDENTICAL to current prod:
  1. prod-baseline : golden_cross=ON,  cautious_max=2
  2. golden_cross=OFF: golden_cross=OFF, cautious_max=2
  3. cautious_max=3 : golden_cross=ON,  cautious_max=3
  4. both            : golden_cross=OFF, cautious_max=3

Fixed prod settings for all configs:
  USE_52W_HIGH_FILTER=false, MOMENTUM_TOP_N=4, MOMENTUM_TOP_N_HOLD_BAND=1
  Dual 70/30, use_vol_adjusted_risk=True, RSI/volume thresholds unchanged
  COMMISSION=0.001, SLIPPAGE_BPS=5

Adoption criteria (golden_cross=OFF or both):
  - Full DCAGR > 0.5pp
  - OOS DCAGR also positive
  - bear2022 not materially worse (Sharpe not more than -0.10 below prod)

Usage:
  python scripts/golden_cross_ablation.py
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

SUB_PERIODS = [
    ("bull20-21", "2020-01-01", "2021-12-31"),
    ("bear2022",  "2022-01-01", "2022-12-31"),
    ("bull23-24", "2023-01-01", "2024-12-31"),
    ("trans2025", "2025-01-01", "2025-12-31"),
]

# (label, use_regime_golden_cross, regime_cautious_max_positions, description)
CONFIGS = [
    ("prod-baseline",    True,  2, "golden_cross=ON,  cautious_max=2 (current prod)"),
    ("golden_cross=OFF", False, 2, "golden_cross=OFF, cautious_max=2"),
    ("cautious_max=3",   True,  3, "golden_cross=ON,  cautious_max=3"),
    ("both",             False, 3, "golden_cross=OFF, cautious_max=3"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _window_metrics(combined: pd.Series, start: str, end: str) -> dict:
    sliced = combined.loc[pd.Timestamp(start):pd.Timestamp(end)]
    if len(sliced) < 2:
        return dict(cagr=0.0, sharpe=0.0, maxdd=0.0, ret=0.0)
    s0, sf = float(sliced.iloc[0]), float(sliced.iloc[-1])
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1 / 365.25)
    cagr = ((sf / s0) ** (1.0 / years) - 1.0) * 100.0 if s0 > 0 else 0.0
    ret  = (sf / s0 - 1.0) * 100.0 if s0 > 0 else 0.0
    return dict(
        cagr=cagr,
        sharpe=compute_sharpe_ratio(sliced),
        maxdd=compute_max_drawdown(sliced),
        ret=ret,
    )


def _run_config(
    loaded: list[str],
    full_ohlcv: dict,
    full_window: dict,
    spy_df,
    qqq_df,
    vix_df,
    *,
    use_regime_golden_cross: bool,
    regime_cautious_max_positions: int,
) -> tuple[pd.Series, int]:
    """Run dual 70/30 backtest for one config. Returns (combined equity series, total trades)."""
    features = TradingFeatureFlags(
        use_vol_adjusted_risk=True,
        vol_target_pct=0.015,
        use_regime_golden_cross=use_regime_golden_cross,
        regime_cautious_max_positions=regime_cautious_max_positions,
        use_scale_in=True,
        use_scale_out=True,
        use_weekly_trend_filter=True,
        use_52w_high_filter=False,   # Phase 21: 52w-high filter OFF
        near_52w_high_pct=0.05,
    )

    leg_cash = scaled_capital(CASH, LEG_PCT / (LEG_PCT + TOP_PCT))
    top_cash = scaled_capital(CASH, TOP_PCT / (LEG_PCT + TOP_PCT))

    # Legacy leg: momentum ranking disabled (signal-driven entry)
    legacy_mom = replace(MomentumRankSettings.from_env().for_production(), enabled=False)

    # Top4 leg: momentum ranking enabled, Top4 + band=1 (prod settings)
    top3_mom = replace(
        MomentumRankSettings.from_env().for_production().for_top3(),
        enabled=True,
        top_n=4,
        dynamic_rebalance_only=False,
        top_n_hold_band=1,
        min_bars=min(MomentumRankSettings.from_env().min_bars, 60),
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
    combined = (
        pd.concat([leg_eq.rename("leg"), top_eq.rename("top")], axis=1, sort=True)
        .ffill()
        .fillna({"leg": leg_cash, "top": top_cash})
        .sum(axis=1)
    )
    total_trades = legacy.total_trades + top3.total_trades
    return combined, total_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Loading yfinance data...", flush=True)
    full_watchlist = parse_watchlist(os.getenv("WATCHLIST"))
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
        t: slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)
        for t in loaded
        if len(slice_ohlcv_window(full_ohlcv[t], FULL_START, FULL_END)) >= 22
    }
    loaded = [t for t in loaded if t in full_window]
    print(f"Loaded {len(loaded)} tickers for backtest.", flush=True)

    # -----------------------------------------------------------------------
    # Run all configs
    # -----------------------------------------------------------------------
    results = []
    for label, use_gc, cautious_max, desc in CONFIGS:
        print(f"  Running {label}...", flush=True)
        combined, trades = _run_config(
            loaded, full_ohlcv, full_window, spy_df, qqq_df, vix_df,
            use_regime_golden_cross=use_gc,
            regime_cautious_max_positions=cautious_max,
        )
        mf   = _window_metrics(combined, FULL_START, FULL_END)
        mis  = _window_metrics(combined, IS_START,   IS_END)
        moos = _window_metrics(combined, OOS_START,  OOS_END)
        sub_ms = [_window_metrics(combined, s, e) for _, s, e in SUB_PERIODS]
        results.append(dict(
            label=label, desc=desc,
            use_gc=use_gc, cautious_max=cautious_max,
            mf=mf, mis=mis, moos=moos,
            sub_ms=sub_ms, trades=trades, series=combined,
        ))

    # -----------------------------------------------------------------------
    # Main comparison table
    # -----------------------------------------------------------------------
    width = 150
    sub_hdr = "  ".join(f"{lbl[:8]:>8}" for lbl, *_ in SUB_PERIODS)
    print()
    print("=" * width)
    print(
        f"PHASE 25: REGIME GOLDEN-CROSS ABLATION  "
        f"Dual {LEG_PCT:.0f}/{TOP_PCT:.0f}  {FULL_START} -> {FULL_END}"
        .center(width)
    )
    print("=" * width)
    print(
        f"  {'Config':<20}  GC  CM  "
        f"{'CAGR':>7}  {'Sharpe':>6}  {'MaxDD':>6}  {'C/D':>5}  {'Tr':>4}  "
        f"{'IS CAGR':>8}  {'IS Sh':>5}  "
        f"{'OOS CAGR':>9}  {'OOS Sh':>6}  {'OOS MDD':>7}  "
        f"sub_sharpe[{sub_hdr}]"
    )
    print("-" * width)

    for r in results:
        mf   = r["mf"]
        mis  = r["mis"]
        moos = r["moos"]
        sub_ms = r["sub_ms"]
        cd = mf["cagr"] / mf["maxdd"] if mf["maxdd"] > 0 else 99.9
        sub_str = "  ".join(f"{m['sharpe']:>8.2f}" for m in sub_ms)
        gc_str = "OFF" if not r["use_gc"] else "ON "
        cm_str = str(r["cautious_max"])
        marker = "  <-- PROD" if r["label"] == "prod-baseline" else ""
        print(
            f"  {r['label']:<20}  {gc_str}  {cm_str}  "
            f"{mf['cagr']:>+6.1f}%  {mf['sharpe']:>6.2f}  {mf['maxdd']:>5.1f}%  {cd:>5.2f}  {r['trades']:>4d}  "
            f"{mis['cagr']:>+7.1f}%  {mis['sharpe']:>5.2f}  "
            f"{moos['cagr']:>+8.1f}%  {moos['sharpe']:>6.2f}  {moos['maxdd']:>6.1f}%  "
            f"sub_sh[{sub_str}]{marker}"
        )

    # -----------------------------------------------------------------------
    # Delta vs prod-baseline
    # -----------------------------------------------------------------------
    print()
    print("DELTA vs prod-baseline".center(width))
    print("-" * width)
    base = next(r for r in results if r["label"] == "prod-baseline")
    bear_idx = [lbl for lbl, *_ in SUB_PERIODS].index("bear2022")
    for r in results:
        if r["label"] == "prod-baseline":
            continue
        mf_d    = r["mf"]["cagr"]    - base["mf"]["cagr"]
        sh_d    = r["mf"]["sharpe"]  - base["mf"]["sharpe"]
        dd_d    = r["mf"]["maxdd"]   - base["mf"]["maxdd"]
        mis_d   = r["mis"]["cagr"]   - base["mis"]["cagr"]
        oos_d   = r["moos"]["cagr"]  - base["moos"]["cagr"]
        oossh_d = r["moos"]["sharpe"]- base["moos"]["sharpe"]
        tr_d    = r["trades"] - base["trades"]
        sub_d   = "  ".join(
            f"{(m['sharpe'] - bm['sharpe']):>+8.2f}"
            for m, bm in zip(r["sub_ms"], base["sub_ms"])
        )
        bear_sharpe = r["sub_ms"][bear_idx]["sharpe"]
        bear_base   = base["sub_ms"][bear_idx]["sharpe"]
        bear_d      = bear_sharpe - bear_base
        bear_flag = "  *** BEAR RISK ***" if bear_sharpe < -1.20 else ""
        print(
            f"  {r['label']:<20}  "
            f"DCAGR={mf_d:>+5.2f}pp  DSh={sh_d:>+5.2f}  DMD={dd_d:>+5.2f}pp  "
            f"DIS={mis_d:>+5.2f}pp  DOOS={oos_d:>+5.2f}pp  DOOS_Sh={oossh_d:>+5.2f}  DTr={tr_d:>+4d}  "
            f"Dbear_sh={bear_d:>+5.2f}  Dsub[{sub_d}]{bear_flag}"
        )

    # -----------------------------------------------------------------------
    # Sub-period detail
    # -----------------------------------------------------------------------
    print()
    print("SUB-PERIOD DETAIL (CAGR / Sharpe / MaxDD)".center(width))
    print("-" * width)
    for sub_label, sub_start, sub_end in SUB_PERIODS:
        idx = [lbl for lbl, *_ in SUB_PERIODS].index(sub_label)
        print(f"\n  [{sub_label}  {sub_start} -> {sub_end}]")
        for r in results:
            m  = r["sub_ms"][idx]
            cd = m["cagr"] / m["maxdd"] if m["maxdd"] > 0 else 99.9
            warn = ""
            if sub_label == "bear2022" and m["sharpe"] < -1.20:
                warn = "  *** HIGH BEAR RISK ***"
            print(
                f"    {r['label']:<20}  "
                f"CAGR={m['cagr']:>+6.1f}%  Sharpe={m['sharpe']:>5.2f}  "
                f"MaxDD={m['maxdd']:>5.1f}%  C/D={cd:>5.2f}{warn}"
            )

    # -----------------------------------------------------------------------
    # Verdict
    # -----------------------------------------------------------------------
    print()
    print("=" * width)
    print("PHASE 25 VERDICT -- REGIME GOLDEN-CROSS ABLATION".center(width))
    print("=" * width)

    prod_r  = next(r for r in results if r["label"] == "prod-baseline")
    gc_off  = next(r for r in results if r["label"] == "golden_cross=OFF")
    cm3_r   = next(r for r in results if r["label"] == "cautious_max=3")
    both_r  = next(r for r in results if r["label"] == "both")

    prod_bear_sh   = prod_r["sub_ms"][bear_idx]["sharpe"]
    gc_off_bear_sh = gc_off["sub_ms"][bear_idx]["sharpe"]
    both_bear_sh   = both_r["sub_ms"][bear_idx]["sharpe"]

    gc_full_d  = gc_off["mf"]["cagr"]  - prod_r["mf"]["cagr"]
    gc_oos_d   = gc_off["moos"]["cagr"]- prod_r["moos"]["cagr"]
    gc_bear_d  = gc_off_bear_sh - prod_bear_sh

    both_full_d = both_r["mf"]["cagr"]  - prod_r["mf"]["cagr"]
    both_oos_d  = both_r["moos"]["cagr"]- prod_r["moos"]["cagr"]
    both_bear_d = both_bear_sh - prod_bear_sh

    print(f"""
  Prod baseline:    Full CAGR={prod_r['mf']['cagr']:>+.1f}%  OOS CAGR={prod_r['moos']['cagr']:>+.1f}%  MaxDD={prod_r['mf']['maxdd']:.1f}%  bear2022 Sh={prod_bear_sh:.2f}
  golden_cross=OFF: Full CAGR={gc_off['mf']['cagr']:>+.1f}%  OOS CAGR={gc_off['moos']['cagr']:>+.1f}%  MaxDD={gc_off['mf']['maxdd']:.1f}%  bear2022 Sh={gc_off_bear_sh:.2f}
    vs prod: DCAGR={gc_full_d:>+.2f}pp  DOOS={gc_oos_d:>+.2f}pp  Dbear_sh={gc_bear_d:>+.2f}
  both (OFF+max3):  Full CAGR={both_r['mf']['cagr']:>+.1f}%  OOS CAGR={both_r['moos']['cagr']:>+.1f}%  MaxDD={both_r['mf']['maxdd']:.1f}%  bear2022 Sh={both_bear_sh:.2f}
    vs prod: DCAGR={both_full_d:>+.2f}pp  DOOS={both_oos_d:>+.2f}pp  Dbear_sh={both_bear_d:>+.2f}
""")

    # Adoption logic
    gc_adopt_criteria = (
        gc_full_d > 0.5 and
        gc_oos_d > 0.0 and
        gc_bear_d > -0.10
    )
    both_adopt_criteria = (
        both_full_d > 0.5 and
        both_oos_d > 0.0 and
        both_bear_d > -0.10
    )

    if gc_adopt_criteria:
        gc_verdict = "ADOPT"
    elif gc_full_d > 0.0 and gc_oos_d > 0.0:
        gc_verdict = "MARGINAL"
    else:
        gc_verdict = "REJECT"

    if both_adopt_criteria:
        both_verdict = "ADOPT"
    elif both_full_d > 0.0 and both_oos_d > 0.0:
        both_verdict = "MARGINAL"
    else:
        both_verdict = "REJECT"

    print(f"  [golden_cross=OFF verdict] : {gc_verdict}")
    print(f"    Criteria: DCAGR>0.5pp? {gc_full_d:+.2f}pp  OOS>0? {gc_oos_d:+.2f}pp  bear_sh ok? {gc_bear_d:+.2f}")
    print()
    print(f"  [both (OFF+max3) verdict]  : {both_verdict}")
    print(f"    Criteria: DCAGR>0.5pp? {both_full_d:+.2f}pp  OOS>0? {both_oos_d:+.2f}pp  bear_sh ok? {both_bear_d:+.2f}")

    # Korean verdict summary
    print()
    print("  [Korean verdict / Korean summary]")
    print("  " + "-" * 70)

    if gc_verdict == "ADOPT":
        print("  * golden_cross=OFF [채택 권장]")
        print(f"    - golden_cross 필터를 끄면 Full CAGR +{gc_full_d:.2f}pp, OOS CAGR +{gc_oos_d:.2f}pp 개선.")
        print(f"    - bear2022 Sharpe 변화: {gc_bear_d:+.2f} -- 허용 범위 내.")
        print("    - USE_REGIME_GOLDEN_CROSS=false 를 .env.example 에 반영 권장.")
    elif gc_verdict == "MARGINAL":
        print("  * golden_cross=OFF [애매 -- 주의 필요]")
        print(f"    - Full CAGR {gc_full_d:+.2f}pp, OOS CAGR {gc_oos_d:+.2f}pp 개선이지만 기준치(0.5pp) 미달.")
        print(f"    - bear2022 Sharpe 변화: {gc_bear_d:+.2f}. 추가 검토 후 결정.")
    else:
        print("  * golden_cross=OFF [기각]")
        print(f"    - Full CAGR {gc_full_d:+.2f}pp, OOS CAGR {gc_oos_d:+.2f}pp -- 유의미한 개선 없음.")
        print("    - USE_REGIME_GOLDEN_CROSS 현행 유지 (true) 권장.")

    if both_verdict == "ADOPT":
        print("  * both (golden_cross=OFF + cautious_max=3) [채택 권장]")
        print(f"    - Full CAGR +{both_full_d:.2f}pp, OOS CAGR +{both_oos_d:.2f}pp, bear2022 Sharpe {both_bear_d:+.2f}.")
    elif both_verdict == "MARGINAL":
        print("  * both (golden_cross=OFF + cautious_max=3) [애매]")
        print(f"    - Full CAGR {both_full_d:+.2f}pp, OOS CAGR {both_oos_d:+.2f}pp -- 단독 변경과 비교 필요.")
    else:
        print("  * both (golden_cross=OFF + cautious_max=3) [기각]")

    print("  " + "-" * 70)
    print()

    print("=" * width)
    best = max(results, key=lambda r: r["mf"]["cagr"] / max(r["mf"]["maxdd"], 0.01))
    print(f"  Best MAR (full period): '{best['label']}' = {best['mf']['cagr'] / max(best['mf']['maxdd'], 0.01):.3f}")
    best_oos = max(results, key=lambda r: r["moos"]["cagr"] / max(r["moos"]["maxdd"], 0.01))
    print(f"  Best OOS MAR:           '{best_oos['label']}' = {best_oos['moos']['cagr'] / max(best_oos['moos']['maxdd'], 0.01):.3f}")
    print("=" * width)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
